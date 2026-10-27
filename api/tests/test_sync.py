from datetime import datetime, timedelta, timezone

from app.purge import purge

from . import helpers as h


def full_batch():
    p = h.plan()
    g = h.goal()
    return p, g, {
        "month_plans": [p],
        "savings_goals": [g],
        "bill_templates": [h.template()],
        "bills": [h.bill(p["id"])],
        "expenses": [h.expense(p["id"]), h.expense(p["id"], amount=2000, tag="transport")],
        "income_events": [
            h.income(p["id"]),
            h.income(p["id"], label="Gift", kind="extra", destination="lockedSavings", savings_goal_id=g["id"]),
        ],
        "savings_allocations": [h.allocation(p["id"], g["id"])],
    }


def ids(records):
    return {t: sorted(r["id"] for r in rows) for t, rows in records.items() if rows}


async def test_push_pull_roundtrip(client):
    _, _, records = full_batch()
    r = await h.push(client, records, [h.today_snapshot()])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rejected"] == []
    assert body["applied"]["expenses"] == 2 and body["applied"]["snapshots"] == 1

    page = (await h.pull(client, body["cursor"])).json()
    assert ids(page["records"]) == ids(records)
    assert page["deleted"] == [] and not page["has_more"]
    assert [s["kind"] for s in page["snapshots"]] == ["today"]

    again = (await h.pull(client, page["cursor"])).json()
    assert ids(again["records"]) == {} and again["deleted"] == [] and again["snapshots"] == []
    assert again["cursor"] == page["cursor"]


async def test_pull_requires_cursor_and_rejects_garbage(client):
    assert (await client.get("/v1/sync/pull")).status_code == 422
    assert (await h.pull(client, "not-a-cursor")).status_code == 422


async def test_pull_pages_with_has_more(client):
    p = h.plan()
    await h.push(client, {"month_plans": [p], "expenses": [h.expense(p["id"]) for _ in range(5)]})
    seen, cursor = [], (await h.push(client, {})).json()["cursor"]
    cursor = "czE6MA"  # s1:0 — start of time
    while True:
        page = (await h.pull(client, cursor, limit=2)).json()
        seen += [r["id"] for rows in page["records"].values() for r in rows]
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == 6 and len(set(seen)) == 6


async def test_idempotent_retry(app, client):
    _, _, records = full_batch()
    key = h.new_id()
    first = await h.push(client, records, key=key)
    await app.state.redis.flushdb()  # force the durable Postgres path on the retry
    second = await h.push(client, records, key=key)
    assert second.json() == first.json()
    third = await h.push(client, records, key=key)  # Redis fast path
    assert third.json() == first.json()
    snap = (await client.get("/v1/sync/snapshot")).json()
    assert len(snap["records"]["expenses"]) == 2


async def test_idempotency_header_and_body_must_agree(client):
    r = await client.post("/v1/sync/push", json={"device_id": "p", "idempotency_key": "a"}, headers={"Idempotency-Key": "b"})
    assert r.status_code == 422


async def test_last_write_wins(client):
    p = h.plan()
    e = h.expense(p["id"], amount=1000, updated_at="2026-10-27T10:00:00Z", device_id="b")
    await h.push(client, {"month_plans": [p], "expenses": [e]})

    async def stored():
        snap = (await client.get("/v1/sync/snapshot")).json()
        return snap["records"]["expenses"][0]["amount"]

    older = await h.push(client, {"expenses": [{**e, "amount": 2000, "updated_at": "2026-10-27T09:00:00Z"}]})
    assert older.json()["skipped"]["expenses"] == 1 and await stored() == 1000

    newer = await h.push(client, {"expenses": [{**e, "amount": 3000, "updated_at": "2026-10-27T11:00:00Z"}]})
    assert newer.json()["applied"]["expenses"] == 1 and await stored() == 3000

    tie_lower = await h.push(client, {"expenses": [{**e, "amount": 4000, "updated_at": "2026-10-27T11:00:00Z", "device_id": "a"}]})
    assert tie_lower.json()["skipped"]["expenses"] == 1 and await stored() == 3000

    tie_higher = await h.push(client, {"expenses": [{**e, "amount": 5000, "updated_at": "2026-10-27T11:00:00Z", "device_id": "c"}]})
    assert tie_higher.json()["applied"]["expenses"] == 1 and await stored() == 5000


