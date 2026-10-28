"""Backend v0.4 sync additions: capabilities, new tables and columns, keep-on-missing, FK rules."""
import logging
from datetime import datetime, timedelta, timezone

from app.purge import purge

from . import helpers as h

SPEC_TABLES = ["savings_goals", "bill_templates", "debts", "month_plans", "bills", "expenses", "income_events",
               "savings_allocations", "loans", "loan_repayments", "debt_payments", "category_limits"]
START = "czE6MA"  # s1:0


async def stored(app, query, *params):
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(query, params)
        return await cur.fetchall()


def v04_plan(year=2026, month=11, **kw):
    return h.plan(year, month, **{
        "closed_days": [9, 3, 9], "rollover_amount": 25000, "rollover_carried": True, "summary_shown": True,
        "buffer_released_amount": 10000, "buffer_released_on": "2026-11-30",
        "created_at": "2026-10-30T18:02:11.000Z", **kw})


# ---- 15. capabilities ----------------------------------------------------------------

async def test_capabilities(client, anon):
    body = (await client.get("/v1/sync/capabilities")).json()
    assert body == {"schema_version": 4, "tables": SPEC_TABLES}
    assert (await anon.get("/v1/sync/capabilities")).status_code == 401


# ---- 16. round trip ------------------------------------------------------------------

async def test_round_trip_of_every_new_table_and_column(client):
    g = h.goal()
    d = h.debt()
    p = v04_plan(rollover_goal_id=g["id"], reserved_debt_ids=[d["id"]])
    loan = h.loan(p["id"])
    records = {
        "savings_goals": [g], "debts": [d], "month_plans": [p],
        "bills": [h.bill(p["id"], name="Stanbic", amount=150000, due_day=12, debt_id=d["id"],
                         is_included=False, exclude_reason="prepaid")],
        "income_events": [h.income(p["id"], is_recurring=True, is_rollover=True)],
        "loans": [loan],
        "loan_repayments": [h.repayment(loan["id"], landed_as_income=True)],
        "debt_payments": [h.debt_payment(d["id"], p["id"])],
        "category_limits": [h.limit()],
    }
    r = await h.push(client, records)
    assert r.json()["rejected"] == [], r.text
    assert all(r.json()["applied"][t] == 1 for t in records)

    for page in ((await h.pull(client, START)).json(), (await client.get("/v1/sync/snapshot")).json()):
        got = {t: {row["id"]: row for row in rows} for t, rows in page["records"].items()}
        for table, rows in records.items():
            for sent in rows:
                row = got[table][sent["id"]]
                for key, value in sent.items():
                    if key == "closed_days":
                        value = [3, 9]
                    elif key in ("created_at", "updated_at"):
                        assert datetime.fromisoformat(row[key]) == datetime.fromisoformat(value), (table, key)
                        continue
                    assert row[key] == value, (table, key)


async def test_pull_returns_tombstones_of_new_tables(client):
    d, lim = h.debt(), h.limit()
    cursor = (await h.push(client, {"debts": [d], "category_limits": [lim]})).json()["cursor"]
    gone = {"updated_at": "2026-10-27T11:00:00Z", "deleted_at": "2026-10-27T11:00:00Z"}
    await h.push(client, {"debts": [h.rec(id=d["id"], **gone)], "category_limits": [h.rec(id=lim["id"], **gone)]})
    page = (await h.pull(client, cursor)).json()
    assert {(x["table"], x["id"]) for x in page["deleted"]} == {("debts", d["id"]), ("category_limits", lim["id"])}
    snap = (await client.get("/v1/sync/snapshot")).json()["records"]
    assert snap["debts"] == [] and snap["category_limits"] == []


# ---- 17. missing fields keep stored values -------------------------------------------

