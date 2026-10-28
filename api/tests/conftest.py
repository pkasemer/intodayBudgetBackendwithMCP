import os

import httpx
import pytest
import pytest_asyncio

from app.config import load_settings
from app.main import create_app

if os.environ.get("INTODAY_TEST") != "1":
    raise RuntimeError("tests truncate every table; run them via the test compose project (INTODAY_TEST=1)")

TOKEN = os.environ["API_TOKEN"]
TRUNCATE = (
    "TRUNCATE month_plans, savings_goals, bill_templates, bills, expenses, income_events, "
    "savings_allocations, debts, loans, loan_repayments, debt_payments, category_limits, "
    "snapshots, idempotency_keys, usage_daily, sync_status CASCADE"
)


async def _start(settings):
    app = create_app(settings)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    async with app.state.pool.connection() as conn:
        await conn.execute(TRUNCATE)
    await app.state.redis.flushdb()
    return app, ctx


def _client(app, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


@pytest_asyncio.fixture
async def app():
    app, ctx = await _start(load_settings())
    yield app
    await ctx.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def client(app):
    async with _client(app) as c:
        yield c


@pytest_asyncio.fixture
async def anon(app):
    async with _client(app, token=None) as c:
        yield c


@pytest_asyncio.fixture
async def app_no_redis():
    from dataclasses import replace

    settings = replace(load_settings(), redis_url="redis://127.0.0.1:1/0")  # nothing listens there
    app = create_app(settings)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    async with app.state.pool.connection() as conn:
        await conn.execute(TRUNCATE)
    yield app
    await ctx.__aexit__(None, None, None)
