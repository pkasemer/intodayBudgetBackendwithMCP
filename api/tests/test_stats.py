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
        for table in ("usage_daily", "sync_status", "usage_status"):
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


# ---- observability ------------------------------------------------------------------

async def test_activity_feed_records_push_pull_snapshot_and_mcp(client):
    p = h.plan()
    await h.push(client, {"month_plans": [p], "expenses": [h.expense(p["id"], amount=0)]}, device="phone-1")  # one rejected
    await h.pull(client, "czE6MA")
    await client.get("/v1/sync/snapshot")
    await h.call_tool(client, "get_today", {})
    await h.call_tool(client, "add_expense", {"amount": 0, "tag": "food", "date": "2026-10-27"})  # refused

    d = await stats(client)
    feed = d["recent"]
    kinds = [e["kind"] for e in feed]
    assert kinds[::-1] == ["push", "pull", "snapshot", "mcp", "mcp"]  # newest first
    push = feed[-1]
    assert push["device_id"] == "phone-1" and push["detail"]["applied"] == 1 and push["detail"]["rejected"] == 1
    assert push["detail"]["tables"] == {"month_plans": 1} and push["ms"] is not None
    assert feed[-2]["detail"]["records"] == 1 and feed[-3]["detail"]["records"] == 1
    assert [e["detail"]["ok"] for e in feed[:2]][::-1] == [True, False]
    assert feed[0]["detail"]["tool"] == "add_expense" and feed[0]["detail"]["write"] is True
    assert d["latency"]["push"]["n"] == 1 and d["latency"]["mcp"]["n"] == 2


async def test_rejections_are_listed_with_field_reasons_never_values(client):
    p = h.plan()
    bad = h.expense(p["id"], amount=123456789)
    await h.push(client, {"month_plans": [p], "expenses": [bad]}, device="phone-1")
    d = await stats(client)
    assert [(r["table"], r["id"]) for r in d["rejections"]] == [("expenses", bad["id"])]
    assert "amount" in d["rejections"][0]["reason"] and "123456789" not in str(d)
    assert any("rejected" in a["text"] for a in d["alerts"])


async def test_replayed_push_is_marked_and_survives_flush(app, client):
    body = {"month_plans": [h.plan()]}
    await h.push(client, body, key="same-key")
    await h.push(client, body, key="same-key")
    await app.state.usage.flush(app.state.pool)  # now the feed comes from Postgres
    d = await stats(client)
    assert [e["detail"]["replay"] for e in d["recent"]] == [True, False]
    assert (await stats(client))["recent"] == d["recent"]  # flushing twice adds nothing


async def test_responses_count_refusals_and_errors_but_not_the_poll_or_home_page(client, anon):
    await anon.get("/v1/sync/pull")  # 401
    await anon.get("/")  # not counted
    await anon.get("/healthz")  # not counted
    await client.get("/v1/sync/pull")  # 422: no cursor
    await h.push(client, {})  # ok
    for _ in range(3):
        await stats(client)
    d = await stats(client)
    assert d["responses"]["today"] == {"ok": 1, "unauthorized": 1, "rate_limited": 0, "client_error": 1, "server_error": 0}
    kinds = sorted(e["kind"] for e in d["recent"])
    assert kinds == ["denied", "failed", "push"]
    assert all("path" in e["detail"] for e in d["recent"] if e["kind"] != "push")


async def test_failed_auth_burst_is_capped_in_the_feed(client, anon):
    for _ in range(40):
        await anon.get("/v1/sync/pull")
    d = await stats(client)
    denied = [e for e in d["recent"] if e["kind"] == "denied"]
    assert 0 < len(denied) <= 25
    assert d["responses"]["today"]["unauthorized"] + d["responses"]["today"]["rate_limited"] == 40


async def test_days_health_devices_and_tables(client):
    p = h.plan()
    await h.push(client, {"month_plans": [p]}, device="phone-1")
    await h.call_tool(client, "add_expense", {"amount": 500, "tag": "food", "date": "2026-10-27"})
    d = await stats(client)
    assert len(d["days"]) == 14 and d["days"][-1]["api"] == 2 and d["days"][-1]["mcp"] == 1
    assert d["health"]["database"] == "up" and d["health"]["redis"] == "up"
    assert d["health"]["schema_version"] == 4 and d["health"]["migration"].startswith("004")
    assert d["health"]["uptime_seconds"] >= 0
    writers = {w["device_id"]: w for w in d["devices_seen"]}
    assert writers["phone-1"]["is_mcp"] is False and writers["phone-1"]["last_push"] is not None
    assert writers["mcp"]["is_mcp"] is True and d["devices"] == 1
    assert set(d["records"]["removed_by_table"]) == set(d["records"]["by_table"]) and len(d["records"]["by_table"]) == 12
    assert d["alerts"] == []


async def test_redis_down_raises_an_alert(app_no_redis):
    import httpx
    from .conftest import TOKEN

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_no_redis), base_url="http://t",
                                 headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        d = await stats(c)
    assert d["health"]["redis"] == "down" and any("Redis" in a["text"] for a in d["alerts"])


async def test_erasure_leaves_a_single_erased_event(app, client):
    await h.push(client, {"month_plans": [h.plan()]})
    await client.delete("/v1/account", headers={"X-Confirm": "delete-all"})
    d = await stats(client)
    assert [e["kind"] for e in d["recent"]] == ["account"] and d["rejections"] == []
