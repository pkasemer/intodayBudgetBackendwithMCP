import httpx

from app.db import run_migrations

from . import helpers as h
from .conftest import TOKEN


async def test_migrations_apply_once(app):
    assert await run_migrations(app.state.settings.database_url) == []
    async with app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT version FROM schema_migrations")
        assert [r["version"] for r in await cur.fetchall()] == ["001_init", "002_usage"]


async def test_works_with_redis_down(app_no_redis):
    transport = httpx.ASGITransport(app=app_no_redis)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        health = (await c.get("/healthz")).json()
        assert health == {"status": "degraded", "db": "up", "redis": "down"}
        p = h.plan()
        r = await h.push(c, {"month_plans": [p], "expenses": [h.expense(p["id"])]}, [h.today_snapshot()], key="k1")
        assert r.status_code == 200 and r.json()["rejected"] == []
        # Idempotency still holds via Postgres alone.
        assert (await h.push(c, {"month_plans": [p]}, key="k1")).json() == r.json()
        assert (await h.pull(c, r.json()["cursor"])).status_code == 200
        assert (await h.call_tool(c, "get_today", {"date": "2026-10-27"}))["isError"] is False
        wrong = httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": "Bearer bad"})
        assert (await wrong.get("/v1/mcp")).status_code == 401  # auth still enforced


async def test_sync_rate_limit(client):
    cursor = "czE6MA"
    statuses = [(await h.pull(client, cursor)).status_code for _ in range(121)]
    assert statuses[:120] == [200] * 120
    last = await h.pull(client, cursor)
    assert statuses[120] == 429 and last.status_code == 429
    assert int(last.headers["Retry-After"]) >= 1
    assert last.json()["error"]["code"] == "rate_limited"
    # The MCP bucket is separate.
    assert (await client.get("/v1/mcp")).status_code == 200


async def test_mcp_rate_limit(client):
    codes = [(await client.get("/v1/mcp")).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


async def test_failed_auth_attempts_are_throttled_but_valid_token_is_not(app, client):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as bad:
        codes = [(await bad.get("/v1/mcp", headers={"Authorization": f"Bearer guess-{i}"})).status_code for i in range(32)]
    assert codes[:30] == [401] * 30 and codes[30:] == [429, 429]
    assert (await client.get("/v1/mcp")).status_code == 200


async def test_body_size_cap(client):
    r = await client.post("/v1/sync/push", content=b"x", headers={"content-length": "9999999"})
    assert r.status_code == 413
