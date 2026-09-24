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
