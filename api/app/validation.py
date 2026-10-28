"""Record validation shared by sync push and MCP writes (backend spec §7.3).

Reasons name the offending field but never echo its value — they end up in API
responses and logs, and values are money data.
"""
import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

MAX_AMOUNT = 9_999_999
MAX_DEBT_AMOUNT = 999_999_999  # bank loans exceed 7 digits (v0.4 §5.2)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_UUID_LIST = 200

EXPENSE_TAGS = ("food", "transport", "dataAirtime", "fun", "shopping", "other")


class Rejected(Exception):
    """A single record failed validation; the rest of the batch is unaffected."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # uuid | int | bool | enum | date | text | timestamp | month | int_array | uuid_array
    required: bool = True
    nullable: bool = False
    lo: int = 0
    hi: int = 0
    values: tuple = ()
    max_len: int = 0
    non_empty: bool = False
    # v0.4 §2.3: a key missing from the JSON keeps the stored value (and takes the column default
    # on insert); a key present with null sets a nullable column to null.
    sticky: bool = False


def _uuid(name, required=True, nullable=False):
    return Field(name, "uuid", required, nullable)


def _int(name, lo, hi):
    return Field(name, "int", lo=lo, hi=hi)


def _amount(name):
    return _int(name, 1, MAX_AMOUNT)


def _bool(name):
    return Field(name, "bool")


def _enum(name, values, required=True, nullable=False):
    return Field(name, "enum", required, nullable, values=values)


def _date(name):
    return Field(name, "date")


def _text(name, max_len, non_empty=False, required=True, nullable=False):
    return Field(name, "text", required, nullable, max_len=max_len, non_empty=non_empty)


def _sticky(f: Field) -> Field:
    return replace(f, required=False, sticky=True)


# Insert/apply order matters: parents before children (v0.4 §6).
TABLES: dict[str, tuple[Field, ...]] = {
    "savings_goals": (
        _text("name", 200, non_empty=True),
        _int("target_amount", 0, MAX_AMOUNT),  # 0 = no target (v0.4 §5.1)
    ),
    "bill_templates": (
        _text("name", 200, non_empty=True),
        _amount("default_amount"),
        _int("default_due_day", 1, 31),
        _bool("is_active"),
    ),
    "debts": (
        _text("name", 100, non_empty=True),
        _enum("kind", ("person", "loan")),
        _int("amount", 1, MAX_DEBT_AMOUNT),
        _date("started_on"),
        _sticky(_int("monthly_payment", 0, MAX_DEBT_AMOUNT)),
        _int("due_day", 1, 31),
        Field("first_month", "month"),
    ),
    "month_plans": (
        _int("year", 2000, 2100),
        _int("month", 1, 12),
        _int("buffer_amount", 0, MAX_AMOUNT),
        _int("start_day", 1, 31),
        _enum("plan_state", ("draft", "active", "closed")),
        _enum("rollover_decision", ("savings", "nextMonth", "released"), required=False, nullable=True),
        _sticky(Field("closed_days", "int_array", lo=1, hi=31)),
        _sticky(_int("rollover_amount", 0, MAX_AMOUNT)),
        _sticky(_bool("rollover_carried")),
        _sticky(_uuid("rollover_goal_id", nullable=True)),
        _sticky(_bool("summary_shown")),
        _sticky(_int("buffer_released_amount", 0, MAX_AMOUNT)),
        _sticky(Field("buffer_released_on", "date", nullable=True)),
        _sticky(Field("reserved_debt_ids", "uuid_array")),
        _sticky(Field("created_at", "timestamp", nullable=True)),
    ),
    "bills": (
        _uuid("plan_id"),
        _text("name", 200, non_empty=True),
        _amount("amount"),
        _int("due_day", 1, 31),
        _bool("is_paid"),
        _bool("is_one_off"),
        _uuid("template_id", required=False, nullable=True),
        _sticky(_bool("is_included")),
        _sticky(_text("exclude_reason", 50, nullable=True)),
        _sticky(_uuid("debt_id", nullable=True)),
    ),
    "expenses": (
        _uuid("plan_id"),
        _amount("amount"),
        _date("date"),
        _enum("tag", EXPENSE_TAGS),
        _text("note", 200, required=False, nullable=True),
    ),
    "income_events": (
        _uuid("plan_id"),
        _text("label", 100, non_empty=True),
        _amount("amount"),
        _date("date"),
        _enum("kind", ("primary", "extra")),
        _enum("status", ("expected", "received")),
        _enum("destination", ("spendable", "lockedSavings")),
        _uuid("savings_goal_id", required=False, nullable=True),
        _text("note", 200, required=False, nullable=True),
        _sticky(_bool("is_recurring")),
        _sticky(_bool("is_rollover")),
    ),
    "savings_allocations": (
        _uuid("plan_id"),
        _uuid("goal_id"),
        _amount("amount"),
    ),
    "loans": (
        _uuid("plan_id", required=False, nullable=True),
        _text("person", 100, non_empty=True),
        _amount("amount"),
        _date("lent_on"),
        Field("due_on", "date", required=False, nullable=True),
        Field("repaid_on", "date", required=False, nullable=True),
        _sticky(_bool("written_off")),
    ),
    "loan_repayments": (
        _uuid("loan_id"),
        _amount("amount"),
        _date("date"),
        _sticky(_bool("landed_as_income")),
    ),
    "debt_payments": (
        _uuid("debt_id", required=False, nullable=True),
        _uuid("plan_id", required=False, nullable=True),
        _amount("amount"),
        _date("date"),
    ),
    "category_limits": (
        _enum("tag", EXPENSE_TAGS),
        _amount("amount"),
    ),
}

BASE_COLUMNS = ("id", "updated_at", "device_id", "deleted_at")


def column_names(table: str) -> list[str]:
    return list(BASE_COLUMNS) + [f.name for f in TABLES[table]]


# ---- scalar parsers --------------------------------------------------------------

def parse_uuid(v, name: str) -> uuid.UUID:
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            pass
    raise Rejected(f"{name} must be a UUID string")


def parse_timestamp(v, name: str) -> datetime:
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            dt = None
        if dt is not None and dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
    raise Rejected(f"{name} must be an RFC 3339 timestamp with a timezone")


def parse_date(v, name: str) -> date:
    if isinstance(v, str) and _DATE_RE.match(v):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    raise Rejected(f"{name} must be a valid calendar date (YYYY-MM-DD)")


def _parse_field(f: Field, raw: dict):
    v = raw.get(f.name)
    if v is None:
        if f.nullable or not f.required:
            return None
        raise Rejected(f"{f.name} is required")
    if f.kind == "uuid":
        return parse_uuid(v, f.name)
    if f.kind == "int":
        # bool is an int subclass in Python; floats (even 5.0) are not integers on the wire.
        if isinstance(v, bool) or not isinstance(v, int) or not f.lo <= v <= f.hi:
            raise Rejected(f"{f.name} must be an integer between {f.lo} and {f.hi}")
        return v
    if f.kind == "bool":
        if not isinstance(v, bool):
            raise Rejected(f"{f.name} must be true or false")
        return v
    if f.kind == "enum":
        if v not in f.values:
            raise Rejected(f"{f.name} must be one of: {', '.join(f.values)}")
        return v
    if f.kind == "date":
        return parse_date(v, f.name)
    if f.kind == "timestamp":
        return parse_timestamp(v, f.name)
    if f.kind == "month":
        if not isinstance(v, str) or not _MONTH_RE.match(v):
            raise Rejected(f"{f.name} must be a month in YYYY-MM form")
        return v
    if f.kind == "int_array":
        if not isinstance(v, list) or any(
            isinstance(n, bool) or not isinstance(n, int) or not f.lo <= n <= f.hi for n in v
        ):
            raise Rejected(f"{f.name} must be a list of integers between {f.lo} and {f.hi}")
        return sorted(set(v))
    if f.kind == "uuid_array":
        if not isinstance(v, list) or len(v) > MAX_UUID_LIST:
            raise Rejected(f"{f.name} must be a list of at most {MAX_UUID_LIST} UUID strings")
        return list(dict.fromkeys(parse_uuid(item, f.name) for item in v))
    if f.kind == "text":
        if not isinstance(v, str) or len(v) > f.max_len or (f.non_empty and not v.strip()):
            need = "a non-empty string" if f.non_empty else "a string"
            raise Rejected(f"{f.name} must be {need} of at most {f.max_len} characters")
        return v
    raise AssertionError(f.kind)


# ---- record-level ----------------------------------------------------------------

def parse_base(raw, batch_device_id: str) -> dict:
    """The four sync columns — all a tombstone needs."""
    if not isinstance(raw, dict):
        raise Rejected("record must be a JSON object")
    device_id = raw.get("device_id", batch_device_id)
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 128:
        raise Rejected("device_id must be a string of 1-128 characters")
    deleted_at = raw.get("deleted_at")
    return {
        "id": parse_uuid(raw.get("id"), "id"),
        "updated_at": parse_timestamp(raw.get("updated_at"), "updated_at"),
        "device_id": device_id,
        "deleted_at": None if deleted_at is None else parse_timestamp(deleted_at, "deleted_at"),
    }


def validate_record(table: str, raw, batch_device_id: str) -> dict:
    """Full validation of a record. Raises Rejected with a value-free reason."""
    rec = parse_base(raw, batch_device_id)
    for f in TABLES[table]:
        if f.sticky:
            if f.name not in raw:
                continue  # absent: the column keeps its stored value (or default on insert)
            if raw[f.name] is None and not f.nullable:
                raise Rejected(f"{f.name} must not be null")
        rec[f.name] = _parse_field(f, raw)
    if table == "debts" and rec.get("monthly_payment", 0) > rec["amount"]:
        raise Rejected("monthly_payment must not exceed amount")
    if table == "income_events":
        locked = rec["destination"] == "lockedSavings"
        if locked and rec["savings_goal_id"] is None:
            raise Rejected("savings_goal_id is required when destination is lockedSavings")
        if not locked and rec["savings_goal_id"] is not None:
            raise Rejected("savings_goal_id is not allowed when destination is spendable")
    return rec


def validate_snapshot(raw, batch_device_id: str) -> dict:
    if not isinstance(raw, dict):
        raise Rejected("snapshot must be a JSON object")
    kind = raw.get("kind")
    if kind not in ("today", "insights"):
        raise Rejected("kind must be one of: today, insights")
    out = {"kind": kind}
    for name, lo, hi in (("plan_year", 2000, 2100), ("plan_month", 1, 12), ("snapshot_version", 1, 10**6)):
        out[name] = _parse_field(_int(name, lo, hi), raw)
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise Rejected("payload must be a JSON object")
    if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()) > 64 * 1024:
        raise Rejected("payload must be at most 64KB")
    out["payload"] = payload
    device_id = raw.get("device_id", batch_device_id)
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 128:
        raise Rejected("device_id must be a string of 1-128 characters")
    out["device_id"] = device_id
    if raw.get("updated_at") is not None:
        out["updated_at"] = parse_timestamp(raw["updated_at"], "updated_at")
    else:
        # The app's push example carries no envelope timestamp; fall back to the
        # engine's own as_of, then to server time.
        as_of = payload.get("as_of")
        try:
            out["updated_at"] = parse_timestamp(as_of, "as_of")
        except Rejected:
            out["updated_at"] = datetime.now(timezone.utc)
    return out


# ---- plan membership -------------------------------------------------------------
#
# Product spec §5.10 rule 3: a record belongs to a plan by ownership (plan_id), not by date.
# Plans are always calendar months and start_day is always 1 in v1, so the only date rule is
# on expenses: an expense must be dated inside its plan's calendar month. Income is exempt —
# salary dated Friday 30 Oct legitimately funds the November plan — and needs only a real date
# and an existing plan. Bills carry no date. start_day is stored but never used in validation.

def date_in_plan(d: date, year: int, month: int) -> bool:
    return d.year == year and d.month == month