async def test_missing_new_columns_keep_stored_values(client):
    d = h.debt()
    p = v04_plan()
    b = h.bill(p["id"], name="Stanbic", debt_id=d["id"], is_included=False, exclude_reason="prepaid")
    inc = h.income(p["id"], is_recurring=True)
    await h.push(client, {"debts": [d], "month_plans": [p], "bills": [b], "income_events": [inc]})

    # An old build re-sends the rows newer, without any v0.4 key.
    newer = {"updated_at": "2026-10-27T11:00:00Z"}
    old_plan = h.rec(**{k: p[k] for k in ("id", "year", "month", "buffer_amount", "start_day", "plan_state")}, **newer)
    old_bill = h.rec(**{k: b[k] for k in ("id", "plan_id", "name", "amount", "due_day", "is_one_off")},
                     is_paid=True, **newer)
    old_inc = h.rec(**{k: inc[k] for k in ("id", "plan_id", "label", "amount", "date", "kind", "status", "destination")},
                    **newer)
    r = await h.push(client, {"month_plans": [old_plan], "bills": [old_bill], "income_events": [old_inc]})
    assert r.json()["rejected"] == [] and r.json()["applied"]["bills"] == 1

    snap = (await client.get("/v1/sync/snapshot")).json()["records"]
    bill, plan, income = snap["bills"][0], snap["month_plans"][0], snap["income_events"][0]
    assert bill["is_paid"] is True  # the v0.3 field did update
    assert (bill["debt_id"], bill["is_included"], bill["exclude_reason"]) == (d["id"], False, "prepaid")
    assert plan["closed_days"] == [3, 9] and plan["rollover_amount"] == 25000 and plan["created_at"].startswith("2026-10-30")
    assert plan["rollover_carried"] is True and plan["buffer_released_on"] == "2026-11-30"
    assert income["is_recurring"] is True

    # An explicit null clears a nullable column.
    cleared = dict(old_bill, debt_id=None, exclude_reason=None, updated_at="2026-10-27T12:00:00Z")
    await h.push(client, {"bills": [cleared]})
    bill = (await client.get("/v1/sync/snapshot")).json()["records"]["bills"][0]
    assert bill["debt_id"] is None and bill["exclude_reason"] is None and bill["is_included"] is False


async def test_missing_new_columns_take_defaults_on_insert(client):
    p = h.plan()
    inc = h.income(p["id"])
    await h.push(client, {"month_plans": [p], "bills": [h.bill(p["id"])], "income_events": [inc],
                          "loans": [h.rec(person="Sam", amount=5000, lent_on="2026-10-01")],
                          "debts": [h.rec(name="Mum", kind="person", amount=90000, started_on="2026-10-01",
                                          due_day=5, first_month="2026-10")]})
    snap = (await client.get("/v1/sync/snapshot")).json()["records"]
    plan, bill = snap["month_plans"][0], snap["bills"][0]
    assert (plan["closed_days"], plan["rollover_amount"], plan["rollover_carried"], plan["rollover_goal_id"]) == ([], 0, False, None)
    assert (plan["summary_shown"], plan["buffer_released_amount"], plan["buffer_released_on"]) == (False, 0, None)
    assert (plan["reserved_debt_ids"], plan["created_at"]) == ([], None)
    assert (bill["is_included"], bill["exclude_reason"], bill["debt_id"]) == (True, None, None)
    assert (snap["income_events"][0]["is_recurring"], snap["income_events"][0]["is_rollover"]) == (False, False)
    assert snap["loans"][0]["written_off"] is False and snap["loans"][0]["plan_id"] is None
    assert snap["debts"][0]["monthly_payment"] == 0


async def test_upgrade_push_keeps_an_mcp_write(client):
    """v0.3 data, an MCP mark_bill_paid, then the app's pull-first upgrade push: nothing is lost."""
    p = h.plan(month=10)
    b = h.bill(p["id"])
    await h.push(client, {"month_plans": [p], "bills": [b]})
    paid = await h.call_tool(client, "mark_bill_paid", {"bill_id": b["id"]})
    assert paid["isError"] is False

    pulled = (await h.pull(client, START)).json()["records"]
    bill = dict(pulled["bills"][0], updated_at="2026-10-28T09:00:00Z", device_id="phone-1")
    plan = dict(pulled["month_plans"][0], created_at="2026-10-01T08:00:00.000Z",
                closed_days=[1, 2], updated_at="2026-10-28T09:00:00Z", device_id="phone-1")
    r = await h.push(client, {"month_plans": [plan], "bills": [dict(bill, is_included=True, debt_id=None)]})
    assert r.json()["rejected"] == []
    snap = (await client.get("/v1/sync/snapshot")).json()["records"]
    assert snap["bills"][0]["is_paid"] is True
    assert snap["month_plans"][0]["created_at"].startswith("2026-10-01")


# ---- 18. foreign-key behaviour (physical deletes: the purge, or manual) -------------

