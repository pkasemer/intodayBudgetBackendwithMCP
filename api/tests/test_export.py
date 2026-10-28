"""CSV and PDF export."""
import csv
import io
import logging
import zipfile

from app import export

from . import helpers as h
from .test_v04 import SPEC_TABLES, v04_plan


def parse(response):
    text = response.content.decode("utf-8")
    assert text.startswith("﻿")  # BOM so Excel reads UTF-8
    return list(csv.DictReader(io.StringIO(text[1:])))


async def seed(client):
    """October with a bit of everything, plus an empty November."""
    g, d = h.goal(name="Laptop"), h.debt(amount=1_000_000)
    p = v04_plan(month=10, closed_days=[9, 3])
    nov = h.plan(month=11)
    ln = h.loan(p["id"], amount=100000)
    bill = h.bill(p["id"], name="Stanbic", amount=150000, due_day=12, is_paid=True, debt_id=d["id"])
    spent = [h.expense(p["id"], amount=15000, date="2026-10-20", tag="food", note="lunch"),
             h.expense(p["id"], amount=4500, date="2026-10-05", tag="transport", note="=HYPERLINK(\"http://x\")"),
             h.expense(p["id"], amount=900, date="2026-10-21", tag="fun", note="movie \U0001F37F — night")]
    records = {
        "savings_goals": [g], "debts": [d], "month_plans": [p, nov],
        "bills": [bill, h.bill(p["id"], name="Rent", amount=300000, due_day=28),
                  h.bill(p["id"], name="Gym", amount=40000, is_included=False)],
        "expenses": spent,
        "income_events": [h.income(p["id"], label="-Salary", amount=900000, status="received"),
                          h.income(p["id"], label="Gift", amount=50000, kind="extra")],
        "savings_allocations": [h.allocation(p["id"], g["id"], amount=70000)],
        "loans": [ln], "loan_repayments": [h.repayment(ln["id"], amount=40000)],
        "debt_payments": [h.debt_payment(d["id"], p["id"], amount=50000)],
        "category_limits": [h.limit()],
    }
    r = await h.push(client, records)
    assert r.json()["rejected"] == [], r.text
    return p, nov, spent


async def test_export_needs_the_token(anon):
    assert (await anon.get("/v1/export/csv")).status_code == 401
    assert (await anon.get("/v1/export/pdf")).status_code == 401


