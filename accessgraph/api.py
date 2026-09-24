import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal

import redis
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from accessgraph.db import connect, init
from accessgraph.graph import check, creates_cycle


@asynccontextmanager
async def lifespan(app):
    init()
    yield


def authorize(x_api_key: str = Header(default="")):
    key = os.environ.get("API_KEY", "")
    if not key or not secrets.compare_digest(x_api_key, key):
        raise HTTPException(401, "Неверный API-ключ")


app = FastAPI(title="AccessGraph", lifespan=lifespan, dependencies=[Depends(authorize)])


class Edge(BaseModel):
    resource: str = Field(pattern=r"^(folder|document|team):[a-zA-Z0-9_-]{1,80}$")
    relation: Literal["owner", "editor", "parent", "member"]
    subject: str = Field(pattern=r"^(folder|team|user):[a-zA-Z0-9_-]{1,80}$")

    @model_validator(mode="after")
    def types(self):
        resource = self.resource.split(":")[0]
        subject = self.subject.split(":")[0]
        if self.relation == "member":
            valid = resource == "team" and subject == "user"
        elif self.relation == "parent":
            valid = resource in {"folder", "document"} and subject == "folder"
        else:
            valid = resource in {"folder", "document"} and subject in {"user", "team"}
        if not valid:
            raise ValueError("Несовместимые типы объектов и отношения")
        return self


class Checks(BaseModel):
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


def mutate(edge, remove=False):
    with connect() as conn:
        revision = conn.execute("SELECT value FROM revision WHERE singleton FOR UPDATE").fetchone()[
            "value"
        ]
        values = (edge.resource, edge.relation, edge.subject)
        if remove:
            result = conn.execute(
                "DELETE FROM edges WHERE resource=%s AND relation=%s AND subject=%s", values
            )
        else:
            edges = conn.execute(
                "SELECT * FROM edges ORDER BY resource,relation,subject"
            ).fetchall()
            if edge.model_dump() not in edges:
                if len(edges) >= 10000:
                    raise HTTPException(409, "Лимит стенда: 10 000 отношений")
                if edge.relation == "parent" and creates_cycle(edges, edge.resource, edge.subject):
                    raise HTTPException(409, "Отношение создаёт цикл")
            result = conn.execute(
                "INSERT INTO edges VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", values
            )
        if result.rowcount:
            revision = conn.execute("UPDATE revision SET value=value+1 RETURNING value").fetchone()[
                "value"
            ]
    return {"revision": revision}


@app.post("/relations")
def add(edge: Edge):
    return mutate(edge)


@app.delete("/relations")
def remove(edge: Edge):
    return mutate(edge, True)


@app.post("/check")
def checks(body: Checks):
    cache = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6331/0"),
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    with connect() as conn:
        # Версия и отношения читаются из одного снимка первичной БД.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        revision = conn.execute("SELECT value FROM revision WHERE singleton").fetchone()["value"]
        namespace = hashlib.sha256(os.environ["DATABASE_URL"].encode()).hexdigest()[:16]
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        key = f"accessgraph:{namespace}:{revision}:{digest}"
        try:
            cached = cache.get(key)
            if cached:
                cache.close()
                return json.loads(cached)
        except redis.RedisError:
            pass
        edges = conn.execute("SELECT * FROM edges ORDER BY resource,relation,subject").fetchall()
        result = {
            "revision": revision,
            "checks": {resource: check(edges, body.user, resource) for resource in body.resources},
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