async def test_deleting_a_plan_detaches_loans_and_cascades_debt_payments(app, client):
    p, d = h.plan(), h.debt()
    ln = h.loan(p["id"])
    dp = h.debt_payment(d["id"], p["id"])
    await h.push(client, {"debts": [d], "month_plans": [p], "loans": [ln], "loan_repayments": [h.repayment(ln["id"])],
                          "debt_payments": [dp]})
    async with app.state.pool.connection() as conn:
        await conn.execute("DELETE FROM month_plans WHERE id = %s", (p["id"],))
    assert (await stored(app, "SELECT plan_id FROM loans"))[0]["plan_id"] is None
    assert await stored(app, "SELECT 1 FROM debt_payments") == []
    assert len(await stored(app, "SELECT 1 FROM loan_repayments")) == 1  # the loan, and so its repayment, survives


async def test_deleting_a_loan_cascades_its_repayments(app, client):
    ln = h.loan()
    await h.push(client, {"loans": [ln], "loan_repayments": [h.repayment(ln["id"])]})
    async with app.state.pool.connection() as conn:
        await conn.execute("DELETE FROM loans WHERE id = %s", (ln["id"],))
    assert await stored(app, "SELECT 1 FROM loan_repayments") == []


async def test_deleting_a_debt_detaches_bills_and_payments(app, client):
    p, d = h.plan(), h.debt()
    await h.push(client, {"debts": [d], "month_plans": [p], "bills": [h.bill(p["id"], debt_id=d["id"])],
                          "debt_payments": [h.debt_payment(d["id"], p["id"])]})
    cursor = (await h.push(client, {})).json()["cursor"]
    async with app.state.pool.connection() as conn:
        await conn.execute("DELETE FROM debts WHERE id = %s", (d["id"],))
    assert (await stored(app, "SELECT debt_id FROM bills"))[0]["debt_id"] is None
    assert (await stored(app, "SELECT debt_id FROM debt_payments"))[0]["debt_id"] is None
    page = (await h.pull(client, cursor)).json()["records"]  # devices see the detached rows
    assert page["bills"][0]["debt_id"] is None and page["debt_payments"][0]["debt_id"] is None


async def test_deleting_a_goal_clears_rollover_goal(app, client):
    g = h.goal()
    p = v04_plan(rollover_goal_id=g["id"])
    await h.push(client, {"savings_goals": [g], "month_plans": [p]})
    async with app.state.pool.connection() as conn:
        await conn.execute("DELETE FROM savings_goals WHERE id = %s", (g["id"],))
    assert (await stored(app, "SELECT rollover_goal_id FROM month_plans"))[0]["rollover_goal_id"] is None


async def test_purge_covers_new_tables(app, client):
    p, d = h.plan(), h.debt()
    ln = h.loan(p["id"])
    await h.push(client, {"debts": [d], "month_plans": [p], "bills": [h.bill(p["id"], debt_id=d["id"])], "loans": [ln],
                          "loan_repayments": [h.repayment(ln["id"])], "category_limits": [h.limit()]})
    gone = {"updated_at": "2026-10-27T11:00:00Z", "deleted_at": "2026-10-27T11:00:00Z"}
    lim = (await client.get("/v1/sync/snapshot")).json()["records"]["category_limits"][0]
    await h.push(client, {"debts": [h.rec(id=d["id"], **gone)], "loans": [h.rec(id=ln["id"], **gone)],
                          "category_limits": [h.rec(id=lim["id"], **gone)]})
    counts = await purge(app.state.pool, now=datetime.now(timezone.utc) + timedelta(days=31))
    assert (counts["debts"], counts["loans"], counts["category_limits"]) == (1, 1, 1)
    assert await stored(app, "SELECT 1 FROM loan_repayments") == []
    assert (await stored(app, "SELECT debt_id FROM bills"))[0]["debt_id"] is None


# ---- 19. category limits -------------------------------------------------------------