async def test_csv_for_one_table(client):
    p, _, spent = await seed(client)
    r = await client.get("/v1/export/csv", params={"table": "expenses"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="intoday-expenses.csv"'
    assert r.headers["cache-control"] == "no-store"
    rows = parse(r)
    assert list(rows[0])[:3] == ["plan", "id", "updated_at"]
    assert [x["date"] for x in rows] == ["2026-10-05", "2026-10-20", "2026-10-21"]  # by date
    assert {x["plan"] for x in rows} == {"2026-10"} and rows[1]["amount"] == "15000" and rows[1]["tag"] == "food"
    assert "\U0001F37F" in rows[2]["note"]  # non-Latin text survives in CSV


async def test_csv_defuses_spreadsheet_formulas(client):
    await seed(client)
    notes = [x["note"] for x in parse(await client.get("/v1/export/csv", params={"table": "expenses"}))]
    assert "'=HYPERLINK(\"http://x\")" in notes
    labels = [x["label"] for x in parse(await client.get("/v1/export/csv", params={"table": "income_events"}))]
    assert "'-Salary" in labels and "Gift" in labels


async def test_csv_excludes_deleted_rows_unless_asked(client):
    p, _, spent = await seed(client)
    await h.push(client, {"expenses": [h.rec(id=spent[0]["id"], updated_at="2026-10-28T11:00:00Z",
                                             deleted_at="2026-10-28T11:00:00Z")]})
    live = parse(await client.get("/v1/export/csv", params={"table": "expenses"}))
    everything = parse(await client.get("/v1/export/csv", params={"table": "expenses", "include_deleted": "true"}))
    assert len(live) == 2 and len(everything) == 3
    assert [x["deleted_at"] for x in everything if x["id"] == spent[0]["id"]][0].startswith("2026-10-28")


async def test_csv_month_filter_and_validation(client):
    p, nov, _ = await seed(client)
    await h.push(client, {"expenses": [h.expense(nov["id"], date="2026-11-03", amount=777)]})
    assert {x["plan"] for x in parse(await client.get("/v1/export/csv", params={"table": "expenses", "year": 2026, "month": 11}))} == {"2026-11"}
    assert len(parse(await client.get("/v1/export/csv", params={"table": "expenses"}))) == 4
    # Tables that don't belong to a plan are exported in full whatever the month.
    assert len(parse(await client.get("/v1/export/csv", params={"table": "savings_goals", "year": 2026, "month": 11}))) == 1
    named = await client.get("/v1/export/csv", params={"table": "expenses", "year": 2026, "month": 11})
    assert 'filename="intoday-expenses-2026-11.csv"' in named.headers["content-disposition"]
    assert (await client.get("/v1/export/csv", params={"year": 2026})).status_code == 422
    assert (await client.get("/v1/export/csv", params={"table": "snapshots"})).status_code == 422
    assert (await client.get("/v1/export/csv", params={"table": "expenses", "year": 2026, "month": 13})).status_code == 422


async def test_csv_serialises_arrays_booleans_and_timestamps(client):
    await seed(client)
    plan = next(x for x in parse(await client.get("/v1/export/csv", params={"table": "month_plans"})) if x["month"] == "10")
    assert plan["closed_days"] == "3;9" and plan["rollover_carried"] == "true" and plan["summary_shown"] == "true"
    assert plan["created_at"].startswith("2026-10-30T18:02:11") and plan["rollover_goal_id"] == ""
    assert "plan" not in plan  # a plan doesn't belong to a plan


async def test_zip_has_every_table(client):
    await seed(client)
    r = await client.get("/v1/export/csv")
    assert r.headers["content-type"] == "application/zip" and "intoday-all.zip" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == sorted(f"{t}.csv" for t in SPEC_TABLES)
    assert len(list(csv.DictReader(io.StringIO(z.read("expenses.csv").decode()[1:])))) == 3
    month = zipfile.ZipFile(io.BytesIO((await client.get("/v1/export/csv", params={"year": 2026, "month": 11})).content))
    assert len(list(csv.DictReader(io.StringIO(month.read("expenses.csv").decode()[1:])))) == 0


async def test_month_report_totals(client, app):
    p, _, _ = await seed(client)
    async with app.state.pool.connection() as conn:
        report = await export.month_report(conn, 2026, 10)
    t = report["totals"]
    assert (t["income_received"], t["income_expected"]) == (900000, 50000)
    assert (t["bills_reserved"], t["bills_paid"], t["bills_unpaid"], t["bills_excluded"]) == (450000, 150000, 300000, 1)
    assert t["spent"] == 20400 and t["by_tag"] == {"food": 15000, "transport": 4500, "fun": 900}
    assert (t["saved"], t["extra_debt_payments"]) == (70000, 50000)
    assert t["lent_owed"] == 60000
    assert [d["balance"] for d in report["debts"]] == [1_000_000 - 150000 - 50000]  # paid debt bill + extra payment
    assert [e["note"] for e in report["expenses"]][0].startswith("=")  # raw data; escaping is a CSV concern


async def test_pdf_report(client):
    await seed(client)
    r = await client.get("/v1/export/pdf", params={"year": 2026, "month": 10})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="intoday-report-2026-10.pdf"'
    assert r.content.startswith(b"%PDF") and r.content.rstrip().endswith(b"%%EOF") and len(r.content) > 2000


async def test_pdf_defaults_to_the_latest_plan_and_handles_missing_months(client):
    assert (await client.get("/v1/export/pdf")).status_code == 404  # nothing stored yet
    await seed(client)
    latest = await client.get("/v1/export/pdf")
    assert "intoday-report-2026-11.pdf" in latest.headers["content-disposition"] and latest.content.startswith(b"%PDF")
    assert (await client.get("/v1/export/pdf", params={"year": 2026, "month": 1})).status_code == 404
    assert (await client.get("/v1/export/pdf", params={"year": 2026})).status_code == 422


async def test_pdf_survives_odd_text_and_long_lists(client):
    p = h.plan()
    many = [h.expense(p["id"], note=f"item {i} \U0001F37F ก —", date="2026-10-%02d" % (1 + i % 28)) for i in range(150)]
    await h.push(client, {"month_plans": [p], "expenses": many,
                          "income_events": [h.income(p["id"], label="Pay €ก")]})
    r = await client.get("/v1/export/pdf", params={"year": 2026, "month": 10})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.content.count(b"/Type /Page\n") >= 3 or r.content.count(b"/Type /Page") >= 3  # it paginated


async def test_exports_show_in_the_feed_without_amounts_and_months_are_listed(client):
    await seed(client)
    await client.get("/v1/export/csv", params={"table": "expenses", "year": 2026, "month": 10})
    await client.get("/v1/export/pdf", params={"year": 2026, "month": 10})
    d = (await client.get("/v1/stats")).json()
    assert d["months"] == ["2026-11", "2026-10"]
    exports = [e for e in d["recent"] if e["kind"] == "export"]
    assert [(e["detail"]["format"], e["detail"]["table"], e["detail"]["rows"], e["detail"]["month"]) for e in exports] == [
        ("pdf", "report", 3, "2026-10"), ("csv", "expenses", 3, "2026-10")]
    assert all(set(e["detail"]) == {"format", "table", "rows", "month"} for e in exports)
