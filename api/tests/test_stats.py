import base64
import hashlib
import re
from datetime import datetime, timezone

from . import helpers as h


async def stats(client):
    r = await client.get("/v1/stats")
    assert r.status_code == 200, r.text
    return r.json()


async def test_stats_requires_the_token(anon, app):
    assert (await anon.get("/v1/stats")).status_code == 401
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer nope"}) as bad:
        assert (await bad.get("/v1/stats")).status_code == 401


async def test_fresh_database_reports_zeros(client):
    d = await stats(client)
    assert d["api_calls"] == {"today": 0, "total": 0} and d["mcp_calls"] == {"today": 0, "total": 0}
    assert d["records"]["total"] == 0 and d["devices"] == 0 and d["records"]["snapshots"] == 0
    assert d["last_sync"] == {"at": None, "kind": None, "last_push": None, "last_pull": None, "last_snapshot": None}
    assert d["counting_since"] is None


async def test_counts_records_devices_and_last_sync(client):
    p = h.plan()
    deleted = h.expense(p["id"])
    await h.push(client, {"month_plans": [p], "expenses": [h.expense(p["id"]), deleted]}, [h.today_snapshot()], device="phone-1")
    tomb = {"id": deleted["id"], "updated_at": "2026-10-28T10:00:00Z", "device_id": "phone-1", "deleted_at": "2026-10-28T10:00:00Z"}
    await h.push(client, {"expenses": [tomb]}, device="phone-1")

    d = await stats(client)
    assert d["records"]["by_table"]["expenses"] == 1  # the tombstone is not a record
    assert d["records"]["by_table"]["month_plans"] == 1 and d["records"]["total"] == 2
    assert d["records"]["snapshots"] == 1 and d["devices"] == 1
    assert d["last_sync"]["kind"] == "push" and d["last_sync"]["last_push"] == d["last_sync"]["at"]
    assert d["last_sync"]["last_pull"] is None
    pushed_at = datetime.fromisoformat(d["last_sync"]["at"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - pushed_at).total_seconds()) < 30

    await h.pull(client, "czE6MA")
    d = await stats(client)
    assert d["last_sync"]["kind"] == "pull" and d["last_sync"]["last_pull"] is not None
    assert d["last_sync"]["last_push"] is not None
    assert (await client.get("/v1/sync/snapshot")).status_code == 200
    assert (await stats(client))["last_sync"]["last_snapshot"] is not None


async def test_api_calls_count_authenticated_calls_but_not_the_stats_poll(client, anon):
    await anon.get("/healthz")
    await anon.get("/v1/mcp")  # rejected: not counted
    await h.push(client, {})
    await h.pull(client, "czE6MA")
    for _ in range(3):
        await stats(client)  # polling must not inflate the number it reports
    d = await stats(client)
    assert d["api_calls"] == {"today": 2, "total": 2}
    assert d["counting_since"] == datetime.now(client._transport.app.state.settings.timezone).date().isoformat()


async def test_mcp_calls_count_tool_calls_and_mcp_is_not_a_device(client):
    p = h.plan()
    await h.push(client, {"month_plans": [p]}, device="phone-1")
    await h.rpc(client, "tools/list")  # a request, not a tool call
    await h.call_tool(client, "get_today", {"date": "2026-10-27"})
    await h.call_tool(client, "add_expense", {"amount": 500, "tag": "food", "date": "2026-10-27"})
    await h.call_tool(client, "add_expense", {"amount": 0, "tag": "food", "date": "2026-10-27"})  # failed calls count too
    d = await stats(client)
    assert d["mcp_calls"] == {"today": 3, "total": 3}
    assert d["api_calls"]["total"] == 1 + 1 + 3  # push + tools/list + three tools/call requests
    assert d["devices"] == 1  # the 'mcp' writer is not a synced device
    assert d["records"]["by_table"]["expenses"] == 1


async def test_counters_survive_a_restart(app, client):
    await h.push(client, {})
    await h.call_tool(client, "get_today", {})
    before = await stats(client)
    await app.state.usage.flush(app.state.pool)
    app.state.usage.reset()  # what a restart does to the in-memory part
    after = await stats(client)
    assert after["api_calls"] == before["api_calls"] and after["mcp_calls"] == before["mcp_calls"]
    assert after["last_sync"]["last_push"] == before["last_sync"]["last_push"]
    # Flushing twice adds nothing.
    await app.state.usage.flush(app.state.pool)
    assert (await stats(client))["api_calls"] == before["api_calls"]


async def test_erasure_clears_the_metrics_too(app, client):
    p = h.plan()
    await h.push(client, {"month_plans": [p]})
    await app.state.usage.flush(app.state.pool)
    assert (await client.delete("/v1/account", headers={"X-Confirm": "delete-all"})).status_code == 200
    await app.state.usage.flush(app.state.pool)
    d = await stats(client)
    assert d["api_calls"]["total"] == 0 and d["records"]["total"] == 0 and d["last_sync"]["at"] is None
    async with app.state.pool.connection() as conn:
        for table in ("usage_daily", "sync_status"):
            cur = await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
            assert (await cur.fetchone())["n"] == 0


async def test_home_page_wires_up_the_ledger(anon):
    r = await anon.get("/")
    for needle in ("Open the ledger", "/v1/stats", "API calls", "MCP calls", "Records", "Last synced"):
        assert needle in r.text
    # The CSP's script hash must match the inline script, or browsers would block it.
    script = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in r.headers["content-security-policy"]
    assert "connect-src 'self'" in r.headers["content-security-policy"]
    # Metrics are never baked into the public page.
    assert "api_calls" not in r.text.split("<script>")[0]
