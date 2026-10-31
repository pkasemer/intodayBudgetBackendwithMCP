from datetime import datetime

from . import helpers as h
from .helpers import call_tool, rpc

READ = {"get_today", "get_month_summary", "list_expenses", "list_bills", "list_income_events", "list_goals", "get_insights",
        "list_months", "list_loans", "list_debts", "get_category_limits"}
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


async def test_list_goals_totals_are_per_goal(client):
    p = h.plan()
    laptop, trip = h.goal(name="Laptop"), h.goal(name="Trip", target_amount=500000)
    await h.push(client, {
        "month_plans": [p],
        "savings_goals": [laptop, trip],
        "savings_allocations": [h.allocation(p["id"], laptop["id"], amount=70000)],
        "income_events": [
            h.income(p["id"], label="Gift", status="received", amount=50000, kind="extra",
                     destination="lockedSavings", savings_goal_id=laptop["id"]),
            h.income(p["id"], label="Refund", status="received", amount=20000, kind="extra",
                     destination="lockedSavings", savings_goal_id=trip["id"]),
            h.income(p["id"], label="Pending", status="expected", amount=999000, kind="extra",
                     destination="lockedSavings", savings_goal_id=trip["id"]),
            h.income(p["id"], label="Salary", status="received", amount=800000),  # spendable: never counted
        ],
    })
    goals = {g["name"]: g for g in (await call_tool(client, "list_goals"))["structuredContent"]["goals"]}
    assert (goals["Laptop"]["allocated_total"], goals["Laptop"]["income_to_savings_received"], goals["Laptop"]["locked_total"]) == (70000, 50000, 120000)
    assert (goals["Trip"]["allocated_total"], goals["Trip"]["income_to_savings_received"], goals["Trip"]["locked_total"]) == (0, 20000, 20000)


async def test_add_expense_needs_a_plan_for_that_calendar_month(client):
    await seed(client)  # October plan only
    assert (await call_tool(client, "add_expense", {"amount": 100, "tag": "food", "date": "2026-10-31"}))["isError"] is False
    assert (await call_tool(client, "add_expense", {"amount": 100, "tag": "food", "date": "2026-11-01"}))["isError"] is True


# ---- v0.4 read tools -----------------------------------------------------------------

async def test_list_months_newest_first(client):
    assert (await call_tool(client, "list_months"))["structuredContent"]["months"] == []
    await h.push(client, {"month_plans": [h.plan(2026, 9), h.plan(2026, 11, plan_state="closed"), h.plan(2026, 10)]})
    months = (await call_tool(client, "list_months"))["structuredContent"]["months"]
    assert [(m["year"], m["month"]) for m in months] == [(2026, 11), (2026, 10), (2026, 9)]
    assert months[0]["plan_state"] == "closed"


async def test_list_loans_remaining_status_and_late_flag(client):
    paid_off, written_off = h.loan(person="Zed", due_on=None), h.loan(person="Wes", written_off=True, due_on="2026-01-09")
    late, future = h.loan(person="Sam", amount=100000, due_on="2026-01-05"), h.loan(person="Ann", due_on="2099-01-01")
    done = h.loan(person="Kim", repaid_on="2026-11-20", due_on="2026-01-02")
    await h.push(client, {
        "loans": [paid_off, written_off, late, future, done],
        "loan_repayments": [h.repayment(late["id"], amount=40000), h.repayment(late["id"], amount=10000, date="2026-11-10"),
                            h.repayment(future["id"], amount=1000, deleted_at="2026-11-11T00:00:00Z")],
    })
    out = (await call_tool(client, "list_loans"))["structuredContent"]
    by_person = {l["person"]: l for l in out["loans"]}
    assert [l["person"] for l in out["loans"]] == ["Kim", "Sam", "Wes", "Ann", "Zed"]  # due date order, undated last
    assert by_person["Sam"]["repaid_total"] == 50000 and by_person["Sam"]["remaining"] == 50000
    assert by_person["Sam"]["late"] is True and by_person["Sam"]["status"] == "open"
    assert by_person["Ann"]["remaining"] == 100000 and by_person["Ann"]["late"] is False  # deleted repayment ignored
    assert by_person["Kim"]["status"] == "repaid" and by_person["Kim"]["late"] is False
    assert by_person["Wes"]["status"] == "written_off" and by_person["Wes"]["late"] is False
    assert out["owed_total"] == 50000 + 100000 + 100000 and out["late_count"] == 1
    only = (await call_tool(client, "list_loans", {"open_only": True}))["structuredContent"]
    assert {l["person"] for l in only["loans"]} == {"Sam", "Ann", "Zed"}
    assert (await call_tool(client, "list_loans", {"open_only": "yes"}))["isError"] is True


