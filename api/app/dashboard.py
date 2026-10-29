"""The data behind the dashboard view: one month in full, plus the all-time picture.

Read-only plain sums of what is stored (same caveat as the PDF report: the app's own Today rules
are richer than a sum, so the app's `today` snapshot is passed through untouched next to them).
"""
from calendar import monthrange
from datetime import date, datetime

from . import export, sync
from . import validation as v

MAX_EXPENSES = 1000
TREND_MONTHS = 6


async def _rows(conn, query: str, *params) -> list[dict]:
    cur = await conn.execute(query, params)
    return await cur.fetchall()


async def build(conn, tz, year: int | None, month: int | None) -> dict:
    months = [(r["year"], r["month"]) for r in await _rows(
        conn, "SELECT year, month FROM month_plans WHERE deleted_at IS NULL ORDER BY year DESC, month DESC")]
    goals = await _goals(conn)
    limits = {r["tag"]: r["amount"] for r in await _rows(
        conn, "SELECT tag, amount FROM category_limits WHERE deleted_at IS NULL")}
    out = {
        "generated_at": datetime.now(tz), "months": [export.month_label(*m) for m in months],
        "month": None, "plan": None, "totals": None, "days": [], "by_tag": [], "income": [], "bills": [],
        "expenses": [], "expenses_truncated": False, "allocations": [], "goals": goals, "loans": [], "debts": [],
        "trend": [], "today": None, "insights": None,
    }
    if year is None:
        if not months:
            return out
        year, month = months[0]
    report = await export.month_report(conn, year, month)  # raises export.NoPlan
    plan = report["plan"]
    today = datetime.now(tz).date()

    spent_by_day: dict[date, int] = {}
    for e in report["expenses"]:
        spent_by_day[e["date"]] = spent_by_day.get(e["date"], 0) + e["amount"]
    last_day = monthrange(year, month)[1]
    days = [{"date": date(year, month, d), "spent": spent_by_day.get(date(year, month, d), 0)} for d in range(1, last_day + 1)]

    spent_by_tag = report["totals"]["by_tag"]
    by_tag = [{"tag": t, "spent": spent_by_tag.get(t, 0), "limit": limits.get(t)} for t in v.EXPENSE_TAGS]
    by_tag.sort(key=lambda x: -x["spent"])

    loans = []
    for l in report["loans"]:
        remaining = max(l["remaining"], 0)
        loans.append({**l, "remaining": remaining, "late": bool(l["due_on"] and l["due_on"] < today and remaining > 0)})

    trend = await _rows(
        conn,
        "SELECT p.year, p.month, "
        "COALESCE((SELECT SUM(amount) FROM income_events WHERE plan_id = p.id AND status = 'received' AND deleted_at IS NULL), 0) AS income, "
        "COALESCE((SELECT SUM(amount) FROM expenses WHERE plan_id = p.id AND deleted_at IS NULL), 0) AS spent, "
        "COALESCE((SELECT SUM(amount) FROM bills WHERE plan_id = p.id AND is_paid AND is_included AND deleted_at IS NULL), 0) AS bills_paid "
        "FROM month_plans p WHERE p.deleted_at IS NULL AND (p.year, p.month) <= (%s, %s) "
        "ORDER BY p.year DESC, p.month DESC LIMIT %s",
        year, month, TREND_MONTHS,
    )
    snaps = {r["kind"]: r for r in await _rows(
        conn, "SELECT kind, payload, updated_at FROM snapshots WHERE plan_year = %s AND plan_month = %s", year, month)}

    plan_view = {k: plan[k] for k in ("year", "month", "plan_state", "buffer_amount", "start_day", "rollover_decision",
                                     "rollover_amount", "closed_days", "created_at", "updated_at")}
    out.update(
        month=export.month_label(year, month), plan=plan_view, totals=report["totals"], days=days, by_tag=by_tag,
        income=[{k: i[k] for k in ("id", "label", "amount", "date", "kind", "status", "destination", "is_recurring", "is_rollover")}
                for i in report["income"]],
        bills=[{k: b[k] for k in ("id", "name", "amount", "due_day", "is_paid", "is_one_off", "is_included", "exclude_reason", "debt_id")}
               for b in report["bills"]],
        expenses=[{k: e[k] for k in ("id", "date", "tag", "note", "amount")} for e in reversed(report["expenses"][-MAX_EXPENSES:])],
        expenses_truncated=len(report["expenses"]) > MAX_EXPENSES,
        allocations=report["allocations"], loans=loans, debts=report["debts"],
        trend=[{"month": export.month_label(t["year"], t["month"]), "income": t["income"], "spent": t["spent"],
                "bills_paid": t["bills_paid"]} for t in reversed(trend)],
        today=_snap(snaps.get("today")), insights=_snap(snaps.get("insights")),
    )
    return out


def _snap(row):
    return None if row is None else {"payload": row["payload"], "updated_at": row["updated_at"]}


async def _goals(conn) -> list[dict]:
    goals = await _rows(
        conn,
        "SELECT g.id, g.name, g.target_amount, "
        "COALESCE((SELECT SUM(a.amount) FROM savings_allocations a WHERE a.goal_id = g.id AND a.deleted_at IS NULL), 0) AS allocated_total, "
        "COALESCE((SELECT SUM(e.amount) FROM income_events e WHERE e.savings_goal_id = g.id "
        "AND e.deleted_at IS NULL AND e.status = 'received'), 0) AS income_to_savings_received "
        "FROM savings_goals g WHERE g.deleted_at IS NULL ORDER BY g.name, g.id",
    )
    for g in goals:
        g["locked_total"] = g["allocated_total"] + g["income_to_savings_received"]
    return goals


def serialise(payload: dict) -> dict:
    return sync.ser_deep(payload)