async def test_tombstone_flow(app, client):
    p = h.plan()
    e = h.expense(p["id"])
    cursor = (await h.push(client, {"month_plans": [p], "expenses": [e]})).json()["cursor"]
    page = (await h.pull(client, cursor)).json()
    cursor = page["cursor"]

    tomb = {"id": e["id"], "updated_at": "2026-10-28T10:00:00Z", "device_id": "phone-1", "deleted_at": "2026-10-28T10:00:00Z"}
    assert (await h.push(client, {"expenses": [tomb]})).json()["applied"]["expenses"] == 1

    page = (await h.pull(client, cursor)).json()
    assert [d["id"] for d in page["deleted"]] == [e["id"]]
    assert page["records"]["expenses"] == []
    assert (await client.get("/v1/sync/snapshot")).json()["records"]["expenses"] == []

    # A stale delete (older than the stored tombstone) is ignored rather than rejected.
    stale = {**tomb, "updated_at": "2026-10-20T10:00:00Z", "deleted_at": "2026-10-20T10:00:00Z"}
    assert (await h.push(client, {"expenses": [stale]})).json()["skipped"]["expenses"] == 1

    # Test clock: 31 days later the tombstone is purged for good.
    later = datetime.now(timezone.utc) + timedelta(days=31)
    async with app.state.pool.connection() as conn:
        await conn.execute("UPDATE expenses SET deleted_at = %s", (datetime.now(timezone.utc),))
    counts = await purge(app.state.pool, now=later)
    assert counts["expenses"] == 1
    async with app.state.pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM expenses")
        assert (await cur.fetchone())["n"] == 0


async def test_tombstone_purge_respects_age_and_parents(app, client):
    p = h.plan()
    await h.push(client, {"month_plans": [p], "expenses": [h.expense(p["id"])]})
    tomb = {"id": p["id"], "updated_at": "2026-10-28T10:00:00Z", "device_id": "phone-1", "deleted_at": "2026-10-28T10:00:00Z"}
    await h.push(client, {"month_plans": [tomb]})
    now = datetime.now(timezone.utc)
    async with app.state.pool.connection() as conn:
        await conn.execute("UPDATE month_plans SET deleted_at = %s", (now - timedelta(days=40),))
    # Plan tombstone is old, but a live child still references it: kept.
    assert (await purge(app.state.pool, now=now))["month_plans"] == 0
    async with app.state.pool.connection() as conn:
        await conn.execute("DELETE FROM expenses")
    assert (await purge(app.state.pool, now=now))["month_plans"] == 1


async def test_idempotency_keys_expire_after_7_days(app, client):
    await h.push(client, {}, key="old-key")
    counts = await purge(app.state.pool, now=datetime.now(timezone.utc) + timedelta(days=8))
    assert counts["idempotency_keys"] == 1


async def test_mcp_write_is_never_skipped_by_push_cursor(client):
    p = h.plan()
    first = (await h.push(client, {"month_plans": [p]})).json()
    result = await h.call_tool(client, "add_expense", {"amount": 5000, "tag": "fun", "date": "2026-10-27"})
    mcp_id = result["structuredContent"]["expense"]["id"]
    # Phone pushes again, supplying its cursor; the response cursor must stop before the MCP row.
    second = (await h.push(client, {"expenses": [h.expense(p["id"])]}, cursor=first["cursor"])).json()
    page = (await h.pull(client, second["cursor"])).json()
    assert mcp_id in [e["id"] for e in page["records"]["expenses"]]


