from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest
from test_api import check, edge, setup

from accessgraph import api
from accessgraph.db import connect


@pytest.mark.parametrize("tenant", ["default", "new"])
@pytest.mark.parametrize(
    "method,path", [("POST", "/relations"), ("DELETE", "/relations"), ("POST", "/relations/batch")]
)
def test_lock_timeout_returns_503_without_mutation(client, monkeypatch, method, path, tenant):
    monkeypatch.setenv("MUTATION_STATEMENT_TIMEOUT_MS", "1000")
    monkeypatch.setenv("MUTATION_LOCK_TIMEOUT_MS", "50")
    relation = edge("folder:f", "owner", "user:a")
    payload = {"additions": [relation]} if path.endswith("batch") else relation
    monkeypatch.setenv("TENANT_KEYS", '{"new":"new-key"}')
    holder = connect()
    if tenant == "new":
        holder.execute("INSERT INTO tenant_revision(tenant) VALUES ('new')")
    holder.execute("SELECT value FROM tenant_revision WHERE tenant=%s FOR UPDATE", (tenant,))
    pool = ThreadPoolExecutor(1)
    future = pool.submit(
        client.request,
        method,
        path,
        json=payload,
        headers={"X-API-Key": "new-key" if tenant == "new" else "test-key"},
    )
    timed_out = False
    try:
        try:
            response = future.result(timeout=1)
        except TimeoutError:
            timed_out = True
    finally:
        holder.rollback()
        holder.close()
        pool.shutdown(wait=True)
    assert not timed_out, "Mutation waited beyond its configured lock budget"
    assert response.status_code == 503
    assert "блокировки" in response.json()["detail"]
    assert client.get("/audit").json() == []
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM edges").fetchone()["n"] == 0
        revision = conn.execute(
            "SELECT value FROM tenant_revision WHERE tenant=%s", (tenant,)
        ).fetchone()
        assert revision is None if tenant == "new" else revision["value"] == 0


def test_sql_timeout_rolls_back_partial_batch_and_keeps_cached_permission(client, monkeypatch):
    setup(client)
    before = check(client)
    audit_before = client.get("/audit").json()
    original = api.would_cycle

    def delayed_cycle(conn, *args):
        conn.execute("SELECT pg_sleep(0.2)")
        return original(conn, *args)

    monkeypatch.setattr(api, "would_cycle", delayed_cycle)
    monkeypatch.setenv("MUTATION_STATEMENT_TIMEOUT_MS", "50")
    monkeypatch.setenv("MUTATION_LOCK_TIMEOUT_MS", "20")
    response = client.post(
        "/relations/batch",
        json={
            "removals": [edge("team:t", "member", "user:a")],
            "additions": [
                edge("folder:temporary", "owner", "user:a"),
                edge("folder:x", "parent", "folder:y"),
            ],
        },
    )
    assert response.status_code == 503
    assert "SQL" in response.json()["detail"]
    assert client.get("/audit").json() == audit_before
    assert check(client) == before
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM edges WHERE resource IN ('folder:temporary','folder:x')"
            ).fetchone()["n"]
            == 0
        )
    # Следующая мутация работает, а настоящий отзыв сразу меняет решение кеша.
    assert (
        client.request("DELETE", "/relations", json=edge("team:t", "member", "user:a")).status_code
        == 200
    )
    assert not check(client)["checks"]["document:d"]["allowed"]


@pytest.mark.parametrize(
    "statement,lock", [("0", "1"), ("30001", "1"), ("1000", "0"), ("20000", "10001"), ("50", "50")]
)
def test_invalid_mutation_budgets_prevent_startup(client, monkeypatch, statement, lock):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MUTATION_STATEMENT_TIMEOUT_MS", statement)
    monkeypatch.setenv("MUTATION_LOCK_TIMEOUT_MS", lock)
    with pytest.raises(ValueError), TestClient(api.app):
        pass
