from datetime import datetime

from . import helpers as h
from .helpers import call_tool, rpc

READ = {"get_today", "get_month_summary", "list_expenses", "list_bills", "list_income_events", "list_goals", "get_insights"}
WRITE = {"add_expense", "mark_bill_paid", "mark_income_received"}


async def seed(client):
    p = h.plan()
    g = h.goal()
    b = h.bill(p["id"])
    inc = h.income(p["id"])
    await h.push(client, {
        "month_plans": [p], "savings_goals": [g], "bills": [b, h.bill(p["id"], name="Data", is_paid=True, due_day=3)],
        "expenses": [h.expense(p["id"]), h.expense(p["id"], amount=2000, tag="transport", date="2026-10-26")],
        "income_events": [inc, h.income(p["id"], label="Gift", status="received", amount=50000, kind="extra",
                                         destination="lockedSavings", savings_goal_id=g["id"])],
        "savings_allocations": [h.allocation(p["id"], g["id"], amount=70000)],
    })
    return p, g, b, inc


async def test_initialize_ping_and_notifications(client):
    r = (await rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})).json()
    assert r["result"]["protocolVersion"] == "2025-06-18"
    assert r["result"]["capabilities"] == {"tools": {"listChanged": False}}
    old = (await rpc(client, "initialize", {"protocolVersion": "1999-01-01"})).json()
    assert old["result"]["protocolVersion"] == "2025-06-18"
    assert (await rpc(client, "ping")).json()["result"] == {}
    note = await client.post("/v1/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202 and note.content == b""
    assert (await rpc(client, "resources/list")).json()["error"]["code"] == -32601


async def test_tools_list_has_domain_tools_and_no_sql(client):
    tools = (await rpc(client, "tools/list")).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == READ | WRITE
    assert not any("sql" in n or "schema" in n or "query" in n for n in names)
    by_name = {t["name"]: t for t in tools}
    assert all(by_name[n]["annotations"]["readOnlyHint"] for n in READ)
    assert not any(by_name[n]["annotations"]["readOnlyHint"] for n in WRITE)
    assert (await client.get("/v1/mcp")).json()["tools"] == tools


async def test_bad_json_and_bad_requests(client):
    r = await client.post("/v1/mcp", content=b"{nope")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    r = await client.post("/v1/mcp", json={"id": 1, "method": "ping"})
    assert r.json()["error"]["code"] == -32600
    r = await rpc(client, "tools/call", {"name": "execute_sql", "arguments": {"query": "select 1"}})
    assert r.json()["error"]["code"] == -32602


async def test_get_today_returns_snapshot_unchanged(client):
    await seed(client)
    snapshot = h.today_snapshot()
    await h.push(client, {}, [snapshot])
    result = await call_tool(client, "get_today", {"date": "2026-10-27"})
    assert result["isError"] is False
    assert result["structuredContent"]["snapshot"] == snapshot["payload"]
    assert result["structuredContent"]["as_of"] == "2026-10-27T07:30:00Z"


async def test_get_today_without_snapshot_falls_back_to_counts(client):
    await seed(client)
    out = (await call_tool(client, "get_today", {"date": "2026-10-27"}))["structuredContent"]
    assert out["snapshot"] is None
    assert out["note"] == "No snapshot yet — the app has not synced."
    assert out["expenses_today"] == 1 and out["bills_due"] == 0
    assert (await call_tool(client, "get_today", {"date": "2026-10-28"}))["structuredContent"]["bills_due"] == 1
    empty = (await call_tool(client, "get_today", {"date": "2031-01-01"}))["structuredContent"]
    assert empty["snapshot"] is None and empty["expenses_today"] == 0


async def test_month_summary_sums_stored_rows(client):
    await seed(client)
    out = (await call_tool(client, "get_month_summary", {"year": 2026, "month": 10}))["structuredContent"]
    assert out["counts"] == {"bills": 2, "expenses": 2, "income_events": 2, "savings_allocations": 1}
    totals = out["totals"]
    assert totals["total_spent"] == 17000
    assert totals["total_income_received"] == 50000 and totals["total_income_expected"] == 900000
    assert totals["bills_paid"] == 1 and totals["bills_unpaid"] == 1
    assert out["plan"]["plan_state"] == "active"
    missing = await call_tool(client, "get_month_summary", {"year": 2030, "month": 1})
    assert missing["isError"] is True


async def test_list_tools_filter_and_order(client):
    await seed(client)
    exp = (await call_tool(client, "list_expenses", {"year": 2026, "month": 10}))["structuredContent"]["expenses"]
    assert [e["date"] for e in exp] == ["2026-10-27", "2026-10-26"]
    only = (await call_tool(client, "list_expenses", {"year": 2026, "month": 10, "tag": "transport", "limit": 1}))
    assert [e["amount"] for e in only["structuredContent"]["expenses"]] == [2000]
    bills = (await call_tool(client, "list_bills", {"year": 2026, "month": 10, "unpaid_only": True}))["structuredContent"]["bills"]
    assert [b["name"] for b in bills] == ["Rent"]
    inc = (await call_tool(client, "list_income_events", {"year": 2026, "month": 10}))["structuredContent"]["income_events"]
    assert {i["label"] for i in inc} == {"Salary", "Gift"}
    none = (await call_tool(client, "list_expenses", {"year": 2030, "month": 1}))["structuredContent"]
    assert none["plan"] is None and none["expenses"] == []
    assert (await call_tool(client, "list_expenses", {"year": 2026, "month": 10, "limit": 500}))["isError"] is True


async def test_list_goals_sums_allocations_and_received_income(client):
    await seed(client)
    goals = (await call_tool(client, "list_goals"))["structuredContent"]["goals"]
    assert len(goals) == 1
    assert goals[0]["allocated_total"] == 70000 and goals[0]["income_to_savings_received"] == 50000
    assert goals[0]["locked_total"] == 120000


async def test_get_insights_snapshot_and_fallback(client):
    await seed(client)
    fallback = (await call_tool(client, "get_insights", {"year": 2026, "month": 10}))["structuredContent"]
    assert fallback["snapshot"] is None and fallback["tag_totals"] == {"food": 15000, "transport": 2000}
    payload = {"snapshot_version": 1, "tag_totals": {"food": 15000}, "streak_current": 2}
    await h.push(client, {}, [{"kind": "insights", "plan_year": 2026, "plan_month": 10, "payload": payload, "snapshot_version": 1}])
    got = (await call_tool(client, "get_insights", {"year": 2026, "month": 10}))["structuredContent"]
    assert got["snapshot"] == payload


async def test_add_expense_writes_as_mcp(client):
    p, *_ = await seed(client)
    out = await call_tool(client, "add_expense", {"amount": 8000, "tag": "fun", "date": "2026-10-27", "note": "cinema"})
    assert out["isError"] is False
    data = out["structuredContent"]
    assert data["device_sync_pending"] is True
    assert data["expense"]["device_id"] == "mcp" and data["expense"]["plan_id"] == p["id"]
    assert data["expense"]["amount"] == 8000
    snap = (await client.get("/v1/sync/snapshot")).json()
    assert any(e["id"] == data["expense"]["id"] for e in snap["records"]["expenses"])


async def test_add_expense_invalid_input_writes_nothing(client):
    await seed(client)
    before = len((await client.get("/v1/sync/snapshot")).json()["records"]["expenses"])
    for args in (
        {"amount": 0, "tag": "food", "date": "2026-10-27"},
        {"amount": -1, "tag": "food", "date": "2026-10-27"},
        {"amount": 12.5, "tag": "food", "date": "2026-10-27"},
        {"amount": 10_000_000, "tag": "food", "date": "2026-10-27"},
        {"amount": 100, "tag": "gambling", "date": "2026-10-27"},
        {"amount": 100, "tag": "food", "date": "2026-02-30"},
        {"amount": 100, "tag": "food", "date": "27/10/2026"},
        {"amount": 100, "tag": "food", "date": "2031-01-01"},
        {"amount": 100, "tag": "food", "date": "2026-10-27", "note": "x" * 201},
        {"tag": "food", "date": "2026-10-27"},
    ):
        result = await call_tool(client, "add_expense", args)
        assert result["isError"] is True, args
        assert "structuredContent" not in result
    after = len((await client.get("/v1/sync/snapshot")).json()["records"]["expenses"])
    assert after == before


async def test_mark_bill_paid(client):
    _, _, b, _ = await seed(client)
    out = await call_tool(client, "mark_bill_paid", {"bill_id": b["id"]})
    assert out["isError"] is False
    bill = out["structuredContent"]["bill"]
    assert bill["is_paid"] is True and bill["device_id"] == "mcp" and out["structuredContent"]["device_sync_pending"] is True
    assert datetime.fromisoformat(bill["updated_at"]) > datetime.fromisoformat(b["updated_at"])
    again = await call_tool(client, "mark_bill_paid", {"bill_id": b["id"]})
    assert again["isError"] is True
    assert (await call_tool(client, "mark_bill_paid", {"bill_id": h.new_id()}))["isError"] is True
    assert (await call_tool(client, "mark_bill_paid", {"bill_id": "nope"}))["isError"] is True


async def test_mark_income_received(client):
    _, _, _, inc = await seed(client)
    out = await call_tool(client, "mark_income_received", {"income_event_id": inc["id"]})
    assert out["structuredContent"]["income_event"]["status"] == "received"
    assert out["structuredContent"]["income_event"]["device_id"] == "mcp"
    assert (await call_tool(client, "mark_income_received", {"income_event_id": inc["id"]}))["isError"] is True


async def test_mcp_write_beats_a_phone_with_a_fast_clock(client):
    p = h.plan()
    b = h.bill(p["id"], updated_at="2099-01-01T00:00:00Z")
    await h.push(client, {"month_plans": [p], "bills": [b]})
    out = await call_tool(client, "mark_bill_paid", {"bill_id": b["id"]})
    stamp = out["structuredContent"]["bill"]["updated_at"]
    assert datetime.fromisoformat(stamp) > datetime.fromisoformat("2099-01-01T00:00:00Z")


async def test_write_logs_carry_ids_not_amounts(app, client):
    import logging

    from app.logs import _JsonFormatter

    p, *_ = await seed(client)
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(_JsonFormatter().format(record))

    logger = logging.getLogger("intoday")
    handler = Capture()
    logger.addHandler(handler)
    try:
        await call_tool(client, "add_expense", {"amount": 123457, "tag": "fun", "date": "2026-10-27", "note": "secret-note"})
    finally:
        logger.removeHandler(handler)
    joined = "\n".join(lines)
    assert "mcp_write" in joined and "123457" not in joined and "secret-note" not in joined