async def test_list_debts_balance_and_next_unpaid_bill(client):
    p10, p11 = h.plan(2026, 10), h.plan(2026, 11)
    bank, mum, done = h.debt(name="Stanbic", amount=1000000), h.debt(name="Mum", kind="person", amount=200000), h.debt(name="Old", amount=50000, monthly_payment=0)
    await h.push(client, {
        "debts": [bank, mum, done], "month_plans": [p10, p11],
        "bills": [
            h.bill(p10["id"], name="Stanbic Oct", amount=150000, is_paid=True, debt_id=bank["id"]),
            h.bill(p11["id"], name="Stanbic Nov", amount=150000, due_day=12, debt_id=bank["id"]),
            h.bill(p11["id"], name="Stanbic Nov late fee", amount=5000, due_day=20, debt_id=bank["id"]),
            h.bill(p11["id"], name="Skipped", amount=9999, is_paid=True, is_included=False, debt_id=bank["id"]),
        ],
        "debt_payments": [h.debt_payment(bank["id"], p10["id"], amount=50000), h.debt_payment(done["id"], p10["id"], amount=70000)],
    })
    out = (await call_tool(client, "list_debts"))["structuredContent"]
    by_name = {d["name"]: d for d in out["debts"]}
    assert [d["name"] for d in out["debts"]] == ["Mum", "Old", "Stanbic"]
    assert by_name["Stanbic"]["paid_total"] == 200000 and by_name["Stanbic"]["balance"] == 800000
    nxt = by_name["Stanbic"]["next_unpaid_bill"]
    assert (nxt["name"], nxt["amount"], nxt["due_day"], nxt["plan"]) == ("Stanbic Nov", 150000, 12, {"year": 2026, "month": 11})
    assert by_name["Mum"]["balance"] == 200000 and by_name["Mum"]["next_unpaid_bill"] is None
    assert by_name["Old"]["balance"] == 0  # overpaid is floored, not negative
    assert out["balance_total"] == 800000 + 200000
    only = (await call_tool(client, "list_debts", {"open_only": True}))["structuredContent"]
    assert [d["name"] for d in only["debts"]] == ["Mum", "Stanbic"] and only["balance_total"] == 1000000


async def test_get_category_limits_spend_against_each_limit(client):
    p = h.plan(2026, 10)
    await h.push(client, {
        "month_plans": [p],
        "category_limits": [h.limit(tag="food", amount=300000), h.limit(tag="fun", amount=10000),
                            h.limit(tag="shopping", amount=50000, deleted_at="2026-10-27T11:00:00Z")],
        "expenses": [h.expense(p["id"], amount=310000), h.expense(p["id"], amount=4000, tag="fun"),
                     h.expense(p["id"], amount=900, tag="other"), h.expense(p["id"], amount=70000, tag="shopping", deleted_at="2026-10-27T11:00:00Z")],
    })
    out = (await call_tool(client, "get_category_limits", {"year": 2026, "month": 10}))["structuredContent"]
    assert out["plan"] == {"year": 2026, "month": 10}
    rows = {r["tag"]: r for r in out["limits"]}
    assert list(rows) == ["food", "transport", "dataAirtime", "fun", "shopping", "other"]
    assert rows["food"] == {"tag": "food", "limit": 300000, "spent": 310000, "remaining": -10000, "over": True}
    assert rows["fun"]["remaining"] == 6000 and rows["fun"]["over"] is False
    assert rows["other"] == {"tag": "other", "limit": None, "spent": 900, "remaining": None, "over": False}
    assert rows["shopping"]["limit"] is None and rows["shopping"]["spent"] == 0  # deleted limit and expense ignored
    empty = (await call_tool(client, "get_category_limits", {"year": 2030, "month": 1}))["structuredContent"]
    assert empty["plan"] is None and empty["limits"][0]["limit"] == 300000 and empty["limits"][0]["spent"] == 0
    assert (await call_tool(client, "get_category_limits", {"year": 2026}))["isError"] is True


async def test_get_category_limits_defaults_to_the_current_month(app, client):
    today = datetime.now(app.state.settings.timezone).date()
    p = h.plan(today.year, today.month)
    await h.push(client, {"month_plans": [p], "category_limits": [h.limit(tag="food", amount=1000)],
                          "expenses": [h.expense(p["id"], amount=400, date=today.isoformat())]})
    out = (await call_tool(client, "get_category_limits"))["structuredContent"]
    assert out["plan"] == {"year": today.year, "month": today.month}
    assert out["limits"][0]["spent"] == 400 and out["limits"][0]["remaining"] == 600
