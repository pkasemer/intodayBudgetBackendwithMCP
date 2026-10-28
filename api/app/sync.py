"""Sync core: LWW record application, push, pull, full snapshot, erase (backend spec §5).

Everything here runs inside a caller-supplied transaction on a pooled connection.
"""
import base64
import logging
import uuid
from datetime import date, datetime, timezone

import psycopg
from psycopg import errors as pgerr
from psycopg import sql
from psycopg.types.json import Jsonb

from . import validation as v
from .errors import validation_error
from .logs import log
from .validation import Rejected

SYNC_TABLES = tuple(v.TABLES)  # dependency order: parents first
ALL_SEQ_TABLES = SYNC_TABLES + ("snapshots",)
MCP_DEVICE = "mcp"
SCHEMA_VERSION = 4  # GET /v1/sync/capabilities; 3 was the v0.3 server (no such endpoint)


# ---- serialisation ---------------------------------------------------------------

def ser_value(value):
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc)
        text = value.replace(tzinfo=None).isoformat(timespec="microseconds" if value.microsecond else "seconds")
        return text + "Z"
    if isinstance(value, (date, uuid.UUID)):
        return value.isoformat() if isinstance(value, date) else str(value)
    if isinstance(value, list):  # closed_days, reserved_debt_ids
        return [ser_value(item) for item in value]
    return value


def ser_row(row: dict) -> dict:
    return {k: ser_value(val) for k, val in row.items() if k != "seq"}


# ---- cursors ---------------------------------------------------------------------

def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"s1:{seq}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        prefix, _, number = text.partition(":")
        seq = int(number)
        if prefix == "s1" and seq >= 0:
            return seq
    except (ValueError, UnicodeDecodeError):
        pass
    raise validation_error("cursor is not valid")


async def current_max_seq(conn) -> int:
    cur = await conn.execute("SELECT last_value, is_called FROM sync_seq")
    row = await cur.fetchone()
    return row["last_value"] if row["is_called"] else 0


async def safe_cursor(conn, device_id: str, start_seq: int) -> int:
    """Furthest position the pushing device can safely resume pulling from.

    Walks forward from `start_seq` over rows this device wrote itself, and stops just
    before the first row written by anyone else (e.g. an MCP write) — so a push can never
    cause the device to skip a foreign write. Computed before the push applies, so the
    batch's own rows are echoed back by the next pull (harmless: LWW ignores them).
    """
    parts = [
        sql.SQL("SELECT seq FROM {t} WHERE seq > %s AND device_id <> %s").format(t=sql.Identifier(t))
        for t in ALL_SEQ_TABLES
    ]
    query = sql.SQL("SELECT MIN(seq) AS first_foreign FROM ({}) AS rows").format(sql.SQL(" UNION ALL ").join(parts))
    params = [start_seq, device_id] * len(ALL_SEQ_TABLES)
    cur = await conn.execute(query, params)
    first_foreign = (await cur.fetchone())["first_foreign"]
    if first_foreign is not None:
        return max(first_foreign - 1, start_seq)
    return max(await current_max_seq(conn), start_seq)


# ---- plan / goal lookups ---------------------------------------------------------

async def fetch_plan(conn, plan_id):
    cur = await conn.execute(
        "SELECT id, year, month, deleted_at FROM month_plans WHERE id = %s", (plan_id,)
    )
    return await cur.fetchone()


async def plan_by_month(conn, year: int, month: int):
    cur = await conn.execute(
        "SELECT * FROM month_plans WHERE year = %s AND month = %s AND deleted_at IS NULL", (year, month)
    )
    return await cur.fetchone()


async def plan_for_date(conn, d: date):
    """The live plan whose calendar month holds `d` (used by MCP writes, which carry no plan_id)."""
    return await plan_by_month(conn, d.year, d.month)


# The column that points at a savings goal, per table.
_GOAL_REF = {"income_events": "savings_goal_id", "savings_allocations": "goal_id", "month_plans": "rollover_goal_id"}


