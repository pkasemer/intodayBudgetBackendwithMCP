import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import dashboard, export, mcp, metrics, sync
from .config import Settings, load_settings
from .db import open_pool, run_migrations, write_lock
from .errors import ApiError, error_body, validation_error
from .guards import guard, token_digest
from .logs import log, setup_logging
from .purge import purge

IDEM_TTL_SECONDS = 24 * 3600
USAGE_FLUSH_SECONDS = 30

_STATIC = Path(__file__).parent / "static"
# Source Serif 4 (SIL OFL, see static/fonts/OFL.txt), embedded as data: URLs so the page needs no other request.
_FONT_FACES = (("400-normal", 400, "normal"), ("400-italic", 400, "italic"), ("600-normal", 600, "normal"))


def _font_css() -> str:
    return "\n".join(
        '  @font-face { font-family: "Source Serif 4"; font-style: %s; font-weight: %d; font-display: swap; '
        'src: url(data:font/woff2;base64,%s) format("woff2"); }'
        % (style, weight, base64.b64encode((_STATIC / "fonts" / f"source-serif-4-latin-{name}.woff2").read_bytes()).decode())
        for name, weight, style in _FONT_FACES
    )


_INDEX_HTML = (_STATIC / "index.html").read_text().replace("/*FONTS*/", _font_css())
_SCRIPT = re.search(r"<script>(.*?)</script>", _INDEX_HTML, re.S).group(1)
# The home page is static and carries no data. The CSP allows only its own inline style, its one
# inline script (by hash), its own embedded fonts and same-origin fetches — nothing else, nothing third-party.
_HOME_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; font-src data:; connect-src 'self'; "
        f"script-src 'sha256-{base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "public, max-age=300",
}


