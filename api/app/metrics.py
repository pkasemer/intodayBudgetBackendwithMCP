"""Usage counters and the numbers behind GET /v1/stats.

Counts and activity events are accumulated in memory and flushed to Postgres every few seconds
(and on shutdown), so a request never waits on a metrics write. Reads add the not-yet-flushed
amounts, so the numbers are live. Only counts, timestamps, tool names and record IDs are stored —
never request contents.
"""
import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from psycopg import sql
from psycopg.types.json import Jsonb

from . import sync
from .logs import log

API, MCP = "api", "mcp"
MAX_PENDING_EVENTS = 500
MAX_PENDING_NOISE = 25  # denied/failed events per flush window: a brute-force burst must not flood the feed
NOISE = ("denied", "failed")
FEED_ROWS = 200
FEED_SHOWN = 40
DAYS_SHOWN = 14
LATENCY_KINDS = ("push", "pull", "snapshot", "mcp")


class Usage:
    def __init__(self, tz):
        self.tz = tz
        self.counts: Counter = Counter()  # (day, bucket) -> n
        self.last: dict = {}  # kind -> (datetime, device_id | None)
        self.statuses: Counter = Counter()  # (day, http status) -> n
        self.events: list[dict] = []  # activity not yet written to Postgres
        self.started_at = datetime.now(timezone.utc)
        self._started = time.monotonic()

    def bump(self, bucket: str, n: int = 1) -> None:
        self.counts[(datetime.now(self.tz).date(), bucket)] += n

    def touch(self, kind: str, device_id: str | None = None) -> None:
        self.last[kind] = (datetime.now(timezone.utc), device_id)

    def status(self, code: int) -> None:
        self.statuses[(datetime.now(self.tz).date(), code)] += 1

    def event(self, kind: str, status: int = 200, device_id: str | None = None, ms: float | None = None,
              **detail) -> None:
        if len(self.events) >= MAX_PENDING_EVENTS:
            return
        if kind in NOISE and sum(1 for e in self.events if e["kind"] == kind) >= MAX_PENDING_NOISE:
            return
        self.events.append({"at": datetime.now(timezone.utc), "kind": kind, "device_id": device_id,
                            "status": status, "ms": None if ms is None else round(ms, 1), "detail": detail})

    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._started)

    def reset(self) -> None:
        self.counts, self.last, self.statuses, self.events = Counter(), {}, Counter(), []

    async def flush(self, pool) -> None:
        counts, last, statuses, events = self.counts, self.last, self.statuses, self.events
        if not (counts or last or statuses or events):
            return
        self.counts, self.last, self.statuses, self.events = Counter(), {}, Counter(), []
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    for (day, code), n in statuses.items():
                        await conn.execute(
                            "INSERT INTO usage_status (day, status, count) VALUES (%s, %s, %s) "
                            "ON CONFLICT (day, status) DO UPDATE SET count = usage_status.count + EXCLUDED.count",
                            (day, code, n),
                        )
                    for e in events:
                        await conn.execute(
                            "INSERT INTO activity (at, kind, device_id, status, ms, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                            (e["at"], e["kind"], e["device_id"], e["status"], e["ms"], Jsonb(e["detail"])),
                        )
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
        except Exception as exc:  # keep everything for the next attempt
            for key, n in counts.items():
                self.counts[key] += n
            for key, n in statuses.items():
                self.statuses[key] += n
            self.events = events + self.events
            for kind, value in last.items():
                if kind not in self.last or self.last[kind][0] < value[0]:
                    self.last[kind] = value
            log("usage_flush_failed", logging.WARNING, error=type(exc).__name__)