async def test_each_validation_rule_rejects_one_record_only(client):
    p = h.plan()
    g = h.goal()
    good = h.expense(p["id"])
    bad = {
        "amount_zero": h.expense(p["id"], amount=0),
        "amount_negative": h.expense(p["id"], amount=-5),
        "amount_float": h.expense(p["id"], amount=10.5),
        "amount_bool": h.expense(p["id"], amount=True),
        "amount_8_digits": h.expense(p["id"], amount=10_000_000),
        "bad_tag": h.expense(p["id"], tag="gambling"),
        "bad_date": h.expense(p["id"], date="2026-02-30"),
        "date_outside_plan": h.expense(p["id"], date="2027-03-05"),
        "unknown_plan": h.expense(h.new_id()),
        "long_note": h.expense(p["id"], note="x" * 201),
    }
    income_bad = {
        "empty_label": h.income(p["id"], label="   "),
        "long_label": h.income(p["id"], label="x" * 101),
        "locked_without_goal": h.income(p["id"], destination="lockedSavings"),
        "spendable_with_goal": h.income(p["id"], savings_goal_id=g["id"]),
        "locked_missing_goal": h.income(p["id"], destination="lockedSavings", savings_goal_id=h.new_id()),
        "bad_kind": h.income(p["id"], kind="bonus"),
        "bad_status": h.income(p["id"], status="late"),
    }
    other_bad = {
        "bad_due_day": h.bill(p["id"], due_day=32),
        "bad_plan_state": h.plan(2026, 11, plan_state="frozen"),
    }
    records = {
        "month_plans": [p, other_bad["bad_plan_state"]],
        "savings_goals": [g],
        "bills": [other_bad["bad_due_day"]],
        "expenses": [good, *bad.values()],
        "income_events": list(income_bad.values()),
    }
    body = (await h.push(client, records)).json()
    rejected_ids = {r["id"] for r in body["rejected"]}
    expected = {r["id"] for r in [*bad.values(), *income_bad.values(), *other_bad.values()]}
    assert rejected_ids == expected
    assert all(r["reason"] for r in body["rejected"])
    assert body["applied"]["expenses"] == 1 and body["applied"]["month_plans"] == 1
    # Reasons name the field, never the value.
    assert not any("gambling" in r["reason"] for r in body["rejected"])


async def test_envelope_errors_are_422(client):
    assert (await client.post("/v1/sync/push", json={"records": {}})).status_code == 422
    assert (await client.post("/v1/sync/push", content=b"not json")).status_code == 422
    assert (await client.post("/v1/sync/push", json={"device_id": "p", "records": {"bills": "x"}})).status_code == 422


async def test_unknown_fields_ignored(client):
    p = {**h.plan(), "future_field": 1}
    body = (await h.push(client, {"month_plans": [p], "new_table": [{"id": "x"}]})).json()
    assert body["applied"]["month_plans"] == 1 and body["rejected"] == []


async def test_snapshot_must_be_small_json_object(client):
    big = h.today_snapshot(filler="x" * 70_000)
    small = h.today_snapshot()
    body = (await h.push(client, {}, [big, small, {**small, "kind": "weekly"}, {**small, "payload": []}])).json()
    assert body["applied"]["snapshots"] == 1
    assert len(body["rejected"]) == 3


async def test_full_snapshot_endpoint(client):
    _, _, records = full_batch()
    await h.push(client, records, [h.today_snapshot()])
    snap = (await client.get("/v1/sync/snapshot")).json()
    assert ids(snap["records"]) == ids(records)
    assert snap["snapshots"][0]["payload"]["safe_today"] == 14900
    # The returned cursor is a valid pull position and nothing is newer.
    again = (await h.pull(client, snap["cursor"])).json()
    assert ids(again["records"]) == {}


async def test_erasure_requires_confirm_header(client):
    _, _, records = full_batch()
    await h.push(client, records, [h.today_snapshot()])
    cursor = (await client.get("/v1/sync/snapshot")).json()["cursor"]

    refused = await client.delete("/v1/account")
    assert refused.status_code == 422
    assert ids((await client.get("/v1/sync/snapshot")).json()["records"]) != {}

    done = await client.delete("/v1/account", headers={"X-Confirm": "delete-all"})
    assert done.status_code == 200 and done.json() == {"deleted": True}
    snap = (await client.get("/v1/sync/snapshot")).json()
    assert ids(snap["records"]) == {} and snap["snapshots"] == []
    page = (await h.pull(client, cursor)).json()
    assert ids(page["records"]) == {} and page["snapshots"] == [] and page["deleted"] == []


async def test_timestamps_round_trip_in_rfc3339_z(client):
    p = h.plan()
    await h.push(client, {"month_plans": [p]})
    row = (await client.get("/v1/sync/snapshot")).json()["records"]["month_plans"][0]
    assert row["updated_at"] == "2026-10-27T10:00:00Z" and row["deleted_at"] is None
    assert "seq" not in row
