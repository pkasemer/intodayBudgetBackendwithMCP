"""Data export: CSV (one table, or every table in a zip) and a monthly PDF report.

Read-only. Everything is built in memory from one consistent snapshot of the database; the data
set of a single user is small. Nothing here logs or returns more than the caller asked for.
"""
import csv
import io
import zipfile
from calendar import month_name
from datetime import datetime, timezone

from psycopg import sql

from . import sync
from . import validation as v

# Tables whose rows belong to a plan (loans and debt payments only when attached to one).
PLAN_OWNED = ("bills", "expenses", "income_events", "savings_allocations", "loans", "debt_payments")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
CSV_BOM = "﻿"  # lets Excel read the UTF-8 file correctly


class NoPlan(Exception):
    """The requested month has no live plan."""


# ---- CSV -------------------------------------------------------------------------

def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ";".join(str(sync.ser_value(item)) for item in value)
    text = str(sync.ser_value(value))
    # Spreadsheet formula injection: a text cell that begins like a formula is defused with a quote.
    if isinstance(value, str) and text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def month_label(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


async def table_rows(conn, table: str, year: int | None, month: int | None, include_deleted: bool):
    """(columns, rows) for one table. Plan-owned tables get a leading `plan` (YYYY-MM) column and can
    be limited to a month; the other tables are always exported in full."""
    cols = v.column_names(table)
    owned = table in PLAN_OWNED
    select = [sql.SQL("t.{}").format(sql.Identifier(c)) for c in cols]
    header = list(cols)
    join = sql.SQL("")
    where = []
    params: list = []
    if owned:
        select.insert(0, sql.SQL("(p.year::text || '-' || lpad(p.month::text, 2, '0')) AS plan"))
        header.insert(0, "plan")
        join = sql.SQL("LEFT JOIN month_plans p ON p.id = t.plan_id")
        if year is not None:
            where.append(sql.SQL("p.year = %s AND p.month = %s"))
            params += [year, month]
    if not include_deleted:
        where.append(sql.SQL("t.deleted_at IS NULL"))
    order = sql.SQL("t.date, t.seq") if "date" in cols else sql.SQL("t.seq")
    query = sql.SQL("SELECT {cols} FROM {t} t {join} {where} ORDER BY {order}").format(
        cols=sql.SQL(", ").join(select), t=sql.Identifier(table), join=join, order=order,
        where=sql.SQL("WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL(""),
    )
    cur = await conn.execute(query, params)
    return header, await cur.fetchall()


def _csv_bytes(header: list[str], rows: list[dict]) -> bytes:
    out = io.StringIO(newline="")
    out.write(CSV_BOM)
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(row[c]) for c in header])
    return out.getvalue().encode("utf-8")


async def csv_export(conn, table: str, year: int | None, month: int | None, include_deleted: bool):
    """Returns (filename, media_type, body, row_count). `table` is a sync table or "all" (a zip)."""
    suffix = "" if year is None else "-" + month_label(year, month)
    if table != "all":
        header, rows = await table_rows(conn, table, year, month, include_deleted)
        return f"intoday-{table}{suffix}.csv", "text/csv; charset=utf-8", _csv_bytes(header, rows), len(rows)
    buf, total = io.BytesIO(), 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sync.SYNC_TABLES:
            header, rows = await table_rows(conn, name, year, month, include_deleted)
            archive.writestr(f"{name}.csv", _csv_bytes(header, rows))
            total += len(rows)
    return f"intoday-all{suffix}.zip", "application/zip", buf.getvalue(), total


# ---- monthly report data -----------------------------------------------------------

async def _all(conn, query: str, *params) -> list[dict]:
    cur = await conn.execute(query, params)
    return await cur.fetchall()


async def latest_plan_month(conn) -> tuple[int, int] | None:
    rows = await _all(conn, "SELECT year, month FROM month_plans WHERE deleted_at IS NULL ORDER BY year DESC, month DESC LIMIT 1")
    return (rows[0]["year"], rows[0]["month"]) if rows else None