async def test_one_live_limit_per_tag(client):
    first, second = h.limit(), h.limit(amount=500000, updated_at="2026-10-27T11:00:00Z")
    await h.push(client, {"category_limits": [first]})
    r = (await h.push(client, {"category_limits": [second]})).json()
    assert [x["id"] for x in r["rejected"]] == [second["id"]]
    live = (await client.get("/v1/sync/snapshot")).json()["records"]["category_limits"]
    assert [x["id"] for x in live] == [first["id"]]

    # Removing the limit is a tombstone; the tag is then free for a new row.
    await h.push(client, {"category_limits": [h.rec(id=first["id"], updated_at="2026-10-27T12:00:00Z",
                                                    deleted_at="2026-10-27T12:00:00Z")]})
    assert (await h.push(client, {"category_limits": [second]})).json()["rejected"] == []
    live = (await client.get("/v1/sync/snapshot")).json()["records"]["category_limits"]
    assert [x["id"] for x in live] == [second["id"]]


async def test_same_id_updates_the_limit_under_lww(client):
    lim = h.limit()
    await h.push(client, {"category_limits": [lim]})
    await h.push(client, {"category_limits": [dict(lim, amount=450000, updated_at="2026-10-27T11:00:00Z")]})
    await h.push(client, {"category_limits": [dict(lim, amount=1, updated_at="2026-10-27T09:00:00Z")]})
    live = (await client.get("/v1/sync/snapshot")).json()["records"]["category_limits"]
    assert [x["amount"] for x in live] == [450000]


# ---- 20. validation ------------------------------------------------------------------

async def reasons(client, records):
    r = (await h.push(client, records)).json()
    return {x["id"]: x["reason"] for x in r["rejected"]}, r


async def test_goal_with_no_target_is_accepted(client):
    g = h.goal(name="Savings", target_amount=0)
    p = h.plan()
    allocation = h.allocation(p["id"], g["id"])
    rejected, r = await reasons(client, {"savings_goals": [g], "month_plans": [p], "savings_allocations": [allocation]})
    assert rejected == {} and r["applied"]["savings_goals"] == 1 and r["applied"]["savings_allocations"] == 1
    rejected, _ = await reasons(client, {"savings_goals": [h.goal(target_amount=-1), h.goal(target_amount=10_000_000)]})
    assert len(rejected) == 2


async def test_debt_and_loan_amount_ranges(client):
    big_debt, big_loan = h.debt(amount=25_000_000), h.loan(amount=25_000_000)
    zero_payment = h.debt(monthly_payment=0)
    too_big = h.debt(amount=1_000_000_000)
    over_amount = h.debt(amount=100000, monthly_payment=100001)
    zero_amount = h.debt(amount=0, monthly_payment=0)
    rejected, r = await reasons(client, {"debts": [big_debt, zero_payment, too_big, over_amount, zero_amount],
                                         "loans": [big_loan]})
    assert set(rejected) == {big_loan["id"], too_big["id"], over_amount["id"], zero_amount["id"]}
    assert r["applied"]["debts"] == 2


async def test_new_field_rules(client):
    p = h.plan()
    bad = {
        "person": h.loan(person=" "), "long_person": h.loan(person="x" * 101),
        "kind": h.debt(kind="bank"), "month13": h.debt(first_month="2026-13"), "month_fmt": h.debt(first_month="2026-1"),
        "due_day": h.debt(due_day=32), "name": h.debt(name=""),
        "closed": v04_plan(closed_days=[0, 5]), "closed_32": v04_plan(closed_days=[32]),
        "closed_type": v04_plan(closed_days="5"), "reserved": v04_plan(reserved_debt_ids=["nope"]),
        "reason": h.bill(p["id"], exclude_reason="x" * 51), "null_bool": h.bill(p["id"], is_included=None),
        "rollover_amount": v04_plan(rollover_amount=10_000_000), "bad_date": h.loan(due_on="2026-02-30"),
        "created_at": v04_plan(created_at="2026-10-30"),
    }
    by_label = {label: rec for label, rec in bad.items()}
    records = {"month_plans": [p, by_label["closed"], by_label["closed_32"], by_label["closed_type"],
                               by_label["reserved"], by_label["rollover_amount"], by_label["created_at"]],
               "bills": [by_label["reason"], by_label["null_bool"]],
               "loans": [by_label["person"], by_label["long_person"], by_label["bad_date"]],
               "debts": [by_label["kind"], by_label["month13"], by_label["month_fmt"], by_label["due_day"], by_label["name"]]}
    rejected, r = await reasons(client, records)
    expected = {rec["id"] for rec in bad.values()}
    assert set(rejected) == expected
    assert r["applied"]["month_plans"] == 1
    assert all(reason for reason in rejected.values())