async def _check_refs(conn, table: str, rec: dict) -> None:
    """Cross-record rules from §7.3 (and v0.4 §5.3) that need the database.

    bills.debt_id and debt_payments.debt_id are deliberately not checked: the debt may arrive
    on a later page of the same split push, so the id is stored as sent."""
    tombstone = rec["deleted_at"] is not None
    if rec.get("plan_id") is not None:
        plan = await fetch_plan(conn, rec["plan_id"])
        if plan is None:
            raise Rejected("plan_id does not match a stored plan")
        # A loan outlives its plan (a deleted plan only detaches it), so only loans may name a deleted one.
        if plan["deleted_at"] is not None and not tombstone and table != "loans":
            raise Rejected("plan_id refers to a deleted plan")
        # Income is exempt: it belongs to a plan by ownership, not by its landing date (§5.10).
        if table == "expenses" and not tombstone and not v.date_in_plan(rec["date"], plan["year"], plan["month"]):
            raise Rejected("date falls outside the plan's month")
    if table == "loan_repayments":
        cur = await conn.execute("SELECT deleted_at FROM loans WHERE id = %s", (rec["loan_id"],))
        loan = await cur.fetchone()
        if loan is None:
            raise Rejected("loan_id does not match a stored loan")
        if loan["deleted_at"] is not None and not tombstone:
            raise Rejected("loan_id refers to a deleted loan")
    goal_id = rec.get(_GOAL_REF.get(table, ""))
    if goal_id is not None:
        cur = await conn.execute("SELECT deleted_at FROM savings_goals WHERE id = %s", (goal_id,))
        goal = await cur.fetchone()
        if goal is None:
            raise Rejected("savings goal does not exist")
        # A closed plan may keep naming a goal deleted since; only a hard delete clears it.
        if goal["deleted_at"] is not None and not tombstone and table != "month_plans":
            raise Rejected("savings goal is deleted")


# ---- last-write-wins -------------------------------------------------------------

# The tiebreak uses the C collation so the winner doesn't depend on database locale.
_LWW_UPSERT = (
    '(EXCLUDED.updated_at > {t}.updated_at OR (EXCLUDED.updated_at = {t}.updated_at '
    'AND EXCLUDED.device_id COLLATE "C" > {t}.device_id COLLATE "C"))'
)
_LWW_UPDATE = (
    '(%s > updated_at OR (%s = updated_at AND %s COLLATE "C" > device_id COLLATE "C"))'
)

_REASONS = {
    pgerr.UniqueViolation: "conflicts with an existing record",
    pgerr.ForeignKeyViolation: "references a record that does not exist",
    pgerr.CheckViolation: "violates a data constraint",
    pgerr.NotNullViolation: "is missing a required value",
}


def _db_reason(exc: psycopg.Error) -> str:
    for cls, reason in _REASONS.items():
        if isinstance(exc, cls):
            return reason
    raise exc


async def _insert_lww(conn, table: str, rec: dict) -> bool:
    # Columns the client did not send are left out of both the INSERT (column default applies)
    # and the UPDATE (stored value kept) — v0.4 §2.3.
    cols = [c for c in v.column_names(table) if c in rec]
    ident = sql.Identifier(table)
    assignments = sql.SQL(", ").join(
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in cols if c != "id"
    )
    query = sql.SQL(
        "INSERT INTO {t} ({cols}) VALUES ({vals}) ON CONFLICT (id) DO UPDATE SET {sets} WHERE "
        + _LWW_UPSERT + " RETURNING id"
    ).format(
        t=ident,
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        sets=assignments,
    )
    try:
        async with conn.transaction():  # savepoint: a bad record must not poison the batch
            cur = await conn.execute(query, [rec[c] for c in cols])
            return await cur.fetchone() is not None
    except psycopg.Error as exc:
        raise Rejected(_db_reason(exc))


async def _tombstone_lww(conn, table: str, base: dict) -> bool:
    query = sql.SQL(
        "UPDATE {t} SET deleted_at = %s, updated_at = %s, device_id = %s WHERE id = %s AND " + _LWW_UPDATE
    ).format(t=sql.Identifier(table))
    u, d = base["updated_at"], base["device_id"]
    cur = await conn.execute(query, (base["deleted_at"], u, d, base["id"], u, u, d))
    return cur.rowcount == 1


