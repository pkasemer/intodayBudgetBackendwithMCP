from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .logs import log

# Every writer (push, MCP write, erase, purge) takes this transaction-scoped lock first.
# That makes commit order equal seq order, which is what keeps pull cursors gap-free.
WRITE_LOCK = 724001
_MIGRATE_LOCK = 724002

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def open_pool(database_url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=30)
    return pool


async def write_lock(conn) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))


async def run_migrations(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply versioned SQL files in order, each exactly once. Returns versions applied now.

    Uses its own autocommit connection (not the pool) so each file gets a real transaction
    and the session-level advisory lock can't leak into pooled connections.
    """
    applied_now: list[str] = []
    async with await psycopg.AsyncConnection.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATE_LOCK,))
        try:
            async with conn.transaction():
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            cur = await conn.execute("SELECT version FROM schema_migrations")
            done = {row["version"] for row in await cur.fetchall()}
            for path in sorted(directory.glob("*.sql")):
                if path.stem in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied_now.append(path.stem)
                log("migration_applied", version=path.stem)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATE_LOCK,))
    return applied_now
