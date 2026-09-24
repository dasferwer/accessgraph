import os
from concurrent.futures import ThreadPoolExecutor

import redis


def edge(resource, relation, subject):
    return {"resource": resource, "relation": relation, "subject": subject}


def check(client):
    return client.post(
        "/check", json={"user": "user:a", "resources": ["document:d", "folder:f"]}
    ).json()


def setup(client):
    for relation in [
        edge("document:d", "parent", "folder:f"),
        edge("folder:f", "editor", "team:t"),
        edge("team:t", "member", "user:a"),
    ]:
        assert client.post("/relations", json=relation).status_code == 200


def test_cached_permission_revoked_immediately(client):
    cache = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6331/0"))
    assert cache.ping()
    setup(client)
    before = check(client)
    assert all(item["allowed"] for item in before["checks"].values())
    assert check(client) == before
    revoked = client.request("DELETE", "/relations", json=edge("team:t", "member", "user:a")).json()
    after = check(client)
    assert after["revision"] == revoked["revision"] > before["revision"]
    assert all(not item["allowed"] for item in after["checks"].values())
    cache.close()


def test_redis_failure_uses_current_database(client, monkeypatch):
    setup(client)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    assert check(client)["checks"]["document:d"]["allowed"]
    client.request("DELETE", "/relations", json=edge("team:t", "member", "user:a"))
    assert not check(client)["checks"]["document:d"]["allowed"]


def test_concurrent_cycle_mutations_are_serialized(client):
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(client.post, "/relations", json=relation)
            for relation in [
                edge("folder:a", "parent", "folder:b"),
                edge("folder:b", "parent", "folder:a"),
            ]
        ]
    assert sorted(f.result().status_code for f in futures) == [200, 409]


def test_idempotent_mutation_and_types(client):
    relation = edge("folder:f", "owner", "user:a")
    assert (
        client.post("/relations", json=relation).json()
        == client.post("/relations", json=relation).json()
    )
    assert client.post("/relations", json=edge("team:t", "parent", "folder:f")).status_code == 422
    assert client.post("/relations", json=edge("folder:f", "parent", "folder:f")).status_code == 409
    assert (
        client.post("/check", json={"user": "user:a", "resources": ["team:t"]}).status_code == 422
    )
    assert client.get("/health", headers={"X-API-Key": "bad"}).status_code == 401


def test_tenant_keys_isolate_graph_revision_cache_and_audit(client, monkeypatch):
    monkeypatch.setenv("TENANT_KEYS", '{"alpha":"alpha-key","beta":"beta-key"}')
    alpha = {"X-API-Key": "alpha-key"}
    beta = {"X-API-Key": "beta-key"}
    relation = edge("folder:f", "owner", "user:a")
    assert client.post("/relations", json=relation, headers=alpha).json()["revision"] == 1
    body = {"user": "user:a", "resources": ["folder:f"]}
    assert client.post("/check", json=body, headers=alpha).json()["checks"]["folder:f"]["allowed"]
    assert not client.post("/check", json=body, headers=beta).json()["checks"]["folder:f"][
        "allowed"
    ]
    assert client.get("/audit", headers=beta).json() == []
    assert len(client.get("/audit", headers=alpha).json()) == 1
    assert client.post("/check", json={**body, "min_revision": 1}, headers=beta).status_code == 409
    # Произвольный заголовок арендатора не заменяет область API-ключа.
    assert not client.post("/check", json=body, headers={**beta, "X-Tenant": "alpha"}).json()[
        "checks"
    ]["folder:f"]["allowed"]


def test_permissions_nested_teams_and_revocation(client):
    additions = [
        edge("team:inner", "member", "user:a"),
        edge("team:outer", "member", "team:inner"),
        edge("folder:root", "viewer", "team:outer"),
        edge("document:d", "parent", "folder:root"),
    ]
    assert client.post("/relations/batch", json={"additions": additions}).status_code == 200
    body = {"user": "user:a", "resources": ["document:d"]}
    assert client.post("/check", json=body).json()["checks"]["document:d"]["allowed"]
    for permission in ("edit", "manage"):
        assert not client.post("/check", json={**body, "permission": permission}).json()["checks"][
            "document:d"
        ]["allowed"]
    client.request("DELETE", "/relations", json=additions[1])
    assert not client.post("/check", json=body).json()["checks"]["document:d"]["allowed"]


def test_batch_cycle_rolls_back_all_changes_and_audit(client):
    additions = [edge("team:a", "member", "team:b"), edge("team:b", "member", "team:a")]
    assert client.post("/relations/batch", json={"additions": additions}).status_code == 409
    assert client.get("/audit").json() == []
    # После отката нет половины цикла, поэтому обратное ребро можно добавить отдельно.
    assert client.post("/relations", json=additions[1]).status_code == 200


def test_only_reachable_subgraph_is_loaded(client):
    from accessgraph.db import connect

    setup(client)
    with connect() as conn, conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO edges(tenant,resource,relation,subject) VALUES ('default',%s,'owner','user:other')",
            [(f"folder:unrelated_{i}",) for i in range(11000)],
        )
    result = check(client)
    assert result["checks"]["document:d"]["allowed"]
    assert result["visited_edges"] == 3
