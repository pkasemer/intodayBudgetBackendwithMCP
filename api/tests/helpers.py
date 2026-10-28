import uuid


def new_id() -> str:
    return str(uuid.uuid4())


def rec(**kw) -> dict:
    base = {"id": new_id(), "updated_at": "2026-10-27T10:00:00Z", "device_id": "phone-1", "deleted_at": None}
    base.update(kw)
    return base


def plan(year=2026, month=10, **kw) -> dict:
    return rec(**{"year": year, "month": month, "buffer_amount": 50000, "start_day": 1, "plan_state": "active", **kw})


def expense(plan_id, **kw) -> dict:
    return rec(**{"plan_id": plan_id, "amount": 15000, "date": "2026-10-27", "tag": "food", "note": None, **kw})


def bill(plan_id, **kw) -> dict:
    return rec(**{"plan_id": plan_id, "name": "Rent", "amount": 300000, "due_day": 28,
                  "is_paid": False, "is_one_off": False, **kw})


def income(plan_id, **kw) -> dict:
    return rec(**{"plan_id": plan_id, "label": "Salary", "amount": 900000, "date": "2026-10-25",
                  "kind": "primary", "status": "expected", "destination": "spendable", **kw})


def goal(**kw) -> dict:
    return rec(**{"name": "Laptop", "target_amount": 2000000, **kw})


def template(**kw) -> dict:
    return rec(**{"name": "Rent", "default_amount": 300000, "default_due_day": 28, "is_active": True, **kw})


def allocation(plan_id, goal_id, **kw) -> dict:
    return rec(**{"plan_id": plan_id, "goal_id": goal_id, "amount": 50000, **kw})


def debt(**kw) -> dict:
    return rec(**{"name": "Stanbic", "kind": "loan", "amount": 1000000, "started_on": "2026-11-10",
                  "monthly_payment": 150000, "due_day": 12, "first_month": "2026-11", **kw})


def loan(plan_id=None, **kw) -> dict:
    return rec(**{"plan_id": plan_id, "person": "Sam", "amount": 100000, "lent_on": "2026-11-08",
                  "due_on": "2026-11-15", "repaid_on": None, "written_off": False, **kw})


def repayment(loan_id, **kw) -> dict:
    return rec(**{"loan_id": loan_id, "amount": 40000, "date": "2026-11-09", "landed_as_income": False, **kw})


def debt_payment(debt_id, plan_id, **kw) -> dict:
    return rec(**{"debt_id": debt_id, "plan_id": plan_id, "amount": 50000, "date": "2026-11-10", **kw})


def limit(**kw) -> dict:
    return rec(**{"tag": "food", "amount": 300000, **kw})


def today_snapshot(**payload) -> dict:
    body = {"snapshot_version": 1, "today_date": "2026-10-27", "safe_today": 14900, "remaining_today": 14900,
            "spent_today": 0, "pace_delta": -123600, "banked": 0, "streak_days": 0, "days_left": 5,
            "left_this_week": 74500, "plant_health": 100, "plant_band": "thriving", "pot_state": "active",
            "as_of": "2026-10-27T07:30:00Z", **payload}
    return {"kind": "today", "plan_year": 2026, "plan_month": 10, "payload": body, "snapshot_version": 1}


async def push(client, records=None, snapshots=None, key=None, device="phone-1", **extra):
    body = {"device_id": device, "idempotency_key": key or new_id(), "records": records or {},
            "snapshots": snapshots or [], **extra}
    r = await client.post("/v1/sync/push", json=body)
    return r


async def pull(client, cursor, **params):
    return await client.get("/v1/sync/pull", params={"cursor": cursor, **params})


async def rpc(client, method, params=None, msg_id=1):
    r = await client.post("/v1/mcp", json={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    return r


async def call_tool(client, name, arguments=None):
    r = await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]