async def month_report(conn, year: int, month: int) -> dict:
    """Plain sums for one plan month, plus what is still owed to and by the user (all time).
    These are simple totals: the app's Today numbers apply more rules than a sum."""
    plans = await _all(conn, "SELECT * FROM month_plans WHERE year = %s AND month = %s AND deleted_at IS NULL", year, month)
    if not plans:
        raise NoPlan()
    plan = plans[0]
    pid = plan["id"]
    income = await _all(conn, "SELECT * FROM income_events WHERE plan_id = %s AND deleted_at IS NULL ORDER BY date, seq", pid)
    bills = await _all(conn, "SELECT * FROM bills WHERE plan_id = %s AND deleted_at IS NULL ORDER BY due_day, name", pid)
    expenses = await _all(conn, "SELECT * FROM expenses WHERE plan_id = %s AND deleted_at IS NULL ORDER BY date, seq", pid)
    allocations = await _all(
        conn,
        "SELECT a.amount, g.name FROM savings_allocations a JOIN savings_goals g ON g.id = a.goal_id "
        "WHERE a.plan_id = %s AND a.deleted_at IS NULL ORDER BY g.name",
        pid,
    )
    extra_debt = await _all(
        conn,
        "SELECT COALESCE(SUM(amount), 0) AS total FROM debt_payments WHERE plan_id = %s AND deleted_at IS NULL", pid,
    )
    loans = await _all(
        conn,
        "SELECT l.person, l.amount, l.lent_on, l.due_on, l.amount - COALESCE(("
        "SELECT SUM(r.amount) FROM loan_repayments r WHERE r.loan_id = l.id AND r.deleted_at IS NULL), 0) AS remaining "
        "FROM loans l WHERE l.deleted_at IS NULL AND l.repaid_on IS NULL AND NOT l.written_off ORDER BY l.due_on NULLS LAST, l.lent_on",
    )
    debts = await _all(
        conn,
        "SELECT d.name, d.kind, d.amount, d.monthly_payment, d.due_day, d.amount "
        "- COALESCE((SELECT SUM(b.amount) FROM bills b WHERE b.debt_id = d.id AND b.is_paid AND b.is_included "
        "AND b.deleted_at IS NULL), 0) "
        "- COALESCE((SELECT SUM(p.amount) FROM debt_payments p WHERE p.debt_id = d.id AND p.deleted_at IS NULL), 0) "
        "AS balance FROM debts d WHERE d.deleted_at IS NULL ORDER BY d.name",
    )

    by_tag: dict[str, int] = {}
    for e in expenses:
        by_tag[e["tag"]] = by_tag.get(e["tag"], 0) + e["amount"]
    included = [b for b in bills if b["is_included"]]
    return {
        "year": year, "month": month, "plan": plan,
        "generated_at": datetime.now(timezone.utc),
        "income": income, "bills": bills, "expenses": expenses, "allocations": allocations,
        "loans": loans, "debts": debts,
        "totals": {
            "income_received": sum(i["amount"] for i in income if i["status"] == "received"),
            "income_expected": sum(i["amount"] for i in income if i["status"] == "expected"),
            "bills_reserved": sum(b["amount"] for b in included),
            "bills_paid": sum(b["amount"] for b in included if b["is_paid"]),
            "bills_unpaid": sum(b["amount"] for b in included if not b["is_paid"]),
            "bills_excluded": sum(1 for b in bills if not b["is_included"]),
            "spent": sum(by_tag.values()),
            "by_tag": dict(sorted(by_tag.items(), key=lambda kv: -kv[1])),
            "saved": sum(a["amount"] for a in allocations),
            "extra_debt_payments": extra_debt[0]["total"],
            "lent_owed": sum(max(l["remaining"], 0) for l in loans),
            "debt_balance": sum(max(d["balance"], 0) for d in debts),
        },
    }


# ---- PDF -------------------------------------------------------------------------

def _latin(text) -> str:
    """The built-in PDF fonts are Latin-1; anything else (emoji, some punctuation) becomes '?'."""
    return str(text).replace("—", "-").replace("–", "-").replace("’", "'").encode(
        "latin-1", "replace").decode("latin-1")


def money(n) -> str:
    return f"UGX {int(n):,}"


def _short_date(d) -> str:
    return d.strftime("%d %b") if d else ""


