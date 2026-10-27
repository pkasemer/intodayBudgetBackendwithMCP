import logging

from app.logs import _JsonFormatter

from .conftest import TOKEN


async def test_healthz_needs_no_auth(anon):
    r = await anon.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "up", "redis": "up"}


async def test_no_token_is_401(anon):
    r = await anon.get("/v1/mcp")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthorized", "message": "unauthorized"}}


async def test_wrong_token_is_401(app):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Authorization": "Bearer nope"}) as c:
        for path in ("/v1/mcp", "/v1/sync/snapshot", "/v1/sync/pull?cursor=x"):
            assert (await c.get(path)).status_code == 401


async def test_right_token_is_200(client):
    assert (await client.get("/v1/mcp")).status_code == 200


async def test_every_endpoint_requires_auth(anon):
    assert (await anon.post("/v1/sync/push", json={})).status_code == 401
    assert (await anon.post("/v1/mcp", json={})).status_code == 401
    assert (await anon.delete("/v1/account", headers={"X-Confirm": "delete-all"})).status_code == 401


async def test_docs_are_not_exposed(anon):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await anon.get(path)).status_code == 404


async def test_token_never_appears_in_logs(app, client):
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(_JsonFormatter().format(record))

    logger = logging.getLogger("intoday")
    handler = Capture()
    logger.addHandler(handler)
    try:
        await client.get("/v1/mcp")
        await client.get("/v1/mcp", headers={"Authorization": "Bearer wrong-token-value"})
        await client.post("/v1/sync/push", json={"device_id": "p", "records": {}})
    finally:
        logger.removeHandler(handler)
    assert lines, "expected request log lines"
    assert not any(TOKEN in line or "wrong-token-value" in line for line in lines)


async def test_home_page_is_public_static_and_locked_down(anon):
    r = await anon.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "InToday" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1]
    assert r.headers["x-content-type-options"] == "nosniff"
    # No third-party anything: no external URLs in the page.
    assert "http://" not in r.text.replace("http://www.w3.org", "") and "https://" not in r.text
