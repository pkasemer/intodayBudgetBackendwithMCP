"""MCP domain tools (backend spec §8). Reads never write; writes are single transactions
that go through the same validation and LWW code as pushed records.

There is deliberately no SQL tool and no schema-browsing tool (§8.1).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import sync
from . import validation as v
from .validation import Rejected

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_YEAR = {"type": "integer", "minimum": 2000, "maximum": 2100}
_MONTH = {"type": "integer", "minimum": 1, "maximum": 12}
_UUID = {"type": "string", "description": "UUID of the record"}


class ToolError(Exception):
    """A tool-level failure; shown to the model as an isError result, not a protocol error."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict
    required: tuple
    handler: Callable[..., Awaitable[dict]]
    write: bool = False
    record_key: str | None = None  # key in a write result holding the row, for ID-only logging

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": self.properties, "required": list(self.required)}

    def listing(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": not self.write,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        }


def check_args(tool: Tool, args) -> dict:
    """Validate arguments against the tool's own schema. Messages name the field, never the value."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    for name in tool.required:
        if args.get(name) is None:
            raise ToolError(f"'{name}' is required")
    out = {}
    for name, spec in tool.properties.items():
        value = args.get(name)
        if value is None:
            continue
        kind = spec["type"]
        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"'{name}' must be an integer")
            if not spec.get("minimum", value) <= value <= spec.get("maximum", value):
                raise ToolError(f"'{name}' must be between {spec['minimum']} and {spec['maximum']}")
        elif kind == "string":
            if not isinstance(value, str):
                raise ToolError(f"'{name}' must be a string")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError(f"'{name}' must be one of: {', '.join(spec['enum'])}")
            if len(value) > spec.get("maxLength", len(value)):
                raise ToolError(f"'{name}' must be at most {spec['maxLength']} characters")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ToolError(f"'{name}' must be true or false")
        out[name] = value
    return out


def _guard(fn):
    """Turn validation rejections from shared code into clean tool errors."""

    async def wrapper(conn, settings, args):
        try:
            return await fn(conn, settings, args)
        except Rejected as exc:
            raise ToolError(exc.reason) from None

    wrapper.__name__ = fn.__name__
    return wrapper


def _snapshot_payload(row) -> dict | None:
    return None if row is None else row["payload"]


async def _snapshot_row(conn, kind: str, plan: dict | None):
    if plan is None:
        return None
    cur = await conn.execute(
        "SELECT payload, updated_at FROM snapshots WHERE kind = %s AND plan_year = %s AND plan_month = %s",
        (kind, plan["year"], plan["month"]),
    )
    return await cur.fetchone()


async def _plan_or_error(conn, args) -> dict:
    plan = await sync.plan_by_month(conn, args["year"], args["month"])
    if plan is None:
        raise ToolError(f"No plan exists for {args['year']}-{args['month']:02d}.")
    return plan


# ---- read tools ------------------------------------------------------------------

@_guard
async def get_today(conn, settings, args):
    day = v.parse_date(args["date"], "date") if "date" in args else datetime.now(settings.timezone).date()
    plan = await sync.plan_for_date(conn, day)
    row = await _snapshot_row(conn, "today", plan)
    if row is not None:
        payload = row["payload"]
        return {
            "date": day,
            "plan": {"year": plan["year"], "month": plan["month"]},
            "snapshot": payload,
            "as_of": payload.get("as_of") or row["updated_at"],
            "snapshot_updated_at": row["updated_at"],
        }
    expenses_today = bills_due = 0
    if plan is not None:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM expenses WHERE plan_id = %s AND date = %s AND deleted_at IS NULL",
            (plan["id"], day),
        )
        expenses_today = (await cur.fetchone())["n"]
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM bills WHERE plan_id = %s AND due_day = %s "
            "AND NOT is_paid AND deleted_at IS NULL",
            (plan["id"], day.day),
        )
        bills_due = (await cur.fetchone())["n"]
    return {
        "date": day,
        "snapshot": None,
        "note": "No snapshot yet — the app has not synced.",
        "expenses_today": expenses_today,
        "bills_due": bills_due,
    }


async def get_month_summary(conn, settings, args):
    plan = await _plan_or_error(conn, args)
    pid = plan["id"]

    async def one(query):
        cur = await conn.execute(query, (pid,))
        return await cur.fetchone()

    spent = await one("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM expenses WHERE plan_id = %s AND deleted_at IS NULL")
    income = await one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount) FILTER (WHERE status = 'received'), 0) AS received, "
        "COALESCE(SUM(amount) FILTER (WHERE status = 'expected'), 0) AS expected "
        "FROM income_events WHERE plan_id = %s AND deleted_at IS NULL"
    )
    bills = await one(
        "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE is_paid) AS paid, COUNT(*) FILTER (WHERE NOT is_paid) AS unpaid, "
        "COALESCE(SUM(amount) FILTER (WHERE is_paid), 0) AS paid_amount, "
        "COALESCE(SUM(amount) FILTER (WHERE NOT is_paid), 0) AS unpaid_amount "
        "FROM bills WHERE plan_id = %s AND deleted_at IS NULL"
    )
    allocations = await one("SELECT COUNT(*) AS n FROM savings_allocations WHERE plan_id = %s AND deleted_at IS NULL")
    # Plain summation of stored rows — arithmetic, not pacing maths (§6, §8.2).
    return {
        "plan": {k: plan[k] for k in ("id", "year", "month", "buffer_amount", "start_day", "plan_state", "rollover_decision", "updated_at")},
        "counts": {
            "bills": bills["n"],
            "expenses": spent["n"],
            "income_events": income["n"],
            "savings_allocations": allocations["n"],
        },
        "totals": {
            "total_spent": spent["total"],
            "total_income_received": income["received"],
            "total_income_expected": income["expected"],
            "bills_paid": bills["paid"],
            "bills_unpaid": bills["unpaid"],
            "bills_paid_amount": bills["paid_amount"],
            "bills_unpaid_amount": bills["unpaid_amount"],
        },
        "snapshots": {
            "today": _snapshot_payload(await _snapshot_row(conn, "today", plan)),
            "insights": _snapshot_payload(await _snapshot_row(conn, "insights", plan)),
        },
    }


async def _rows_for_plan(conn, args, table: str, where: str, order: str, extra=(), limit: int | None = None):
    plan = await sync.plan_by_month(conn, args["year"], args["month"])
    if plan is None:
        return None, []
    query = f"SELECT * FROM {table} WHERE plan_id = %s AND deleted_at IS NULL {where} ORDER BY {order}"
    params = [plan["id"], *extra]
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    cur = await conn.execute(query, params)
    return plan, [sync.ser_row(r) for r in await cur.fetchall()]


async def list_expenses(conn, settings, args):
    where, extra = ("AND tag = %s", [args["tag"]]) if "tag" in args else ("", [])
    plan, rows = await _rows_for_plan(
        conn, args, "expenses", where, "date DESC, updated_at DESC, id", extra, args.get("limit", 50)
    )
    return {"plan": None if plan is None else {"year": plan["year"], "month": plan["month"]}, "expenses": rows}


async def list_bills(conn, settings, args):
    where = "AND NOT is_paid" if args.get("unpaid_only") else ""
    plan, rows = await _rows_for_plan(conn, args, "bills", where, "due_day, name, id")
    return {"plan": None if plan is None else {"year": plan["year"], "month": plan["month"]}, "bills": rows}


async def list_income_events(conn, settings, args):
    plan, rows = await _rows_for_plan(conn, args, "income_events", "", "date, id")
    return {"plan": None if plan is None else {"year": plan["year"], "month": plan["month"]}, "income_events": rows}


async def list_goals(conn, settings, args):
    cur = await conn.execute(
        "SELECT g.id, g.name, g.target_amount, g.updated_at, "
        "COALESCE((SELECT SUM(a.amount) FROM savings_allocations a "
        "          WHERE a.goal_id = g.id AND a.deleted_at IS NULL), 0) AS allocated_total, "
        "COALESCE((SELECT SUM(e.amount) FROM income_events e WHERE e.savings_goal_id = g.id "
        "          AND e.deleted_at IS NULL AND e.status = 'received'), 0) AS income_to_savings_received "
        "FROM savings_goals g WHERE g.deleted_at IS NULL ORDER BY g.name, g.id"
    )
    goals = []
    for row in await cur.fetchall():
        row["locked_total"] = row["allocated_total"] + row["income_to_savings_received"]
        goals.append(row)
    return {"goals": goals}


async def get_insights(conn, settings, args):
    plan = await sync.plan_by_month(conn, args["year"], args["month"])
    row = await _snapshot_row(conn, "insights", plan)
    if row is not None:
        return {"snapshot": row["payload"], "snapshot_updated_at": row["updated_at"]}
    totals = {}
    if plan is not None:
        cur = await conn.execute(
            "SELECT tag, SUM(amount) AS total FROM expenses WHERE plan_id = %s AND deleted_at IS NULL GROUP BY tag",
            (plan["id"],),
        )
        totals = {r["tag"]: r["total"] for r in await cur.fetchall()}
    return {"snapshot": None, "note": "No insights snapshot yet — the app has not synced.", "tag_totals": totals}


async def list_months(conn, settings, args):
    cur = await conn.execute(
        "SELECT year, month, plan_state FROM month_plans WHERE deleted_at IS NULL ORDER BY year DESC, month DESC"
    )
    return {"months": await cur.fetchall()}


async def list_loans(conn, settings, args):
    today = datetime.now(settings.timezone).date()
    cur = await conn.execute(
        "SELECT l.id, l.person, l.amount, l.lent_on, l.due_on, l.repaid_on, l.written_off, l.updated_at, "
        "COALESCE((SELECT SUM(r.amount) FROM loan_repayments r WHERE r.loan_id = l.id AND r.deleted_at IS NULL), 0) "
        "AS repaid_total FROM loans l WHERE l.deleted_at IS NULL "
        + ("AND l.repaid_on IS NULL AND NOT l.written_off " if args.get("open_only") else "")
        + "ORDER BY l.due_on NULLS LAST, l.lent_on, l.id"
    )
    loans, owed, late_count = [], 0, 0
    for row in await cur.fetchall():
        row["status"] = "written_off" if row["written_off"] else "repaid" if row["repaid_on"] else "open"
        row["remaining"] = max(row["amount"] - row["repaid_total"], 0)
        row["late"] = bool(row["status"] == "open" and row["remaining"] > 0 and row["due_on"] and row["due_on"] < today)
        if row["status"] == "open":
            owed += row["remaining"]
        late_count += row["late"]
        loans.append(row)
    return {"loans": loans, "owed_total": owed, "late_count": late_count}


async def list_debts(conn, settings, args):
    # balance = amount − paid included bills that name the debt − extra debt payments (same sum as the PDF report).
    cur = await conn.execute(
        "SELECT d.id, d.name, d.kind, d.amount, d.monthly_payment, d.due_day, d.started_on, d.first_month, d.updated_at, "
        "COALESCE((SELECT SUM(b.amount) FROM bills b WHERE b.debt_id = d.id AND b.is_paid AND b.is_included "
        "AND b.deleted_at IS NULL), 0) "
        "+ COALESCE((SELECT SUM(p.amount) FROM debt_payments p WHERE p.debt_id = d.id AND p.deleted_at IS NULL), 0) "
        "AS paid_total FROM debts d WHERE d.deleted_at IS NULL ORDER BY d.name, d.id"
    )
    debts = await cur.fetchall()
    cur = await conn.execute(
        "SELECT DISTINCT ON (b.debt_id) b.debt_id, b.id, b.name, b.amount, b.due_day, p.year, p.month "
        "FROM bills b JOIN month_plans p ON p.id = b.plan_id AND p.deleted_at IS NULL "
        "WHERE b.deleted_at IS NULL AND NOT b.is_paid AND b.is_included AND b.debt_id IS NOT NULL "
        "ORDER BY b.debt_id, p.year, p.month, b.due_day, b.id"
    )
    next_bill = {r["debt_id"]: r for r in await cur.fetchall()}
    out, total = [], 0
    for row in debts:
        row["balance"] = max(row["amount"] - row["paid_total"], 0)
        nb = next_bill.get(row["id"])
        row["next_unpaid_bill"] = None if nb is None else {
            "id": nb["id"], "name": nb["name"], "amount": nb["amount"], "due_day": nb["due_day"],
            "plan": {"year": nb["year"], "month": nb["month"]},
        }
        if args.get("open_only") and row["balance"] == 0:
            continue
        total += row["balance"]
        out.append(row)
    return {"debts": out, "balance_total": total}


async def get_category_limits(conn, settings, args):
    if ("year" in args) != ("month" in args):
        raise ToolError("Give both 'year' and 'month', or neither.")
    if "year" in args:
        plan = await sync.plan_by_month(conn, args["year"], args["month"])
    else:
        plan = await sync.plan_for_date(conn, datetime.now(settings.timezone).date())
    cur = await conn.execute("SELECT tag, amount FROM category_limits WHERE deleted_at IS NULL")
    limits = {r["tag"]: r["amount"] for r in await cur.fetchall()}
    spent = {}
    if plan is not None:
        cur = await conn.execute(
            "SELECT tag, SUM(amount) AS total FROM expenses WHERE plan_id = %s AND deleted_at IS NULL GROUP BY tag",
            (plan["id"],),
        )
        spent = {r["tag"]: r["total"] for r in await cur.fetchall()}
    rows = []
    for tag in v.EXPENSE_TAGS:
        limit, used = limits.get(tag), spent.get(tag, 0)
        rows.append({
            "tag": tag, "limit": limit, "spent": used,
            "remaining": None if limit is None else limit - used,
            "over": limit is not None and used > limit,
        })
    return {"plan": None if plan is None else {"year": plan["year"], "month": plan["month"]}, "limits": rows}


# ---- write tools -----------------------------------------------------------------

def _now_ms() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


@_guard
async def add_expense(conn, settings, args):
    day = v.parse_date(args["date"], "date")
    plan = await sync.plan_for_date(conn, day)
    if plan is None:
        raise ToolError("No plan covers that date; the plan must exist (created by the app) first.")
    raw = {
        "id": str(uuid.uuid4()),
        "updated_at": sync.ser_value(_now_ms()),
        "device_id": sync.MCP_DEVICE,
        "deleted_at": None,
        "plan_id": str(plan["id"]),
        "amount": args["amount"],
        "date": args["date"],
        "tag": args["tag"],
        "note": args.get("note"),
    }
    await sync.apply_record(conn, "expenses", raw, sync.MCP_DEVICE)
    cur = await conn.execute("SELECT * FROM expenses WHERE id = %s", (raw["id"],))
    return {"expense": sync.ser_row(await cur.fetchone()), "device_sync_pending": True}


async def _flip(conn, table: str, record_id: str, column: str, current, new, noun: str, done_message: str):
    """Set one column on a row if it currently holds `current`; bump LWW so the phone takes it."""
    rid = v.parse_uuid(record_id, f"{noun}_id")
    cur = await conn.execute(f"SELECT * FROM {table} WHERE id = %s AND deleted_at IS NULL FOR UPDATE", (rid,))
    row = await cur.fetchone()
    if row is None:
        raise ToolError(f"No such {noun.replace('_', ' ')}.")
    if row[column] != current:
        raise ToolError(done_message)
    # GREATEST keeps the write ahead of the row's own clock even if the phone's was fast.
    cur = await conn.execute(
        f"UPDATE {table} SET {column} = %s, updated_at = GREATEST(%s, updated_at + interval '1 millisecond'), "
        f"device_id = %s WHERE id = %s RETURNING *",
        (new, _now_ms(), sync.MCP_DEVICE, rid),
    )
    return sync.ser_row(await cur.fetchone())


@_guard
async def mark_bill_paid(conn, settings, args):
    bill = await _flip(conn, "bills", args["bill_id"], "is_paid", False, True, "bill", "That bill is already marked paid.")
    return {"bill": bill, "device_sync_pending": True}


@_guard
async def mark_income_received(conn, settings, args):
    event = await _flip(
        conn, "income_events", args["income_event_id"], "status", "expected", "received",
        "income_event", "That income is not in the expected state.",
    )
    return {"income_event": event, "device_sync_pending": True}


# ---- registry --------------------------------------------------------------------

_YM = {"year": _YEAR, "month": _MONTH}

TOOLS: tuple[Tool, ...] = (
    Tool(
        "get_today",
        "Today's numbers as computed by the app (safe_today, remaining_today, pace_delta, ...), "
        "from the latest synced snapshot. Falls back to raw counts if the app has not synced.",
        {"date": {"type": "string", "description": "YYYY-MM-DD; defaults to today", "pattern": DATE_PATTERN}},
        (),
        get_today,
    ),
    Tool(
        "get_month_summary",
        "A plan month: plan row, record counts, simple totals (spent, income received, bills paid/unpaid) "
        "and the live snapshots if present.",
        _YM, ("year", "month"), get_month_summary,
    ),
    Tool(
        "list_expenses",
        "Expenses for a plan month, newest first.",
        {**_YM, "tag": {"type": "string", "enum": list(v.EXPENSE_TAGS)},
         "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "default 50"}},
        ("year", "month"), list_expenses,
    ),
    Tool(
        "list_bills", "Bills for a plan month.",
        {**_YM, "unpaid_only": {"type": "boolean"}}, ("year", "month"), list_bills,
    ),
    Tool(
        "list_income_events", "Income rows for a plan month, with status and destination.",
        _YM, ("year", "month"), list_income_events,
    ),
    Tool(
        "list_goals",
        "Savings goals with locked totals (allocations plus received income sent to savings).",
        {}, (), list_goals,
    ),
    Tool(
        "get_insights",
        "The Insights numbers from the latest synced snapshot, or raw tag totals if none yet.",
        _YM, ("year", "month"), get_insights,
    ),
    Tool(
        "list_months",
        "The plan months that exist (newest first), so year/month arguments to other tools can be chosen.",
        {}, (), list_months,
    ),
    Tool(
        "list_loans",
        "Money lent to people: amount, repaid so far, remaining, due date and whether it is late. "
        "open_only hides repaid and written-off loans.",
        {"open_only": {"type": "boolean"}}, (), list_loans,
    ),
    Tool(
        "list_debts",
        "Debts the user owes: amount, balance left, monthly payment, due day and the next unpaid reserved bill. "
        "open_only hides cleared debts.",
        {"open_only": {"type": "boolean"}}, (), list_debts,
    ),
    Tool(
        "get_category_limits",
        "Each spending category's limit and how much is spent against it, for the current month "
        "or the given year and month.",
        _YM, (), get_category_limits,
    ),
    Tool(
        "add_expense",
        "Add an expense. Writes data: needs the user's approval in the conversation. "
        "The phone picks it up on its next sync.",
        {"amount": {"type": "integer", "minimum": 1, "maximum": v.MAX_AMOUNT, "description": "whole UGX"},
         "tag": {"type": "string", "enum": list(v.EXPENSE_TAGS)},
         "date": {"type": "string", "description": "YYYY-MM-DD", "pattern": DATE_PATTERN},
         "note": {"type": "string", "maxLength": 200}},
        ("amount", "tag", "date"), add_expense, write=True, record_key="expense",
    ),
    Tool(
        "mark_bill_paid",
        "Mark an unpaid bill as paid. Writes data: needs the user's approval in the conversation.",
        {"bill_id": _UUID}, ("bill_id",), mark_bill_paid, write=True, record_key="bill",
    ),
    Tool(
        "mark_income_received",
        "Mark an expected income as received. Writes data: needs the user's approval in the conversation.",
        {"income_event_id": _UUID}, ("income_event_id",), mark_income_received, write=True, record_key="income_event",
    ),
)

BY_NAME = {t.name: t for t in TOOLS}