async def apply_record(conn, table: str, raw, batch_device_id: str) -> str:
    """Apply one record under LWW. Returns 'applied' or 'skipped'; raises Rejected."""
    base = v.parse_base(raw, batch_device_id)
    if base["deleted_at"] is not None:
        # A delete only needs the sync columns; the rest of the row is already stored.
        if await _tombstone_lww(conn, table, base):
            return "applied"
        cur = await conn.execute(sql.SQL("SELECT 1 FROM {t} WHERE id = %s").format(t=sql.Identifier(table)), (base["id"],))
        if await cur.fetchone():
            return "skipped"  # stale delete lost to a newer write
        try:
            rec = v.validate_record(table, raw, batch_device_id)
        except Rejected:
            return "skipped"  # delete of something we never had, and no body to store
    else:
        rec = v.validate_record(table, raw, batch_device_id)
    await _check_refs(conn, table, rec)
    return "applied" if await _insert_lww(conn, table, rec) else "skipped"


async def apply_snapshot(conn, raw, batch_device_id: str) -> str:
    snap = v.validate_snapshot(raw, batch_device_id)
    query = sql.SQL(
        "INSERT INTO snapshots (kind, plan_year, plan_month, payload, snapshot_version, updated_at, device_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (kind, plan_year, plan_month) DO UPDATE SET "
        "payload = EXCLUDED.payload, snapshot_version = EXCLUDED.snapshot_version, "
        "updated_at = EXCLUDED.updated_at, device_id = EXCLUDED.device_id WHERE "
        + _LWW_UPSERT.format(t="snapshots") + " RETURNING id"
    )
    async with conn.transaction():
        cur = await conn.execute(
            query,
            (snap["kind"], snap["plan_year"], snap["plan_month"], Jsonb(snap["payload"]),
             snap["snapshot_version"], snap["updated_at"], snap["device_id"]),
        )
        return "applied" if await cur.fetchone() else "skipped"


# ---- push ------------------------------------------------------------------------

def parse_push_envelope(body, header_key: str | None, max_records: int):
    if not isinstance(body, dict):
        raise validation_error("request body must be a JSON object")
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 128:
        raise validation_error("device_id is required (a string of 1-128 characters)")
    records = body.get("records", {})
    if not isinstance(records, dict):
        raise validation_error("records must be an object keyed by table name")
    total = 0
    for table in SYNC_TABLES:
        items = records.get(table, [])
        if not isinstance(items, list):
            raise validation_error(f"records.{table} must be a list")
        total += len(items)
    snapshots = body.get("snapshots", [])
    if not isinstance(snapshots, list):
        raise validation_error("snapshots must be a list")
    if total + len(snapshots) > max_records:
        raise validation_error(f"a push may carry at most {max_records} records")
    body_key = body.get("idempotency_key")
    if body_key is not None and not isinstance(body_key, str):
        raise validation_error("idempotency_key must be a string")
    if header_key and body_key and header_key != body_key:
        raise validation_error("Idempotency-Key header and idempotency_key body field disagree")
    key = header_key or body_key
    if key is not None and not 1 <= len(key) <= 128:
        raise validation_error("idempotency key must be 1-128 characters")
    start_seq = decode_cursor(body["cursor"]) if body.get("cursor") else 0
    return device_id, records, snapshots, key, start_seq


async def apply_push(conn, device_id: str, records: dict, snapshots: list, start_seq: int) -> dict:
    cursor = encode_cursor(await safe_cursor(conn, device_id, start_seq))
    applied = {t: 0 for t in SYNC_TABLES + ("snapshots",)}
    skipped = dict(applied)
    rejected: list[dict] = []
    loans_touched: set[str] = set()
    for table in SYNC_TABLES:
        for raw in records.get(table, []):
            rec_id = raw.get("id") if isinstance(raw, dict) and isinstance(raw.get("id"), str) else None
            try:
                outcome = await apply_record(conn, table, raw, device_id)
            except Rejected as exc:
                rejected.append({"table": table, "id": rec_id, "reason": exc.reason})
                continue
            (applied if outcome == "applied" else skipped)[table] += 1
            if outcome == "applied" and table in ("loans", "loan_repayments") and raw.get("deleted_at") is None:
                loans_touched.add(raw["id"] if table == "loans" else raw["loan_id"])
    await _log_overpaid_loans(conn, loans_touched)
    for raw in snapshots:
        label = None
        if isinstance(raw, dict):
            label = f"{raw.get('kind')}/{raw.get('plan_year')}/{raw.get('plan_month')}"
        try:
            outcome = await apply_snapshot(conn, raw, device_id)
        except Rejected as exc:
            rejected.append({"table": "snapshots", "id": label, "reason": exc.reason})
            continue
        (applied if outcome == "applied" else skipped)["snapshots"] += 1
    log(
        "sync_push",
        device_id=device_id,
        applied=sum(applied.values()),
        skipped=sum(skipped.values()),
        rejected=[{"table": r["table"], "id": r["id"]} for r in rejected],
    )
    return {"applied": applied, "skipped": skipped, "rejected": rejected, "cursor": cursor}