async def test_repayment_needs_a_stored_loan_and_plan_refs_must_exist(client):
    p = h.plan()
    orphan = h.repayment(h.new_id())
    ghost_loan = h.loan(h.new_id())
    ghost_payment = h.debt_payment(None, h.new_id())
    ok_payment = h.debt_payment(h.new_id(), p["id"])  # a missing debt is stored as sent
    rejected, r = await reasons(client, {"month_plans": [p], "loans": [ghost_loan], "loan_repayments": [orphan],
                                         "debt_payments": [ghost_payment, ok_payment]})
    assert set(rejected) == {orphan["id"], ghost_loan["id"], ghost_payment["id"]}
    assert "loan" in rejected[orphan["id"]] and "plan" in rejected[ghost_loan["id"]]
    assert r["applied"]["debt_payments"] == 1


async def test_bill_naming_a_missing_debt_is_stored_and_survives_a_split_push(client):
    p, d = h.plan(), h.debt()
    b = h.bill(p["id"], debt_id=d["id"])
    first = await h.push(client, {"month_plans": [p], "bills": [b]})  # the debt arrives on a later page
    assert first.json()["rejected"] == []
    await h.push(client, {"debts": [d]})
    bill = (await client.get("/v1/sync/snapshot")).json()["records"]["bills"][0]
    assert bill["debt_id"] == d["id"]


async def test_plan_may_name_a_deleted_goal_but_not_a_missing_one(client):
    g = h.goal()
    gone = {"updated_at": "2026-10-27T11:00:00Z", "deleted_at": "2026-10-27T11:00:00Z"}
    await h.push(client, {"savings_goals": [g]})
    await h.push(client, {"savings_goals": [h.rec(id=g["id"], **gone)]})
    live = v04_plan(rollover_goal_id=g["id"])
    missing = v04_plan(month=12, rollover_goal_id=h.new_id())
    rejected, _ = await reasons(client, {"month_plans": [live, missing]})
    assert set(rejected) == {missing["id"]}


async def test_loan_may_outlive_a_deleted_plan(client):
    p = h.plan()
    await h.push(client, {"month_plans": [p]})
    await h.push(client, {"month_plans": [h.rec(id=p["id"], updated_at="2026-10-27T11:00:00Z",
                                                deleted_at="2026-10-27T11:00:00Z")]})
    rejected, _ = await reasons(client, {"loans": [h.loan(p["id"])]})
    assert rejected == {}


async def test_overpaid_loan_is_logged_not_rejected(client):
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append((record.getMessage(), getattr(record, "fields", {})))

    handler = Collect()
    logging.getLogger("intoday").addHandler(handler)
    try:
        ln = h.loan(amount=100000)
        r = await h.push(client, {"loans": [ln], "loan_repayments": [h.repayment(ln["id"], amount=70000),
                                                                      h.repayment(ln["id"], amount=70000)]})
    finally:
        logging.getLogger("intoday").removeHandler(handler)
    assert r.json()["rejected"] == [] and r.json()["applied"]["loan_repayments"] == 2
    assert ("loan_overpaid", {"loan_id": ln["id"]}) in seen
    assert "70000" not in str(seen)  # ids only, never amounts


async def test_income_dated_outside_the_plan_month_is_still_fine_with_new_flags(client):
    p = h.plan(month=11, year=2026)
    rejected, _ = await reasons(client, {"month_plans": [p],
                                         "income_events": [h.income(p["id"], date="2026-10-30", is_rollover=False)]})
    assert rejected == {}


# ---- 22. erasure ---------------------------------------------------------------------

async def test_erase_empties_new_tables(app, client):
    p, d = h.plan(), h.debt()
    ln = h.loan(p["id"])
    await h.push(client, {"debts": [d], "month_plans": [p], "loans": [ln], "loan_repayments": [h.repayment(ln["id"])],
                          "debt_payments": [h.debt_payment(d["id"], p["id"])], "category_limits": [h.limit()]})
    r = await client.delete("/v1/account", headers={"X-Confirm": "delete-all"})
    assert r.status_code == 200
    for table in ("debts", "loans", "loan_repayments", "debt_payments", "category_limits"):
        assert await stored(app, f"SELECT 1 FROM {table}") == [], table
    snap = (await client.get("/v1/sync/snapshot")).json()["records"]
    assert all(rows == [] for rows in snap.values())
