"""Minimal MCP server over Streamable HTTP (backend spec §8.1).

Implements exactly initialize, tools/list, tools/call and ping — no resources, prompts or
sampling. Stateless: no session IDs, every POST stands alone. Responses are plain JSON
(never an SSE stream); notifications get 202.
"""
import json
import logging

from . import sync, tools
from .db import write_lock
from .logs import log

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "intoday-backend", "version": "0.1.0"}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(val) for k, val in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(val) for val in obj]
    return sync.ser_value(obj)


def tool_listing() -> list[dict]:
    return [t.listing() for t in tools.TOOLS]


async def _call_tool(app, name: str, arguments) -> dict:
    tool = tools.BY_NAME[name]
    app.state.usage.bump("mcp")
    try:
        args = tools.check_args(tool, arguments)
        async with app.state.pool.connection() as conn:
            async with conn.transaction():
                if tool.write:
                    await write_lock(conn)
                else:
                    await conn.execute("SET TRANSACTION READ ONLY")
                result = _jsonable(await tool.handler(conn, app.state.settings, args))
    except tools.ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    if tool.write:
        # Tool name + record ID only; never amounts or notes.
        log("mcp_write", tool=name, record_id=result[tool.record_key]["id"])
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "structuredContent": result,
        "isError": False,
    }


async def handle_message(app, msg):
    """One JSON-RPC message in, one response dict out (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "invalid request")
    if "id" not in msg:
        return None  # notification (e.g. notifications/initialized): nothing to answer
    msg_id, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        wanted = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tool_listing()}}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in tools.BY_NAME:
            return _error(msg_id, INVALID_PARAMS, "unknown tool")
        try:
            result = await _call_tool(app, name, params.get("arguments"))
        except Exception as exc:  # database trouble etc. — type only, messages can carry data
            log("mcp_internal_error", logging.ERROR, tool=name, error=type(exc).__name__)
            return _error(msg_id, INTERNAL_ERROR, "internal error")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    return _error(msg_id, METHOD_NOT_FOUND, "method not found")


async def handle_body(app, raw: bytes):
    """Returns (status, body) where body is None for an empty 202."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return 400, _error(None, PARSE_ERROR, "parse error")
    if isinstance(payload, list):
        if not payload:
            return 400, _error(None, INVALID_REQUEST, "empty batch")
        replies = [r for r in [await handle_message(app, m) for m in payload] if r is not None]
        return (200, replies) if replies else (202, None)
    reply = await handle_message(app, payload)
    return (200, reply) if reply is not None else (202, None)