def _json(status: int, body, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _push_event(state, device_id: str, response: dict, started: float, replay: bool = False) -> None:
    """Activity-feed entry for a push: counts, plus table/id/reason for each rejected record
    (reasons name a field, never a value)."""
    rejected = response["rejected"]
    state.usage.event(
        "push", device_id=device_id, ms=_ms(started),
        applied=sum(response["applied"].values()), skipped=sum(response["skipped"].values()),
        rejected=len(rejected), replay=replay,
        tables={t: n for t, n in response["applied"].items() if n},
        rejects=[{"table": r["table"], "id": r["id"], "reason": r["reason"]} for r in rejected[:20]],
    )


async def _purge_loop(app: FastAPI) -> None:
    while True:
        try:
            await purge(app.state.pool)
        except Exception as exc:
            log("purge_failed", logging.ERROR, error=type(exc).__name__)
        await asyncio.sleep(app.state.settings.purge_interval_seconds)


async def _usage_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(USAGE_FLUSH_SECONDS)
        await app.state.usage.flush(app.state.pool)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        app.state.settings = settings or load_settings()
        app.state.token_digest = token_digest(app.state.settings.api_token)
        await run_migrations(app.state.settings.database_url)
        app.state.pool = await open_pool(app.state.settings.database_url)
        # Short timeouts: a dead Redis must cost milliseconds, not stall requests.
        app.state.redis = aioredis.from_url(
            app.state.settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        app.state.usage = metrics.Usage(app.state.settings.timezone)
        purge_task = asyncio.create_task(_purge_loop(app))
        usage_task = asyncio.create_task(_usage_loop(app))
        log("started")
        try:
            yield
        finally:
            purge_task.cancel()
            usage_task.cancel()
            for task in (purge_task, usage_task):
                with suppress(asyncio.CancelledError):
                    await task
            await app.state.usage.flush(app.state.pool)
            await app.state.redis.aclose()
            await app.state.pool.close()

    # No /docs, /redoc or /openapi.json: the only unauthenticated surface is /healthz.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        started = time.monotonic()
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > app.state.settings.max_body_bytes:
            return _json(413, error_body("validation", "request body too large"))
        response = await call_next(request)
        ms = round((time.monotonic() - started) * 1000, 1)
        path = request.url.path
        # Path only: no query string, no headers (the token lives in a header).
        log("request", method=request.method, path=path, status=response.status_code, ms=ms)
        # The home page, health checks and the dashboard's own polls would drown out real traffic.
        # (An erase request is not counted either: it wiped the counters it would have landed in.)
        if path.startswith("/v1/") and path not in ("/v1/stats", "/v1/dashboard") and not getattr(request.state, "erased", False):
            usage = request.app.state.usage
            code = response.status_code
            usage.status(code)
            if code in (401, 429):
                usage.event("denied", code, path=path)
            elif code >= 400:
                usage.event("failed", code, ms=ms, path=path)
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return _json(exc.status, error_body(exc.code, exc.message), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def request_validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"] if p != "query")
        return _json(422, error_body("validation", f"invalid or missing field: {field}"))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 401: "unauthorized", 429: "rate_limited"}.get(exc.status_code, "validation")
        return _json(exc.status_code, error_body(code, "not found" if exc.status_code == 404 else "request error"))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # Exception type only: driver error messages can carry row values.
        log("unhandled_error", logging.ERROR, error=type(exc).__name__)
        usage = getattr(request.app.state, "usage", None)
        if usage is not None:
            usage.status(500)
            usage.event("failed", 500, path=request.url.path, error=type(exc).__name__)
        return _json(500, error_body("internal", "internal error"))

    sync_guard = guard("sync", "sync_rate_limit")
    mcp_guard = guard("mcp", "mcp_rate_limit")
    account_guard = guard("account", "mcp_rate_limit")
    stats_guard = guard("stats", "sync_rate_limit", count=False)
    export_guard = guard("export", "mcp_rate_limit")
    dashboard_guard = guard("dashboard", "sync_rate_limit", count=False)  # a UI poll, like the stats

    async def json_body(request: Request):
        try:
            return json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            raise validation_error("request body must be valid JSON") from None

    # ---- home page ---------------------------------------------------------------
    @app.get("/")
    async def home():
        return HTMLResponse(_INDEX_HTML, headers=_HOME_HEADERS)

    # ---- health ------------------------------------------------------------------
    @app.get("/healthz")
    async def healthz(request: Request):
        db = redis_state = "up"
        try:
            async with request.app.state.pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            db = "down"
        try:
            await request.app.state.redis.ping()
        except Exception:
            redis_state = "down"
        # Redis is optional (§9), so only a database outage fails the healthcheck.
        status = "down" if db == "down" else ("ok" if redis_state == "up" else "degraded")
        return _json(503 if db == "down" else 200, {"status": status, "db": db, "redis": redis_state})

    # ---- sync --------------------------------------------------------------------
    @app.post("/v1/sync/push", dependencies=[Depends(sync_guard)])
    async def sync_push(request: Request):
        state = request.app.state
        started = time.monotonic()
        body = await json_body(request)
        device_id, records, snapshots, key, start_seq = sync.parse_push_envelope(
            body, request.headers.get("idempotency-key"), state.settings.max_push_records
        )
        if key:
            try:  # fast path; Postgres below is the durable record
                cached = await state.redis.get(f"idem:{key}")
            except Exception:
                cached = None
            if cached:
                state.usage.touch("push", device_id)
                _push_event(state, device_id, json.loads(cached), started, replay=True)
                return _json(200, json.loads(cached))
        async with state.pool.connection() as conn:
            async with conn.transaction():
                await write_lock(conn)
                if key:
                    cur = await conn.execute("SELECT response FROM idempotency_keys WHERE key = %s", (key,))
                    row = await cur.fetchone()
                    if row:
                        state.usage.touch("push", device_id)
                        _push_event(state, device_id, row["response"], started, replay=True)
                        return _json(200, row["response"])
                response = await sync.apply_push(conn, device_id, records, snapshots, start_seq)
                if key:
                    await conn.execute(
                        "INSERT INTO idempotency_keys (key, response) VALUES (%s, %s)",
                        (key, Jsonb(response)),
                    )
        if key:
            try:
                await state.redis.set(f"idem:{key}", json.dumps(response), ex=IDEM_TTL_SECONDS)
            except Exception:
                pass
        state.usage.touch("push", device_id)
        _push_event(state, device_id, response, started)
        return _json(200, response)

    @app.get("/v1/sync/pull", dependencies=[Depends(sync_guard)])
    async def sync_pull(request: Request, cursor: str | None = None, limit: int = Query(500, ge=1, le=1000)):
        if not cursor:
            raise validation_error("cursor is required; use /v1/sync/snapshot for a first sync")
        started = time.monotonic()
        seq = sync.decode_cursor(cursor)
        async with request.app.state.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                page = await sync.pull_page(conn, seq, limit)
        request.app.state.usage.touch("pull")
        request.app.state.usage.event(
            "pull", ms=_ms(started), records=sum(len(r) for r in page["records"].values()),
            deleted=len(page["deleted"]), snapshots=len(page["snapshots"]), has_more=page["has_more"],
        )
        return _json(200, page)

    @app.get("/v1/sync/capabilities", dependencies=[Depends(sync_guard)])
    async def sync_capabilities():
        return _json(200, {"schema_version": sync.SCHEMA_VERSION, "tables": list(sync.SYNC_TABLES)})

    @app.get("/v1/sync/snapshot", dependencies=[Depends(sync_guard)])
    async def sync_snapshot(request: Request):
        started = time.monotonic()
        async with request.app.state.pool.connection() as conn:
            async with conn.transaction():
                snapshot = await sync.full_snapshot(conn)
        request.app.state.usage.touch("snapshot")
        request.app.state.usage.event(
            "snapshot", ms=_ms(started), records=sum(len(r) for r in snapshot["records"].values()),
            snapshots=len(snapshot["snapshots"]),
        )
        return _json(200, snapshot)

    # ---- stats (feeds the home page; authenticated, never public) ----------------
    @app.get("/v1/stats", dependencies=[Depends(stats_guard)])
    async def stats(request: Request):
        state = request.app.state
        return _json(200, await metrics.collect(state.pool, state.usage, state.redis))

    # ---- dashboard data (token required; feeds the dashboard view) ------------------
    @app.get("/v1/dashboard", dependencies=[Depends(dashboard_guard)])
    async def dashboard_data(
        request: Request, year: int | None = Query(None, ge=2000, le=2100), month: int | None = Query(None, ge=1, le=12),
    ):
        if (year is None) != (month is None):
            raise validation_error("year and month go together: send both or neither")
        async with request.app.state.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                try:
                    data = await dashboard.build(conn, request.app.state.settings.timezone, year, month)
                except export.NoPlan:
                    raise ApiError(404, "not_found", f"no plan for {export.month_label(year, month)}") from None
        return _json(200, dashboard.serialise(data))

    # ---- export ------------------------------------------------------------------
    def _month_args(year: int | None, month: int | None):
        if (year is None) != (month is None):
            raise validation_error("year and month go together: send both or neither")
        return year, month

    def _download(filename: str, media_type: str, body: bytes) -> Response:
        return Response(body, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @app.get("/v1/export/csv", dependencies=[Depends(export_guard)])
    async def export_csv(
        request: Request, table: str = "all", year: int | None = Query(None, ge=2000, le=2100),
        month: int | None = Query(None, ge=1, le=12), include_deleted: bool = False,
    ):
        started = time.monotonic()
        year, month = _month_args(year, month)
        if table != "all" and table not in sync.SYNC_TABLES:
            raise validation_error("table must be one of: all, " + ", ".join(sync.SYNC_TABLES))
        async with request.app.state.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                filename, media_type, body, rows = await export.csv_export(conn, table, year, month, include_deleted)
        request.app.state.usage.event(
            "export", ms=_ms(started), format="csv", table=table, rows=rows,
            month=None if year is None else export.month_label(year, month),
        )
        return _download(filename, media_type, body)

    @app.get("/v1/export/pdf", dependencies=[Depends(export_guard)])
    async def export_pdf(
        request: Request, year: int | None = Query(None, ge=2000, le=2100), month: int | None = Query(None, ge=1, le=12),
    ):
        started = time.monotonic()
        year, month = _month_args(year, month)
        async with request.app.state.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                if year is None:
                    latest = await export.latest_plan_month(conn)
                    if latest is None:
                        raise ApiError(404, "not_found", "there is no plan to report on yet")
                    year, month = latest
                try:
                    report = await export.month_report(conn, year, month)
                except export.NoPlan:
                    raise ApiError(404, "not_found", f"no plan for {export.month_label(year, month)}") from None
        body = await asyncio.to_thread(export.render_pdf, report)
        request.app.state.usage.event(
            "export", ms=_ms(started), format="pdf", table="report", rows=len(report["expenses"]),
            month=export.month_label(year, month),
        )
        return _download(f"intoday-report-{export.month_label(year, month)}.pdf", "application/pdf", body)

    # ---- MCP ---------------------------------------------------------------------
    @app.post("/v1/mcp", dependencies=[Depends(mcp_guard)])
    async def mcp_post(request: Request):
        status, body = await mcp.handle_body(request.app, await request.body())
        if body is None:
            return Response(status_code=status)
        return _json(status, body)

    @app.get("/v1/mcp", dependencies=[Depends(mcp_guard)])
    async def mcp_discovery():
        return _json(200, {"tools": mcp.tool_listing()})

    # ---- erasure -----------------------------------------------------------------
    @app.delete("/v1/account", dependencies=[Depends(account_guard)])
    async def delete_account(request: Request):
        if request.headers.get("x-confirm") != "delete-all":
            raise validation_error("refused: send header X-Confirm: delete-all to erase all data")
        async with request.app.state.pool.connection() as conn:
            async with conn.transaction():
                await write_lock(conn)
                await sync.erase_all(conn)
        try:
            keys = [k async for k in request.app.state.redis.scan_iter(match="idem:*")]
            if keys:
                await request.app.state.redis.delete(*keys)
        except Exception:
            pass
        request.app.state.usage.reset()
        request.state.erased = True
        request.app.state.usage.event("account", action="erased")
        log("account_erased")
        return _json(200, {"deleted": True})

    return app
