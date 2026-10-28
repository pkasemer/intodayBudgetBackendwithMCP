"""Retention: tombstones live 30 days, idempotency keys 7 (backend spec §4)."""
from datetime import datetime, timedelta, timezone

from .db import write_lock
from .logs import log

TOMBSTONE_DAYS = 30
IDEMPOTENCY_DAYS = 7

# Loans and debts are childless here: deleting a loan cascades its repayments, and deleting a debt
# detaches (nulls) the bills and payments that name it.
_CHILDREN = (
    "savings_allocations", "income_events", "expenses", "bills", "bill_templates",
    "loan_repayments", "debt_payments", "category_limits", "loans", "debts",
)


async def purge(pool, now: datetime | None = None) -> dict:
    """Hard-delete expired tombstones and idempotency keys. `now` is injectable so tests
    can drive the clock. Parents are only purged once nothing references them."""
    now = now or datetime.now(timezone.utc)
    tomb_cutoff = now - timedelta(days=TOMBSTONE_DAYS)
    idem_cutoff = now - timedelta(days=IDEMPOTENCY_DAYS)
    counts: dict[str, int] = {}
    async with pool.connection() as conn:
        async with conn.transaction():
            await write_lock(conn)
            for table in _CHILDREN:
                cur = await conn.execute(f"DELETE FROM {table} WHERE deleted_at < %s", (tomb_cutoff,))
                counts[table] = cur.rowcount
            cur = await conn.execute(
                "DELETE FROM savings_goals g WHERE g.deleted_at < %s "
                "AND NOT EXISTS (SELECT 1 FROM income_events e WHERE e.savings_goal_id = g.id) "
                "AND NOT EXISTS (SELECT 1 FROM savings_allocations a WHERE a.goal_id = g.id)",
                (tomb_cutoff,),
            )
            counts["savings_goals"] = cur.rowcount
            cur = await conn.execute(
                "DELETE FROM month_plans p WHERE p.deleted_at < %s "
                "AND NOT EXISTS (SELECT 1 FROM bills x WHERE x.plan_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM expenses x WHERE x.plan_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM income_events x WHERE x.plan_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM savings_allocations x WHERE x.plan_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM debt_payments x WHERE x.plan_id = p.id)",
                (tomb_cutoff,),
            )
            counts["month_plans"] = cur.rowcount
            cur = await conn.execute("DELETE FROM idempotency_keys WHERE created_at < %s", (idem_cutoff,))
            counts["idempotency_keys"] = cur.rowcount
    if any(counts.values()):
        log("purge", **counts)
    return counts