async def _log_overpaid_loans(conn, loan_ids: set[str]) -> None:
    """Repayments summing past the loan are a client bug (the app clamps): log, never reject (v0.4 §4.2)."""
    if not loan_ids:
        return
    cur = await conn.execute(
        "SELECT l.id FROM loans l JOIN (SELECT loan_id, SUM(amount) AS repaid FROM loan_repayments "
        "WHERE deleted_at IS NULL AND loan_id = ANY(%s) GROUP BY loan_id) r ON r.loan_id = l.id "
        "WHERE l.deleted_at IS NULL AND r.repaid > l.amount",
        ([uuid.UUID(i) for i in loan_ids],),
    )
    for row in await cur.fetchall():
        log("loan_overpaid", logging.WARNING, loan_id=str(row["id"]))


# ---- pull / snapshot -------------------------------------------------------------

async def pull_page(conn, seq: int, limit: int) -> dict:
    items = []  # (seq, kind, table, row)
    for table in ALL_SEQ_TABLES:
        cur = await conn.execute(
            sql.SQL("SELECT * FROM {t} WHERE seq > %s ORDER BY seq LIMIT %s").format(t=sql.Identifier(table)),
            (seq, limit + 1),
        )
        items.extend((row["seq"], table, row) for row in await cur.fetchall())
    items.sort(key=lambda item: item[0])
    page = items[:limit]
    has_more = len(items) > limit
    records = {t: [] for t in SYNC_TABLES}
    deleted, snapshots = [], []
    for _, table, row in page:
        if table == "snapshots":
            snapshots.append(_ser_snapshot(row))
        elif row["deleted_at"] is not None:
            deleted.append({"table": table, **{k: ser_value(row[k]) for k in ("id", "deleted_at", "updated_at", "device_id")}})
        else:
            records[table].append(ser_row(row))
    next_seq = page[-1][0] if page else seq
    return {
        "cursor": encode_cursor(next_seq),
        "has_more": has_more,
        "records": records,
        "deleted": deleted,
        "snapshots": snapshots,
    }


def _ser_snapshot(row: dict) -> dict:
    out = ser_row(row)
    out.pop("id", None)
    return out


async def full_snapshot(conn) -> dict:
    """Every live record plus live snapshots, consistent as of one point in time."""
    await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    cursor = encode_cursor(await _visible_max_seq(conn))
    records = {}
    for table in SYNC_TABLES:
        cur = await conn.execute(
            sql.SQL("SELECT * FROM {t} WHERE deleted_at IS NULL ORDER BY seq").format(t=sql.Identifier(table))
        )
        records[table] = [ser_row(r) for r in await cur.fetchall()]
    cur = await conn.execute("SELECT * FROM snapshots ORDER BY seq")
    snapshots = [_ser_snapshot(r) for r in await cur.fetchall()]
    return {"cursor": cursor, "has_more": False, "records": records, "snapshots": snapshots}


async def _visible_max_seq(conn) -> int:
    # Max over rows visible to this transaction's snapshot (the sequence itself is not MVCC).
    parts = [sql.SQL("SELECT MAX(seq) AS m FROM {t}").format(t=sql.Identifier(t)) for t in ALL_SEQ_TABLES]
    cur = await conn.execute(sql.SQL("SELECT MAX(m) AS m FROM ({}) AS x").format(sql.SQL(" UNION ALL ").join(parts)))
    return (await cur.fetchone())["m"] or 0


# ---- erase -----------------------------------------------------------------------

async def erase_all(conn) -> None:
    """Delete every row in every table. The seq sequence is deliberately not reset, so
    cursors issued before the erase stay valid and simply return nothing."""
    tables = sql.SQL(", ").join(sql.Identifier(t) for t in ALL_SEQ_TABLES + ("idempotency_keys", "usage_daily", "sync_status"))
    await conn.execute(sql.SQL("TRUNCATE {} CASCADE").format(tables))
