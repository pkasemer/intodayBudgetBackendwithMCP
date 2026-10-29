"""GET /v1/dashboard: the month in full, for the dashboard view."""
from . import helpers as h
from .test_export import seed


async def dash(client, **params):
    r = await client.get("/v1/dashboard", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_dashboard_needs_the_token_and_valid_params(anon, client):
    assert (await anon.get("/v1/dashboard")).status_code == 401
    assert (await client.get("/v1/dashboard", params={"year": 2026})).status_code == 422
    assert (await client.get("/v1/dashboard", params={"year": 2026, "month": 13})).status_code == 422
    assert (await client.get("/v1/dashboard", params={"year": 2026, "month": 3})).status_code == 404


async def test_empty_database_gives_an_empty_dashboard(client):
    d = await dash(client)
    assert d["months"] == [] and d["month"] is None and d["plan"] is None and d["totals"] is None
    assert d["goals"] == [] and d["trend"] == [] and d["today"] is None


async def test_month_in_full(client):
    await seed(client)
    d = await dash(client)  # latest plan is the empty November
    assert d["months"] == ["2026-11", "2026-10"] and d["month"] == "2026-11" and d["expenses"] == []
    assert len(d["days"]) == 30 and d["totals"]["spent"] == 0

    d = await dash(client, year=2026, month=10)
    assert d["month"] == "2026-10" and len(d["days"]) == 31
    assert d["plan"]["plan_state"] == "active" and d["plan"]["closed_days"] == [3, 9]
    t = d["totals"]
    assert (t["income_received"], t["income_expected"], t["spent"], t["bills_paid"], t["bills_unpaid"]) == (900000, 50000, 20400, 150000, 300000)
    assert [e["date"] for e in d["expenses"]] == ["2026-10-21", "2026-10-20", "2026-10-05"]  # newest first
    assert {x["date"]: x["spent"] for x in d["days"] if x["spent"]} == {"2026-10-05": 4500, "2026-10-20": 15000, "2026-10-21": 900}
    assert sum(x["spent"] for x in d["days"]) == t["spent"]
    assert [b["name"] for b in d["bills"]] == ["Stanbic", "Gym", "Rent"]  # by due day, then name
    assert [i["label"] for i in d["income"]] == ["-Salary", "Gift"]  # raw labels: the browser renders text, never HTML


async def test_categories_carry_their_limits(client):
    await seed(client)
    d = await dash(client, year=2026, month=10)
    tags = {x["tag"]: x for x in d["by_tag"]}
    assert tags["food"] == {"tag": "food", "spent": 15000, "limit": 300000}  # seed() sets a food limit
    assert tags["transport"]["limit"] is None and tags["shopping"]["spent"] == 0
    assert [x["tag"] for x in d["by_tag"]][:3] == ["food", "transport", "fun"]  # biggest first


async def test_goals_loans_and_debts(client):
    await seed(client)
    old = h.loan(None, person="Ann", amount=50000, due_on="2026-01-01")
    await h.push(client, {"loans": [old]})
    d = await dash(client, year=2026, month=10)
    laptop = d["goals"][0]
    assert (laptop["name"], laptop["allocated_total"], laptop["locked_total"]) == ("Laptop", 70000, 70000)
    loans = {l["person"]: l for l in d["loans"]}
    assert loans["Sam"]["remaining"] == 60000 and loans["Sam"]["late"] is False
    assert loans["Ann"]["remaining"] == 50000 and loans["Ann"]["late"] is True
    assert [x["balance"] for x in d["debts"]] == [800000]


async def test_trend_lists_recent_months_oldest_first(client):
    p9, p10 = h.plan(month=9), h.plan(month=10)
    await h.push(client, {"month_plans": [p9, p10], "expenses": [h.expense(p9["id"], date="2026-09-02", amount=700), h.expense(p10["id"], amount=900)],
                          "income_events": [h.income(p10["id"], status="received", amount=5000)]})
    d = await dash(client, year=2026, month=10)
    assert d["trend"] == [{"month": "2026-09", "income": 0, "spent": 700, "bills_paid": 0},
                          {"month": "2026-10", "income": 5000, "spent": 900, "bills_paid": 0}]
    assert [x["month"] for x in (await dash(client, year=2026, month=9))["trend"]] == ["2026-09"]


async def test_app_snapshots_pass_through_untouched(client):
    p = h.plan()
    snap = h.today_snapshot(safe_today=14900)
    await h.push(client, {"month_plans": [p]}, [snap])
    d = await dash(client, year=2026, month=10)
    assert d["today"]["payload"]["safe_today"] == 14900 and d["today"]["updated_at"].endswith("Z")
    assert d["insights"] is None


async def test_dashboard_polls_are_not_counted_as_api_calls(client):
    for _ in range(3):
        await dash(client)
    s = (await client.get("/v1/stats")).json()
    assert s["api_calls"]["total"] == 0 and s["responses"]["today"]["ok"] == 0


async def test_home_page_has_landing_and_dashboard_views(anon):
    r = await anon.get("/")
    for needle in ('id="landing"', 'id="dash"', "/v1/dashboard", "Overview", "Spending", "Money", "Reports", "System", "Open the ledger"):
        assert needle in r.text
    assert "UGX" not in r.text.split("<script>")[0].split("<body>")[1]  # no figures baked into the markup