def render_pdf(report: dict) -> bytes:
    from fpdf import FPDF  # imported here so the rest of the module works without it (tests of CSV)

    class Report(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(120, 110, 95)
            self.cell(0, 6, f"InToday - {_latin(title)} - page {self.page_no()}/{{nb}}", align="C")

    year, month, t = report["year"], report["month"], report["totals"]
    title = f"{month_name[month]} {year}"
    pdf = Report(format="A4")
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, 18)
    pdf.set_title(f"InToday - {title}")
    pdf.set_creator("InToday backend")
    pdf.alias_nb_pages()
    pdf.add_page()
    ink, faint = (58, 46, 33), (120, 110, 95)

    def heading(text, size=12, gap=5):
        if pdf.get_y() > pdf.h - 60:  # keep a heading with the start of its table
            pdf.add_page()
        pdf.ln(gap)
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(*ink)
        pdf.cell(0, 7, _latin(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*faint)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2)

    def note(text):
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*faint)
        pdf.multi_cell(0, 5, _latin(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*ink)

    def table(headers, rows, widths, right=()):
        if not rows:
            note("None.")
            return
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(*ink)
        align = tuple("RIGHT" if i in right else "LEFT" for i in range(len(headers)))
        with pdf.table(col_widths=widths, text_align=align, borders_layout="HORIZONTAL_LINES",
                       line_height=5.5, first_row_as_headings=True) as tbl:
            head = tbl.row()
            for h_ in headers:
                head.cell(_latin(h_))
            for r in rows:
                row = tbl.row()
                for value in r:
                    row.cell(_latin(value))

    pdf.set_text_color(*ink)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, _latin(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(*faint)
    plan = report["plan"]
    pdf.cell(0, 5, _latin(f"Plan {plan['plan_state']} - buffer {money(plan['buffer_amount'])} - generated "
                          f"{report['generated_at'].strftime('%d %b %Y %H:%M')} UTC"), new_x="LMARGIN", new_y="NEXT")

    heading("Summary")
    table(["", "Amount"], [
        ["Income received", money(t["income_received"])],
        ["Income still expected", money(t["income_expected"])],
        ["Bills (reserved this month)", money(t["bills_reserved"])],
        ["   of which paid", money(t["bills_paid"])],
        ["   of which unpaid", money(t["bills_unpaid"])],
        ["Spent", money(t["spent"])],
        ["Saved to goals", money(t["saved"])],
        ["Extra debt payments", money(t["extra_debt_payments"])],
    ], (110, 72), right=(1,))
    note("Plain totals of what is recorded. The app's Today numbers apply more rules than a sum.")

    heading("Spending by category")
    table(["Category", "Spent"], [[k, money(n)] for k, n in t["by_tag"].items()], (110, 72), right=(1,))

    heading("Income")
    table(["Date", "Label", "Status", "Goes to", "Amount"],
          [[_short_date(i["date"]), i["label"], i["status"], "savings" if i["destination"] == "lockedSavings" else "spendable",
            money(i["amount"])] for i in report["income"]], (22, 62, 28, 28, 42), right=(4,))

    heading("Bills")
    table(["Due", "Bill", "State", "Amount"],
          [[f"day {b['due_day']}", b["name"],
            ("paid" if b["is_paid"] else "unpaid") + ("" if b["is_included"] else " (not this month)"),
            money(b["amount"])] for b in report["bills"]], (22, 80, 38, 42), right=(3,))

    heading(f"Expenses ({len(report['expenses'])})")
    table(["Date", "Category", "Note", "Amount"],
          [[_short_date(e["date"]), e["tag"], e["note"] or "", money(e["amount"])] for e in report["expenses"]],
          (22, 32, 86, 42), right=(3,))

    heading("Savings goals funded this month")
    table(["Goal", "Amount"], [[a["name"], money(a["amount"])] for a in report["allocations"]], (110, 72), right=(1,))

    heading("Money lent, still owed to you (all time)")
    table(["Person", "Lent", "Due", "Still owed"],
          [[l["person"], _short_date(l["lent_on"]), _short_date(l["due_on"]), money(max(l["remaining"], 0))]
           for l in report["loans"]], (80, 28, 28, 46), right=(3,))
    if report["loans"]:
        note(f"Total owed to you: {money(t['lent_owed'])}")

    heading("Debts, still owed by you (all time)")
    table(["Debt", "Kind", "Monthly", "Balance"],
          [[d["name"], d["kind"], money(d["monthly_payment"]) if d["monthly_payment"] else "-", money(max(d["balance"], 0))]
           for d in report["debts"]], (70, 24, 44, 44), right=(2, 3))
    if report["debts"]:
        note(f"Total you owe: {money(t['debt_balance'])}")

    return bytes(pdf.output())
