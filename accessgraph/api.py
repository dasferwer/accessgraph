import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal

import redis
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from psycopg.errors import LockNotAvailable, QueryCanceled
from pydantic import BaseModel, Field, model_validator

from accessgraph.db import connect, init
from accessgraph.repository import evaluate, would_cycle


@asynccontextmanager
async def lifespan(app):
    mutation_timeouts()
    init()
    yield


def authorize(x_api_key: str = Header(default="")):
    configured = json.loads(os.environ.get("TENANT_KEYS", "{}"))
    fallback = os.environ.get("API_KEY", "")
    if fallback:
        configured.setdefault("default", fallback)
    for tenant, key in configured.items():
        if key and secrets.compare_digest(x_api_key, key):
            return tenant
    raise HTTPException(401, "Неверный API-ключ")


app = FastAPI(title="AccessGraph", lifespan=lifespan, dependencies=[Depends(authorize)])


class Edge(BaseModel):
    resource: str = Field(pattern=r"^(folder|document|team):[a-zA-Z0-9_-]{1,80}$")
    relation: Literal["owner", "editor", "viewer", "parent", "member"]
    subject: str = Field(pattern=r"^(folder|team|user):[a-zA-Z0-9_-]{1,80}$")

    @model_validator(mode="after")
    def types(self):
        resource = self.resource.split(":")[0]
        subject = self.subject.split(":")[0]
        if self.relation == "member":
            valid = resource == "team" and subject in {"user", "team"}
        elif self.relation == "parent":
            valid = resource in {"folder", "document"} and subject == "folder"
        else:
            valid = resource in {"folder", "document"} and subject in {"user", "team"}
        if not valid:
            raise ValueError("Несовместимые типы объектов и отношения")
        return self


class Checks(BaseModel):
    permission: Literal["view", "edit", "manage"] = "view"
    min_revision: int = Field(default=0, ge=0)
    user: str = Field(pattern=r"^user:[a-zA-Z0-9_-]{1,80}$")
    resources: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def resource_types(self):
        import re

        if any(
            not re.fullmatch(r"(?:folder|document):[a-zA-Z0-9_-]{1,80}", item)
            for item in self.resources
        ):
            raise ValueError("Проверять можно папки и документы")
        return self


class MutationTimeouts(BaseModel):
    statement_ms: int = Field(ge=1, le=30000)
    lock_ms: int = Field(ge=1, le=10000)

    @model_validator(mode="after")
    def lock_budget(self):
        if self.lock_ms >= self.statement_ms:
            raise ValueError("Бюджет блокировки должен быть меньше SQL-бюджета")
        return self


def mutation_timeouts():
    return MutationTimeouts(
        statement_ms=os.environ.get("MUTATION_STATEMENT_TIMEOUT_MS", "2000"),
        lock_ms=os.environ.get("MUTATION_LOCK_TIMEOUT_MS", "500"),
    )


def mutate_many(conn, tenant, additions, removals):
    budgets = mutation_timeouts()
    conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{budgets.statement_ms}ms",))
    conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{budgets.lock_ms}ms",))
    conn.execute(
        "INSERT INTO tenant_revision(tenant) VALUES (%s) ON CONFLICT DO NOTHING", (tenant,)
    )
    revision = conn.execute(
        "SELECT value FROM tenant_revision WHERE tenant=%s FOR UPDATE", (tenant,)
    ).fetchone()["value"]
    changed = []
    for edge in removals:
        values = (tenant, edge.resource, edge.relation, edge.subject)
        if conn.execute(
            "DELETE FROM edges WHERE tenant=%s AND resource=%s AND relation=%s AND subject=%s",
            values,
        ).rowcount:
            changed.append(("remove", edge))
    for edge in additions:
        if edge.relation in ("parent", "member") and would_cycle(
            conn, tenant, edge.resource, edge.subject, edge.relation
        ):
            raise HTTPException(409, "Отношение создаёт цикл")
        values = (tenant, edge.resource, edge.relation, edge.subject)
        if conn.execute(
            "INSERT INTO edges(tenant,resource,relation,subject) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            values,
        ).rowcount:
            changed.append(("add", edge))
    if changed:
        revision = conn.execute(
            "UPDATE tenant_revision SET value=value+1 WHERE tenant=%s RETURNING value", (tenant,)
        ).fetchone()["value"]
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO audit(tenant,revision,action,resource,relation,subject) VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    (tenant, revision, action, e.resource, e.relation, e.subject)
                    for action, e in changed
                ],
            )
    return {"revision": revision}


def mutate(tenant, additions, removals):
    try:
        with connect() as conn:
            return mutate_many(conn, tenant, additions, removals)
    except LockNotAvailable as exc:
        raise HTTPException(503, "Превышен бюджет ожидания блокировки; изменения отменены") from exc
    except QueryCanceled as exc:
        raise HTTPException(503, "Превышен SQL-бюджет мутации; изменения отменены") from exc


@app.post("/relations")
def add(edge: Edge, tenant: str = Depends(authorize)):
    return mutate(tenant, [edge], [])


@app.delete("/relations")
def remove(edge: Edge, tenant: str = Depends(authorize)):
    return mutate(tenant, [], [edge])


class Changes(BaseModel):
    additions: list[Edge] = Field(default_factory=list, max_length=100)
    removals: list[Edge] = Field(default_factory=list, max_length=100)


@app.post("/relations/batch")
def batch(body: Changes, tenant: str = Depends(authorize)):
    return mutate(tenant, body.additions, body.removals)


@app.get("/audit")
def audit(
    after: int = 0, limit: int = Query(default=100, ge=1, le=1000), tenant: str = Depends(authorize)
):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM audit WHERE tenant=%s AND id>%s ORDER BY id LIMIT %s",
            (tenant, after, limit),
        ).fetchall()


@app.post("/check")
def checks(body: Checks, tenant: str = Depends(authorize)):
    cache = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6331/0"),
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    with connect() as conn:
        # Версия и отношения читаются из одного снимка первичной БД.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout='2s'")
        version = conn.execute(
            "SELECT value FROM tenant_revision WHERE tenant=%s", (tenant,)
        ).fetchone()
        revision = version["value"] if version else 0
        if revision < body.min_revision:
            raise HTTPException(409, "Запрошенная версия ещё недоступна")
        namespace = hashlib.sha256(os.environ["DATABASE_URL"].encode()).hexdigest()[:16]
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        key = f"accessgraph:v2:{namespace}:{tenant}:{revision}:{digest}"
        try:
            cached = cache.get(key)
            if cached:
                cache.close()
                return json.loads(cached)
        except redis.RedisError:
            pass
        try:
            evaluated, visited = evaluate(conn, tenant, body.user, body.resources, body.permission)
        except ValueError as exc:
            raise HTTPException(413, str(exc)) from exc
        except QueryCanceled as exc:
            raise HTTPException(503, "Превышен бюджет проверки прав; решение не принято") from exc
        result = {
            "revision": revision,
            "checks": evaluated,
            "visited_edges": visited,
            "permission": body.permission,
        }

    try:
        cache.setex(key, 60, json.dumps(result))
    except redis.RedisError:
        pass
    finally:
        cache.close()
    return result


@app.get("/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}