def _percentile(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def _status_class(code: int) -> str:
    if code == 401:
        return "unauthorized"
    if code == 429:
        return "rate_limited"
    if code >= 500:
        return "server_error"
    return "client_error" if code >= 400 else "ok"


def _event_out(e: dict) -> dict:
    return {"at": sync.ser_value(e["at"]), "kind": e["kind"], "device_id": e["device_id"],
            "status": e["status"], "ms": e["ms"], "detail": e["detail"]}


async def collect(pool, usage: Usage, redis=None) -> dict:
    now = datetime.now(timezone.utc)
    today = datetime.now(usage.tz).date()
    window_start = today - timedelta(days=DAYS_SHOWN - 1)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ ONLY")
            cur = await conn.execute("SELECT day, bucket, count FROM usage_daily")
            daily = await cur.fetchall()
            cur = await conn.execute("SELECT kind, at, device_id FROM sync_status")
            statuses = {r["kind"]: (r["at"], r["device_id"]) for r in await cur.fetchall()}
            by_table, removed = {}, {}
            for table in sync.SYNC_TABLES:
                cur = await conn.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FILTER (WHERE deleted_at IS NULL) AS live, "
                        "COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) AS gone FROM {t}"
                    ).format(t=sql.Identifier(table))
                )
                row = await cur.fetchone()
                by_table[table], removed[table] = row["live"], row["gone"]
            cur = await conn.execute("SELECT COUNT(*) AS n FROM snapshots")
            snapshots = (await cur.fetchone())["n"]
            parts = [sql.SQL("SELECT device_id FROM {t}").format(t=sql.Identifier(t)) for t in sync.ALL_SEQ_TABLES]
            cur = await conn.execute(
                sql.SQL("SELECT device_id, COUNT(*) AS n FROM ({}) AS d GROUP BY device_id ORDER BY n DESC").format(
                    sql.SQL(" UNION ALL ").join(parts)
                )
            )
            writers = await cur.fetchall()
            cur = await conn.execute("SELECT day, status, count FROM usage_status WHERE day >= %s", (window_start,))
            status_rows = await cur.fetchall()
            cur = await conn.execute(
                "SELECT at, kind, device_id, status, ms, detail FROM activity ORDER BY at DESC, id DESC LIMIT %s",
                (FEED_ROWS,),
            )
            feed = await cur.fetchall()
            cur = await conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
            migration = await cur.fetchone()
            cur = await conn.execute(
                "SELECT year, month FROM month_plans WHERE deleted_at IS NULL ORDER BY year DESC, month DESC"
            )
            months = [f"{r['year']}-{r['month']:02d}" for r in await cur.fetchall()]

    redis_state = "unknown"
    if redis is not None:
        try:
            await redis.ping()
            redis_state = "up"
        except Exception:
            redis_state = "down"

    # ---- call counters ---------------------------------------------------------------
    totals: Counter = Counter()
    per_day: dict = {}
    first_day: date | None = None
    for row in daily:
        per_day[(row["day"], row["bucket"])] = row["count"]
        totals[row["bucket"]] += row["count"]
        first_day = row["day"] if first_day is None else min(first_day, row["day"])
    for (day, bucket), n in usage.counts.items():  # not yet flushed
        per_day[(day, bucket)] = per_day.get((day, bucket), 0) + n
        totals[bucket] += n
        first_day = day if first_day is None else min(first_day, day)
    today_counts = {b: per_day.get((today, b), 0) for b in (API, MCP)}
    days = []
    for offset in range(DAYS_SHOWN):
        d = window_start + timedelta(days=offset)
        days.append({"day": d.isoformat(), "api": per_day.get((d, API), 0), "mcp": per_day.get((d, MCP), 0)})

    for kind, value in usage.last.items():
        if kind not in statuses or statuses[kind][0] < value[0]:
            statuses[kind] = value

    def stamp(kind):
        return sync.ser_value(statuses[kind][0]) if kind in statuses else None

    latest = max(statuses, key=lambda k: statuses[k][0]) if statuses else None

    # ---- responses -------------------------------------------------------------------
    status_by_day: Counter = Counter()
    for row in status_rows:
        status_by_day[(row["day"], row["status"])] += row["count"]
    for (day, code), n in usage.statuses.items():
        status_by_day[(day, code)] += n
    classes = ("ok", "unauthorized", "rate_limited", "client_error", "server_error")
    responses_today = {c: 0 for c in classes}
    responses_window = {c: 0 for c in classes}
    for (day, code), n in status_by_day.items():
        if day >= window_start:
            responses_window[_status_class(code)] += n
        if day == today:
            responses_today[_status_class(code)] += n

    # ---- activity ----------------------------------------------------------------------
    events = sorted(usage.events + [dict(r) for r in feed], key=lambda e: e["at"], reverse=True)
    latency = {}
    for kind in LATENCY_KINDS:
        values = sorted(e["ms"] for e in events if e["kind"] == kind and e["ms"] is not None)
        if values:
            latency[kind] = {"n": len(values), "p50": values[len(values) // 2], "p95": _percentile(values, 0.95)}
    day_ago = now - timedelta(hours=24)
    rejections, rejected_24h = [], 0
    for e in events:
        if e["kind"] != "push":
            continue
        if e["at"] >= day_ago:
            rejected_24h += e["detail"].get("rejected", 0)
        for r in e["detail"].get("rejects", []):
            if len(rejections) < 20:
                rejections.append({"at": sync.ser_value(e["at"]), "device_id": e["device_id"], **r})
    last_push_by_device: dict = {}
    for e in events:
        if e["kind"] == "push" and e["device_id"] and e["device_id"] not in last_push_by_device:
            last_push_by_device[e["device_id"]] = sync.ser_value(e["at"])
    devices_out = [
        {"device_id": w["device_id"], "rows": w["n"], "is_mcp": w["device_id"] == sync.MCP_DEVICE,
         "last_push": last_push_by_device.get(w["device_id"])}
        for w in writers
    ]

    # ---- things worth a glance -------------------------------------------------------------
    alerts = []
    if redis_state == "down":
        alerts.append({"level": "bad", "text": "Redis is unreachable: rate limiting is off, the API still works."})
    if responses_today["server_error"]:
        alerts.append({"level": "bad", "text": f"{responses_today['server_error']} server error(s) today."})
    if rejected_24h:
        alerts.append({"level": "warn", "text": f"{rejected_24h} record(s) rejected by the server in the last 24 hours."})
    if responses_today["rate_limited"]:
        alerts.append({"level": "warn", "text": f"{responses_today['rate_limited']} request(s) were rate limited today."})
    if responses_today["unauthorized"] >= 10:
        alerts.append({"level": "warn", "text": f"{responses_today['unauthorized']} failed sign-ins today."})
    synced_devices = sum(1 for w in writers if w["device_id"] != sync.MCP_DEVICE)
    if latest and synced_devices and (now - statuses[latest][0]) > timedelta(hours=24):
        alerts.append({"level": "warn", "text": "No sync from the app for more than a day."})

    return {
        "generated_at": sync.ser_value(now),
        "counting_since": None if first_day is None else first_day.isoformat(),
        "api_calls": {"today": today_counts[API], "total": totals[API]},
        "mcp_calls": {"today": today_counts[MCP], "total": totals[MCP]},
        "records": {"total": sum(by_table.values()), "by_table": by_table, "removed_by_table": removed,
                    "snapshots": snapshots},
        "devices": synced_devices,
        "last_sync": {
            "at": None if latest is None else stamp(latest),
            "kind": latest,
            "last_push": stamp("push"),
            "last_pull": stamp("pull"),
            "last_snapshot": stamp("snapshot"),
        },
        "health": {
            "database": "up", "redis": redis_state, "uptime_seconds": usage.uptime_seconds(),
            "started_at": sync.ser_value(usage.started_at),
            "schema_version": sync.SCHEMA_VERSION, "migration": migration["version"] if migration else None,
        },
        "months": months,
        "days": days,
        "responses": {"today": responses_today, "window": responses_window, "window_days": DAYS_SHOWN},
        "latency": latency,
        "devices_seen": devices_out,
        "recent": [_event_out(e) for e in events[:FEED_SHOWN]],
        "rejections": rejections,
        "alerts": alerts,
    }
