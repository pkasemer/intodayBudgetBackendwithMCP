# InToday backend — handoff

Status as of 27 Oct 2026. Built from `intoday-backend-spec.md` (backend spec v0.1). This document
says what exists, how it behaves, what was decided on judgement, and what is still open. It contains
no secrets.

- **Repo:** `git@github.com:pkasemer/intodayBudgetBackendwithMCP.git` (branch `main`, one commit)
- **Live URL:** `https://intoday.pkasemer.com` (nginx on the VPS → `127.0.0.1:8100`, Let's Encrypt)
- **Verified live (unauthenticated checks only):** `/healthz` → 200 `{"status":"ok","db":"up","redis":"up"}`;
  no token → 401; `http://` → 301 to `https://`; HSTS present; `/docs` → 404; `/` serves the home page once `deploy/enable-homepage.sh` has been run; containers healthy.
- **Not verified live:** any authenticated call. The production token was never given to the builder.

## 1. What was built

One user, one bearer token. FastAPI (Python 3.13) + Postgres 17 + Redis 7 in Docker Compose.
The app is the only money-maths engine: the server mirrors records, stores the app's computed
snapshots opaquely, and exposes domain MCP tools. The server never computes Safe Today, pace, etc.

```
iPhone app ──HTTPS──▶ nginx (host) ──▶ 127.0.0.1:8100 ──▶ api ──▶ db (Postgres)
Muse / MCP client ──HTTPS──▶ same                                └▶ redis (cache only)
```

- Compose has **three** services (`api`, `db`, `redis`), not the spec's four: there is no Caddy because
  the VPS's existing nginx owns 80/443. Only `api` publishes a port, on loopback.
- Migrations: `api/migrations/*.sql`, applied by the api on start, once each (`schema_migrations`).
- Logs: JSON to stdout, counts and record IDs only (never amounts, notes, labels, token).
- Retention: tombstones purged after 30 days, idempotency keys after 7; purge runs at start and every 6 h.

### Repo layout

| Path | What |
|---|---|
| `docker-compose.yml` | production stack |
| `docker-compose.test.yml` | test overlay (no published ports) + `tests` runner |
| `api/app/sync.py` | LWW apply, push, pull, full snapshot, erase |
| `api/app/validation.py` | record validation shared by push and MCP writes |
| `api/app/static/index.html` | public home page at `/` (static, strict CSP, no external requests) |
| `api/app/mcp.py`, `tools.py` | MCP JSON-RPC server and the ten tools |
| `api/app/guards.py` | bearer auth + Redis rate limiting |
| `api/tests/` | 46 pytest tests |
| `scripts/` | `dev.sh`, `acceptance.sh`, `backup.sh`, `restore-check.sh`, `live_check.py` |
| `deploy/` | `intoday.pkasemer.com.conf` (nginx block), `install-nginx.sh`, `enable-homepage.sh` |

## 2. API contract (for the iOS sync client)

Base `https://intoday.pkasemer.com`. JSON everywhere. All endpoints except `/healthz` need
`Authorization: Bearer <token>`.

Errors: `{"error":{"code":"validation|unauthorized|not_found|rate_limited|internal","message":"..."}}`
with the matching status (422 validation, 401, 404, 429 + `Retry-After`, 500, 413 for bodies over 4 MB).
Unknown JSON fields are ignored.

### Records

Every synced record carries `id` (UUID string), `updated_at` (RFC 3339 UTC), `device_id` (string,
1–128 chars; use a stable per-install UUID), `deleted_at` (null or timestamp). Tables, in dependency
order: `month_plans`, `savings_goals`, `bill_templates`, `bills`, `expenses`, `income_events`,
`savings_allocations`. Fields mirror the spec §4 field-for-field. Money is a whole-number integer.

Validation (per record; an invalid record is rejected alone, never the batch):
amounts are integers 1–9,999,999 (`buffer_amount` allows 0); floats, booleans, zero, negatives and
8+ digits are rejected; `tag` ∈ `food|transport|dataAirtime|fun|shopping|other`; dates are real
`YYYY-MM-DD` and must fall in the referenced plan's window; `note` ≤ 200; income `label` required,
non-blank, ≤ 100; `destination=lockedSavings` requires an existing `savings_goal_id`, `spendable`
forbids it; `due_day` 1–31; enums are exact. Rejection reasons name the field, never the value.

### `POST /v1/sync/push`

```json
{ "device_id": "…", "idempotency_key": "uuid", "cursor": "<optional, your stored pull cursor>",
  "records": { "month_plans": [], "bills": [], "expenses": [], "income_events": [],
               "savings_goals": [], "savings_allocations": [], "bill_templates": [] },
  "snapshots": [ { "kind": "today", "plan_year": 2026, "plan_month": 10,
                   "payload": { }, "snapshot_version": 1 } ] }
```

Send the key as the `Idempotency-Key` header too (header and body must match if both are present).
Response: `{"applied":{table:n,…,"snapshots":n},"skipped":{…},"rejected":[{"table","id","reason"}],"cursor":"…"}`.
Retrying with the same key returns the stored response and applies nothing. At most 5,000 records per push.

- **LWW:** a record applies only if its `updated_at` is newer, or equal with a greater `device_id`
  (C-collation string compare). Otherwise it is `skipped`.
- **Tombstones:** push with `deleted_at` set. Only the sync columns are needed to delete an existing row.
  A stale delete is `skipped`, not rejected.
- **Snapshots:** upsert by `(kind, plan_year, plan_month)`, same LWW rule. `kind` ∈ `today|insights`,
  `payload` must be a JSON object ≤ 64 KB. If a snapshot has no `updated_at`, the server uses
  `payload.as_of`, else server time.

### `GET /v1/sync/pull?cursor=<opaque>&limit=500`

Cursor is required (first sync uses the snapshot endpoint). Response:
`{"cursor","has_more","records":{…all seven tables…},"deleted":[{"table","id","deleted_at","updated_at","device_id"}],"snapshots":[…]}`.
Page until `has_more` is false. Tombstoned rows come back in `deleted`, never in `records`.

### `GET /v1/sync/snapshot`

Every live record plus live snapshots, consistent as of one moment, plus a `cursor` to resume pulling from.
For fresh installs and explicit re-provisioning only; the app must confirm before replacing a non-empty store.

### Cursor behaviour — read this

- Cursors walk a **server-assigned sequence**, not `updated_at`. A client clock that goes backwards
  therefore can't make pulls skip rows.
- The push response `cursor` is computed **before** the batch applies and stops short of any row written
  by another writer (e.g. an MCP write). So after a push, your next pull echoes your own just-pushed rows
  back (harmless: same LWW rule) and **never skips an MCP write**. Send your stored cursor as `cursor`
  in the next push so it advances.
- MCP writes use `device_id = "mcp"`, and their `updated_at` is `max(now, row.updated_at + 1ms)`, so a
  phone with a fast clock can't silently overwrite them.

### Timestamps

RFC 3339 UTC with `Z`. Fractional seconds appear only when non-zero; MCP writes carry milliseconds.
**The client must parse both with and without fractional seconds.**

### Other endpoints

- `DELETE /v1/account` with header `X-Confirm: delete-all` erases every row (sequence is not reset, so old
  cursors stay valid and just return nothing). Without the header: 422, nothing deleted.
- `GET /healthz` (no auth): `ok`, `degraded` (Redis down, still serving) or 503 `down` (database down).

### Rate limits (Redis, fixed 60 s window, per token)

120/min on sync endpoints, 60/min on `/v1/mcp`, separate buckets. If Redis is down, limiting is skipped
and logged (never fails closed). Bad-token attempts share one global counter (30/min → 429); a valid
token is never affected by it.

## 3. MCP server

`POST /v1/mcp` — Streamable HTTP, JSON-RPC 2.0, same bearer token. Stateless, plain-JSON responses, no
session IDs; notifications get 202. Implements `initialize` (negotiates 2025-06-18 / 2025-03-26 /
2024-11-05), `tools/list`, `tools/call`, `ping` only. `GET /v1/mcp` returns `{"tools":[…]}`.
Tool failures come back as `isError: true` results; results carry both text content and
`structuredContent`.

**There are ten tools** (the spec text says "seven" in §14 but §8 lists seven read + three write; all
ten are exposed). No SQL or schema tool exists, by design.

| Tool | Input | Notes |
|---|---|---|
| `get_today` | `date?` | Latest `today` snapshot + `as_of`; if none: `snapshot:null`, a note, `expenses_today`, `bills_due` |
| `get_month_summary` | `year, month` | Plan row, counts, plain sums (spent, income received/expected, bills paid/unpaid), live snapshots |
| `list_expenses` | `year, month, tag?, limit?` | Newest first; limit default 50, max 200 |
| `list_bills` | `year, month, unpaid_only?` | |
| `list_income_events` | `year, month` | |
| `list_goals` | — | `allocated_total`, `income_to_savings_received` (received income only), `locked_total` = sum |
| `get_insights` | `year, month` | Latest `insights` snapshot, else raw `tag_totals` |
| `add_expense` | `amount, tag, date, note?` | **Write.** Plan chosen by date; `device_id:"mcp"`; returns row + `device_sync_pending:true` |
| `mark_bill_paid` | `bill_id` | **Write.** Only unpaid, live bills |
| `mark_income_received` | `income_event_id` | **Write.** Only `expected` rows |

Writes are single transactions through the same validation and LWW code as push. The phone sees them on
its next pull; the server never pushes to the device. Per the standing rule, approval happens in the Muse
conversation — a tool existing is not permission.

To connect a client: add `https://intoday.pkasemer.com/v1/mcp` as a custom MCP connector with a bearer
token header, entered in the client's own settings (never pasted into chat or committed).

## 4. Testing done

- `scripts/dev.sh run --rm tests` → **46 passed** (real Postgres + Redis, in-process ASGI): auth, push/pull
  round trip, paging, idempotent retry (Redis and Postgres paths), LWW incl. tie-break, tombstones and
  purge with an injected clock, snapshot round trip, erasure, every validation rule, all ten MCP tools,
  Redis-down behaviour, rate limits, body cap, log hygiene, and the MCP-write-not-skipped cursor case.
- `scripts/acceptance.sh` → **passed**: cold start from empty volumes, Redis stopped/started under live
  traffic, backup + restore drill (row counts matched), no token or amounts in container logs.
- Spec §14 items 1–10 and 12–14 are covered by the above. Item 11 (TLS/redirect/HSTS) was checked on the
  live host. Item 7's "equals the app's Today screen" needs a human with the app (pending).

## 5. Decisions made on judgement — please confirm

1. **No Caddy** (nginx already owns 80/443); api binds loopback `:8100` only.
2. **Ten tools**, not seven (see §3).
3. **Cursor ordering** by server sequence instead of `(updated_at, id)` (spec §5.1 and §5.3 conflict; see §2).
4. **Plan date window.** The product spec wasn't available, so what `start_day ≠ 1` means is ambiguous.
   `validation.plan_window` is deliberately lenient: with `start_day=1` it's the calendar month; otherwise it
   spans `start_day` of the previous month to `start_day` of the next. `add_expense` prefers the plan whose
   calendar month holds the date. **Tighten once the product spec's cycle rule is confirmed.**
5. **`list_goals` totals** sum allocations plus *received* income sent to savings (spec wording is vague);
   the parts are exposed separately in case this double-counts.
6. **`updated_at` fallback for snapshots** with no envelope timestamp (spec's push example has none).
7. `idempotency_key` is optional (the spec's shape lists it, but nothing breaks without it).

## 6. Open items

- **Backups are not scheduled.** `scripts/backup.sh` works (7 dumps, 0600, default `/var/backups/intoday`,
  which needs root to create — or set `BACKUP_DIR`). Add the cron line from its header, then run
  `scripts/restore-check.sh <dump>` once against real data.
- **Authenticated live smoke test** with the production token (owner to run; example in README).
- **Cloudflare SSL mode** should be *Full (strict)* since the name is proxied.
- **iOS sync client:** not built (separate work item). The two sides must agree on `device_id` format
  (UUID string, stable per install, keychain) and the snapshot payload schemas (spec Appendix A; the
  `insights` fields are still to be fixed by the app developer).
- **Spec says build only after v1 ships.** The owner explicitly gave the go-ahead to build now.
- **Git author** on the single commit is `pktest <test@gmail.com>`, not the owner's identity (amend +
  force-push if wanted). Confirm the GitHub repo is private.
- **Docker-image pins** (`postgres:17-alpine`, `redis:7-alpine`, `python:3.13-slim`, pinned Python deps)
  should be re-checked at the next deploy, per the spec.

## 7. Operating it

```bash
docker compose ps                    # run from /home/pk/budget-mcp
docker compose logs -f api           # JSON logs
curl -s https://intoday.pkasemer.com/healthz
```

- **Rotate the token:** edit `API_TOKEN` in `.env`, `docker compose up -d api`; the old token dies immediately.
- **Deploy a change:** `git pull && docker compose up -d --build`.
- **Restore a dump:** `docker compose exec -T db pg_restore -U intoday -d intoday --clean --if-exists < dump`.
- **Dev/test:** `scripts/dev.sh up -d --wait db redis`, `scripts/dev.sh run --rm tests`,
  `scripts/acceptance.sh`, `scripts/dev.sh down -v` (separate project `intoday-dev`, throwaway token in
  git-ignored `.env.dev`, no published ports).
- **Secrets:** `.env` and `.env.dev` are git-ignored. The builder never held the production token or DB
  password; the owner created `.env` on the server.
- **Boundaries:** the stack makes no outbound calls and has no analytics. It is independent of the trading
  project on this VPS (separate Compose project, no shared files; only the host nginx is shared).
