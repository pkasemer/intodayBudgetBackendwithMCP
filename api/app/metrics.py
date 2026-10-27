"""Usage counters and the numbers behind GET /v1/stats.

Counts are accumulated in memory and flushed to Postgres every few seconds (and on shutdown),
so a request never waits on a metrics write. Reads add the not-yet-flushed amounts, so the
numbers are live. Only counts and timestamps are stored — never request contents.
"""
import logging
from collections import Counter
from datetime import date, datetime, timezone

from psycopg import sql

from . import sync
from .logs import log

API, MCP = "api", "mcp"


class Usage:
    def __init__(self, tz):
        self.tz = tz
        self.counts: Counter = Counter()  # (day, bucket) -> n
        self.last: dict = {}  # kind -> (datetime, device_id | None)

    def bump(self, bucket: str, n: int = 1) -> None:
        self.counts[(datetime.now(self.tz).date(), bucket)] += n

    def touch(self, kind: str, device_id: str | None = None) -> None:
        self.last[kind] = (datetime.now(timezone.utc), device_id)

    def reset(self) -> None:
        self.counts, self.last = Counter(), {}

    async def flush(self, pool) -> None:
        counts, last = self.counts, self.last
        if not counts and not last:
            return
        self.counts, self.last = Counter(), {}
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    for (day, bucket), n in counts.items():
                        await conn.execute(
                            "INSERT INTO usage_daily (day, bucket, count) VALUES (%s, %s, %s) "
                            "ON CONFLICT (day, bucket) DO UPDATE SET count = usage_daily.count + EXCLUDED.count",
                            (day, bucket, n),
                        )
                    for kind, (at, device_id) in last.items():
                        await conn.execute(
                            "INSERT INTO sync_status (kind, at, device_id) VALUES (%s, %s, %s) "
                            "ON CONFLICT (kind) DO UPDATE SET at = EXCLUDED.at, device_id = EXCLUDED.device_id "
                            "WHERE EXCLUDED.at > sync_status.at",
                            (kind, at, device_id),
                        )
        except Exception as exc:  # keep the counts for the next attempt
            for key, n in counts.items():
                self.counts[key] += n
            for kind, value in last.items():
                if kind not in self.last or self.last[kind][0] < value[0]:
                    self.last[kind] = value
            log("usage_flush_failed", logging.WARNING, error=type(exc).__name__)


async def collect(pool, usage: Usage) -> dict:
    today = datetime.now(usage.tz).date()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ ONLY")
            cur = await conn.execute("SELECT day, bucket, count FROM usage_daily")
            daily = await cur.fetchall()
            cur = await conn.execute("SELECT kind, at, device_id FROM sync_status")
            statuses = {r["kind"]: (r["at"], r["device_id"]) for r in await cur.fetchall()}
            by_table = {}
            for table in sync.SYNC_TABLES:
                cur = await conn.execute(
                    sql.SQL("SELECT COUNT(*) AS n FROM {t} WHERE deleted_at IS NULL").format(t=sql.Identifier(table))
                )
                by_table[table] = (await cur.fetchone())["n"]
            cur = await conn.execute("SELECT COUNT(*) AS n FROM snapshots")
            snapshots = (await cur.fetchone())["n"]
            parts = [sql.SQL("SELECT device_id FROM {t}").format(t=sql.Identifier(t)) for t in sync.ALL_SEQ_TABLES]
            cur = await conn.execute(
                sql.SQL("SELECT COUNT(DISTINCT device_id) AS n FROM ({}) AS d WHERE device_id <> %s").format(
                    sql.SQL(" UNION ALL ").join(parts)
                ),
                (sync.MCP_DEVICE,),
            )
            devices = (await cur.fetchone())["n"]

    totals: Counter = Counter()
    today_counts: Counter = Counter()
    first_day: date | None = None
    for row in daily:
        totals[row["bucket"]] += row["count"]
        if row["day"] == today:
            today_counts[row["bucket"]] += row["count"]
        first_day = row["day"] if first_day is None else min(first_day, row["day"])
    for (day, bucket), n in usage.counts.items():  # not yet flushed
        totals[bucket] += n
        if day == today:
            today_counts[bucket] += n
        first_day = day if first_day is None else min(first_day, day)
    for kind, value in usage.last.items():
        if kind not in statuses or statuses[kind][0] < value[0]:
            statuses[kind] = value

    def stamp(kind):
        return sync.ser_value(statuses[kind][0]) if kind in statuses else None

    latest = max(statuses, key=lambda k: statuses[k][0]) if statuses else None
    return {
        "generated_at": sync.ser_value(datetime.now(timezone.utc)),
        "counting_since": None if first_day is None else first_day.isoformat(),
        "api_calls": {"today": today_counts[API], "total": totals[API]},
        "mcp_calls": {"today": today_counts[MCP], "total": totals[MCP]},
        "records": {"total": sum(by_table.values()), "by_table": by_table, "snapshots": snapshots},
        "devices": devices,
        "last_sync": {
            "at": None if latest is None else stamp(latest),
            "kind": latest,
            "last_push": stamp("push"),
            "last_pull": stamp("pull"),
            "last_snapshot": stamp("snapshot"),
        },
    }
