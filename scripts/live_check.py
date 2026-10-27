"""Tiny HTTP client run *inside* the api container (python - <mode> < live_check.py), so the
live checks need no published port and no token on the host command line.
Modes: health | seed | smoke"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"
TOKEN = os.environ["API_TOKEN"]


def call(method, path, body=None, auth=True, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def seed():
    plan_id = str(uuid.uuid4())
    now = "2026-10-27T10:00:00Z"
    base = {"updated_at": now, "device_id": "live-check", "deleted_at": None}
    body = {
        "device_id": "live-check", "idempotency_key": str(uuid.uuid4()),
        "records": {
            "month_plans": [{**base, "id": plan_id, "year": 2026, "month": 10, "buffer_amount": 0, "start_day": 1, "plan_state": "active"}],
            "expenses": [{**base, "id": str(uuid.uuid4()), "plan_id": plan_id, "amount": 15000, "date": "2026-10-27", "tag": "food", "note": None}],
        },
        "snapshots": [{"kind": "today", "plan_year": 2026, "plan_month": 10, "snapshot_version": 1,
                       "payload": {"snapshot_version": 1, "safe_today": 14900, "as_of": now}}],
    }
    return call("POST", "/v1/sync/push", body)


mode = sys.argv[1]
if mode == "health":
    print(json.dumps(call("GET", "/healthz", auth=False)))
elif mode == "seed":
    status, out = seed()
    print(json.dumps({"status": status, "applied": out["applied"], "rejected": out["rejected"]}))
elif mode == "smoke":
    status, pushed = seed()
    pstatus, pulled = call("GET", "/v1/sync/pull?cursor=" + pushed["cursor"])
    mstatus, tools = call("POST", "/v1/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tstatus, today = call("POST", "/v1/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "get_today", "arguments": {"date": "2026-10-27"}}})
    ustatus, _ = call("GET", "/v1/mcp", auth=False)
    ok = (status, pstatus, mstatus, tstatus, ustatus) == (200, 200, 200, 200, 401) and not today["result"]["isError"]
    print(json.dumps({"ok": ok, "push": status, "pull": pstatus, "mcp_list": mstatus, "tools": len(tools["result"]["tools"]),
                      "get_today": tstatus, "no_token": ustatus}))
    sys.exit(0 if ok else 1)
