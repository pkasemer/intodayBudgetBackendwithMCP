# InToday backend

Sync + MCP service for the InToday iOS app. Built from `intoday-backend-spec.md` (backend spec v0.1,
27 Oct 2026). One user, one bearer token. FastAPI (Python 3.13) + Postgres 17 + Redis 7 in Docker
Compose. The app is the only money-maths engine: the server mirrors the app's records, stores the app's
computed snapshots opaquely, and exposes domain MCP tools. It never computes Safe Today, pace, etc.

This is the single reference document: status, architecture, API and MCP contract, operations,
testing, decisions and open items. It contains no secrets.

- **Live:** `https://intoday.pkasemer.com` (home page at `/`, API under `/v1`)
- **Repo:** `git@github.com:pkasemer/intodayBudgetBackendwithMCP.git` (branch `main`)

**Contents:** [Status](#1-status) · [Architecture](#2-architecture) · [Repo layout](#3-repo-layout) ·
[API contract](#4-api-contract-for-the-ios-sync-client) · [MCP server](#5-mcp-server) ·
[Operating it](#6-operating-it) · [Testing](#7-testing) · [Security and privacy](#8-security-and-privacy) ·
[Decisions to confirm](#9-decisions-made-on-judgement-please-confirm) · [Open items](#10-open-items)

---

## 1. Status

Verified live on 27 Oct 2026:

- `GET /healthz` → 200 `{"status":"ok","db":"up","redis":"up"}`; HTTPS with HSTS; `http://` → 301 to `https://`.
- `GET /` → 200, the public home page (its metrics panel stays locked without the token). `GET /v1/stats` without a token → 401. `/docs` and any other non-API path → 404.
- No token or a wrong token → 401. The first production token (pasted into chat for testing) was **rotated**
  and the old one now returns 401. The owner has tested authenticated requests with the new token.
- Authenticated `GET /v1/sync/pull` and `GET /v1/sync/snapshot` → 200 on a still-empty database (0 records,
  cursor `czE6MA`).
- Containers `api`, `db`, `redis` healthy. A successful MCP handshake and tool call has been seen in the logs.

Not yet exercised against production: a real sync from the iOS app (the database is still empty), and MCP
write tools from a real Claude client. All of it is covered by the test suite and the live acceptance run
on the dev stack.

## 2. Architecture

```
iPhone app ──HTTPS──▶ nginx (host) ──▶ 127.0.0.1:8100 ──▶ api ──▶ db (Postgres)
Muse / MCP client ──HTTPS──▶ same                                └▶ redis (cache only)
```

- **Three services**, not the spec's four: there is no Caddy, because the VPS's existing nginx owns 80/443.
  Only `api` publishes a port, on loopback (`127.0.0.1:8100`, `API_PORT` in `.env`). Postgres and Redis
  publish nothing. TLS, HSTS and the redirect are nginx's job (Let's Encrypt via certbot).
- **Postgres** is the system of record (named volume `pgdata`). **Redis** is ephemeral only: the idempotency
  fast path and rate limiting. Losing Redis changes nothing but speed and limiting.
- **Migrations:** `api/migrations/*.sql`, applied by the api on start, once each (`schema_migrations`).
- **Logs:** JSON to stdout, counts and record IDs only — never amounts, notes, labels or the token.
- **Retention:** tombstones purged after 30 days, idempotency keys after 7; purge runs at start and every 6 h.
- **Usage metrics:** counters for API calls and MCP tool calls are batched in memory and flushed to Postgres every 30 s and on
  shutdown (`usage_daily`, `sync_status`; migration `002_usage`). Counts and timestamps only — never request contents.
  `DELETE /v1/account` erases them too.
- **Resource limits:** `api` 512 MiB / 0.5 CPU, `db` 1 GiB / 1 CPU, `redis` 256 MiB. Healthchecks and
  `restart: unless-stopped` on all.
- **No outbound calls:** no analytics, telemetry or third-party requests. Independent of the trading project
  on this VPS (separate Compose project, no shared files; only the host nginx is shared).

## 3. Repo layout

| Path | What |
|---|---|
| `docker-compose.yml` | Production stack (`api`, `db`, `redis`) |
| `docker-compose.test.yml` | Test overlay (no published ports) + `tests` runner |
| `api/app/sync.py` | LWW apply, push, pull, full snapshot, erase |
| `api/app/validation.py` | Record validation shared by push and MCP writes |
| `api/app/mcp.py`, `tools.py` | MCP JSON-RPC server and the fourteen tools |
| `api/app/guards.py` | Bearer auth + Redis rate limiting |
| `api/app/dashboard.py` | The month-in-full payload behind `GET /v1/dashboard` |
| `api/app/export.py` | CSV / zip export and the monthly PDF report (`GET /v1/export/*`; PDF via `fpdf2`) |
| `api/app/purge.py` | Tombstone and idempotency-key retention |
| `api/app/metrics.py` | Usage counters and activity events (batched, flushed to Postgres) and the numbers behind `GET /v1/stats` |
| `api/app/static/index.html` | Public home page at `/` (static, strict CSP, no external requests; the landing page plus, after the token is accepted, the dashboard view fed by `GET /v1/dashboard` and `GET /v1/stats`) |
| `api/migrations/` | Versioned SQL |
| `api/tests/` | 116 pytest tests (need Postgres + Redis; refuse to run outside the test project) |
| `scripts/` | `dev.sh`, `acceptance.sh`, `backup.sh`, `restore-check.sh`, `live_check.py` |
| `deploy/` | `intoday.pkasemer.com.conf` (HTTP-only nginx block), `install-nginx.sh` (installs it + certbot), `enable-homepage.sh` (routes `/` to the home page) |
| `.env.example` | Variable names only; `.env` and `.env.dev` are git-ignored |

## 4. API contract (for the iOS sync client)

Base `https://intoday.pkasemer.com`. JSON everywhere. All endpoints except `/healthz` and `/` need
`Authorization: Bearer <token>`.

Errors: `{"error":{"code":"validation|unauthorized|not_found|rate_limited|internal","message":"..."}}`
with the matching status (422 validation, 401, 404, 429 + `Retry-After`, 500, 413 for bodies over 4 MB).
Unknown JSON fields are ignored.

### Which endpoints the app uses

| Endpoint | When |
|---|---|
| `POST /v1/sync/push` | After every local change (debounced ~2 s) and on app foreground. Sends changed records and the app's computed snapshots. |
| `GET /v1/sync/pull?cursor=<cursor>` | On foreground and after each push. Fetches changes from other writers (e.g. MCP writes). |
| `GET /v1/sync/capabilities` | At the start of every sync cycle. `schema_version` 4 means the v0.4 tables and columns below are supported. |
| `GET /v1/sync/snapshot` | Only on a fresh install or explicit re-provision. Returns everything plus a starting cursor; the app confirms before replacing a non-empty local store. |

Flow: first sync is `/v1/sync/snapshot` → store its `cursor` → from then on push and pull with that cursor
(send it as the optional `cursor` field in the push body), storing each new cursor and paging while
`has_more`. Every push needs a fresh UUID `idempotency_key` (also sendable as the `Idempotency-Key`
header). A delete is a normal record with `deleted_at` set. Muse uses `POST /v1/mcp` instead; the app doesn't.

### Records

Every synced record carries `id` (UUID string), `updated_at` (RFC 3339 UTC), `device_id` (string, 1–128
chars; use a stable per-install UUID), `deleted_at` (null or timestamp). Twelve tables, in dependency order
(this is also the apply order inside a push, and the `tables` list of `/v1/sync/capabilities`):
`savings_goals`, `bill_templates`, `debts`, `month_plans`, `bills`, `expenses`, `income_events`,
`savings_allocations`, `loans`, `loan_repayments`, `debt_payments`, `category_limits`. Fields mirror backend spec
v0.3 §4 and v0.4 §3–4 field-for-field. Money is a whole-number integer.

### Schema 4 (backend spec v0.4)

`GET /v1/sync/capabilities` → `{"schema_version":4,"tables":[…the twelve…]}`. A v0.3 server answers 404; the app then
sends only v0.3 tables and fields. On first seeing 4 the app pulls, then pushes every row once with the new data.

- **New columns** on `month_plans` (`closed_days`, `rollover_amount`, `rollover_carried`, `rollover_goal_id`,
  `summary_shown`, `buffer_released_amount`, `buffer_released_on`, `reserved_debt_ids`, `created_at`), `bills`
  (`is_included`, `exclude_reason`, `debt_id`) and `income_events` (`is_recurring`, `is_rollover`).
- **New tables:** `debts`, `loans`, `loan_repayments`, `debt_payments`, `category_limits`. What is owed on a loan,
  and a debt's balance, are derived by the reader — never stored.
- **A missing key keeps the stored value.** For every column above that is new on an existing table (and for
  `debts.monthly_payment`, `loans.written_off`, `loan_repayments.landed_as_income`): key absent → default on
  insert, stored value kept on update; key present with `null` → clears a nullable column; `null` on a
  non-nullable column is rejected. Older app builds and MCP writes therefore never reset v0.4 data.
- `closed_days` is stored sorted and de-duplicated (each 1–31). `reserved_debt_ids` is a list of UUIDs with no FK.
- **References, on physical delete** (tombstones never trigger these): plan → `loans.plan_id` set null,
  `debt_payments` cascade; loan → `loan_repayments` cascade; debt → `bills.debt_id` and `debt_payments.debt_id` set
  null (a trigger, because those ids must be accepted before their debt arrives); goal →
  `month_plans.rollover_goal_id` set null. The 30-day purge covers all five new tables.
- **Reference checks on push:** `loan_repayments.loan_id` must name a stored live loan; a non-null `loans.plan_id` /
  `debt_payments.plan_id` must name a stored plan (a loan may name a *deleted* plan — it outlives it); `rollover_goal_id`
  must name a stored goal (deleted is fine); `bills.debt_id` and `debt_payments.debt_id` are stored as sent even when the
  debt is unknown. Repayments summing past a loan are logged (`loan_overpaid`, loan id only), never rejected.
- `category_limits`: one live row per `tag`. A second live row with another id for the same tag is rejected as a
  conflict; a tombstone frees the tag. The app derives one id per tag, so two phones write the same row.

Validation (per record; an invalid record is rejected alone, never the batch):

- Amounts are integers 1–9,999,999 (`buffer_amount` allows 0; `savings_goals.target_amount` allows 0 = no target;
  rollover and released-buffer amounts are 0–9,999,999). **Debts are wider:** `debts.amount` 1–999,999,999 and
  `monthly_payment` 0–999,999,999, not above `amount`. Floats, booleans, negatives and out-of-range digits are rejected.
- `person` / debt `name` non-empty ≤ 100; `kind` ∈ `person|loan`; `first_month` `YYYY-MM` with month 1–12;
  `exclude_reason` ≤ 50; `created_at` an RFC 3339 timestamp with a zone.
- `tag` ∈ `food|transport|dataAirtime|fun|shopping|other`. Dates are real `YYYY-MM-DD`.
- **Plan membership is by ownership, never by date** (product spec §5.10 rule 3). Plans are calendar months and
  `start_day` is stored but ignored. An **expense** must be dated inside its plan's calendar month; **income is
  exempt** (needs only a real date and an existing plan — salary dated 30 Oct can fund the November plan).
  Bills carry no date.
- `note` ≤ 200; income `label` required, non-blank, ≤ 100; `destination=lockedSavings` requires an existing
  `savings_goal_id`, `spendable` forbids it; `due_day` 1–31; enums are exact.
- Rejection reasons name the field, never the value.

### `POST /v1/sync/push`

```json
{ "device_id": "…", "idempotency_key": "uuid", "cursor": "<optional, your stored pull cursor>",
  "records": { "month_plans": [], "bills": [], "expenses": [], "income_events": [],
               "savings_goals": [], "savings_allocations": [], "bill_templates": [] },
  "snapshots": [ { "kind": "today", "plan_year": 2026, "plan_month": 10,
                   "payload": { }, "snapshot_version": 1 } ] }
```

Response: `{"applied":{table:n,…,"snapshots":n},"skipped":{…},"rejected":[{"table","id","reason"}],"cursor":"…"}`.
Header and body idempotency keys must match if both are sent. Retrying with the same key returns the stored
response and applies nothing. At most 5,000 records per push. Rejected snapshots use `"<kind>/<year>/<month>"`
as their `id`.

- **LWW:** a record applies only if its `updated_at` is newer, or equal with a greater `device_id`
  (C-collation string compare). Otherwise it is `skipped`.
- **Tombstones:** push with `deleted_at` set. Only the sync columns are needed to delete an existing row. A stale
  delete is `skipped`, not rejected.
- **Snapshots:** upsert by `(kind, plan_year, plan_month)`, same LWW rule. `kind` ∈ `today|insights`, `payload`
  must be a JSON object ≤ 64 KB. With no `updated_at`, the server uses `payload.as_of`, else server time.

### `GET /v1/sync/pull?cursor=<opaque>&limit=500`

Cursor is required (a first sync uses the snapshot endpoint). Response:
`{"cursor","has_more","records":{…all twelve tables…},"deleted":[{"table","id","deleted_at","updated_at","device_id"}],"snapshots":[…]}`.
Page until `has_more` is false. Tombstoned rows come back in `deleted`, never in `records`.

### `GET /v1/sync/snapshot`

Every live record plus live snapshots, consistent as of one moment, plus a `cursor` to resume pulling from.

### Cursor behaviour — read this

- Cursors walk a **server-assigned sequence**, not `updated_at`, so a client clock that goes backwards can't
  make pulls skip rows.
- The push response `cursor` is computed **before** the batch applies and stops short of any row written by
  another writer (e.g. an MCP write). After a push, your next pull echoes your own just-pushed rows back
  (harmless: same LWW rule) and **never skips an MCP write**. Send your stored cursor as `cursor` in the next
  push so it advances.
- MCP writes use `device_id = "mcp"`, and their `updated_at` is `max(now, row.updated_at + 1ms)`, so a phone
  with a fast clock can't silently overwrite them.

### Timestamps

RFC 3339 UTC with `Z`. Fractional seconds appear only when non-zero; MCP writes carry milliseconds.
**The client must parse both with and without fractional seconds.**

### Export (`GET /v1/export/csv`, `GET /v1/export/pdf`)

Token required; rate-limited like MCP (60/min); read-only; answered with `Content-Disposition: attachment` and `Cache-Control: no-store`.
The home page's *Export* section calls these (the token never leaves the tab), or use `curl.exe -H "Authorization: Bearer …" -o file URL`.

- **`/v1/export/csv?table=<name|all>&year=&month=&include_deleted=false`** — one table as CSV, or `all` (the default) as a zip with
  one CSV per table (all twelve, in dependency order). Columns are the stored columns; plan-owned tables (`bills`, `expenses`,
  `income_events`, `savings_allocations`, `loans`, `debt_payments`) get a leading `plan` column (`2026-10`). `year` and `month`
  go together and limit those tables to that plan month (loans not attached to a plan are then left out); goals, templates, debts,
  limits and loan repayments are always exported in full. Deleted rows are left out unless `include_deleted=true`. Files are UTF-8
  with a BOM (so Excel opens them correctly); lists are `;`-joined; booleans are `true`/`false`; text that starts with `= + - @`
  is prefixed with `'` so a spreadsheet cannot run it as a formula.
- **`/v1/export/pdf?year=&month=`** — a monthly report: summary sums, spending by category, income, bills, every expense, goals
  funded that month, money lent and still owed, and debts with derived balances (the last two are all-time). With no `year`/`month` it
  reports the latest plan; 404 if the month has no plan. These are plain sums of what is recorded, not the app's Today rules.
  The PDF uses built-in Latin-1 fonts, so emoji and non-Latin characters in names or notes print as `?` (the CSV keeps them).
- Each export adds an `export` entry to the activity feed (format, table, row count, month — never contents).

### Other endpoints

- `DELETE /v1/account` with header `X-Confirm: delete-all` erases every row of all twelve tables (the sequence isn't reset, so old
  cursors stay valid and return nothing). Without the header: 422, nothing deleted. No undo beyond the 7-day
  backup window.
- `GET /healthz` (no auth): `ok`, `degraded` (Redis down, still serving) or 503 `down` (database down).
- `GET /` (no auth): the static home page. It contains no data.
- `GET /v1/stats` (token required): the numbers shown on the home page, in the **Home page metrics** section below.

### The dashboard (home page after unlock)

`/` shows the landing page. *Open the ledger* asks for the token; once `/v1/dashboard` and `/v1/stats` accept it, the page swaps to
a dashboard in the same document (no new route, so no nginx change; the browser's back button and `#/tab` links work). The token
stays in that tab's `sessionStorage`, is sent only to this origin, and *Lock* forgets it and returns to the landing page. Reloading
with a stored token goes straight back to the dashboard. Data refreshes every 15 s while the tab is visible.

- **Overview:** six totals for the month (income received, spent, bills, saved, owed to you, you owe), the app's *Today* numbers
  (from its synced snapshot), daily spending, spending by category against any limits, and what's coming up (unpaid bills with days
  until due or late, expected income).
- **Spending:** categories, the daily chart and every expense with category filters.
- **Money:** income, bills with paid/unpaid state, savings goals, money lent (with late flags) and debts (progress toward paid off).
- **Reports:** a six-month income/spent/bills chart, the month's totals, the app's insights snapshot, and **Export** (CSV or the PDF).
- **System:** the operational view below (health, calls, responses, writers, rejected records, live feed).
- A month picker and arrows in the header switch the plan month; a banner appears on any tab when something needs attention.

`GET /v1/dashboard?year=&month=` (token required; not counted as an API call; `year`/`month` together or neither, default the
latest plan, 404 for a month without a plan) returns one month in full: `months`, `plan`, `totals`, `days`, `by_tag` (with limits),
`income`, `bills`, `expenses` (newest first, max 1,000), `goals`, `loans`, `debts`, a six-month `trend`, and the app's `today` and
`insights` snapshots passed through untouched. All figures are plain sums of stored data; the app's Today rules are richer.

### Operations (`GET /v1/stats`, the System tab)

The home page is public, so its numbers are not: the panel under the tagline is locked until you click *Open the ledger*
and enter the API token. The token is kept in that tab's `sessionStorage` only, is sent only to this origin (the CSP allows
no other `connect-src`), and *Close the ledger* forgets it. While open, the panel refreshes every 10 s.

Opened, it shows: things needing attention (or "All quiet"), the five headline numbers, service health (database, Redis, uptime,
schema and migration), calls per day for 14 days (API with the MCP share overlaid), responses today (answered / turned away /
rate limited / bad requests / server errors) with typical and slowest latency per kind, rows per table (live and deleted), who has
written (each phone and Claude/MCP), rejected records with the field-level reason, and a newest-first activity feed.

```json
{ "generated_at": "…", "counting_since": "2026-10-27",
  "api_calls": {"today": 12, "total": 340}, "mcp_calls": {"today": 3, "total": 41},
  "records": {"total": 58, "by_table": {"expenses": 40, …}, "snapshots": 2},
  "devices": 1,
  "last_sync": {"at": "…", "kind": "push|pull|snapshot", "last_push": "…", "last_pull": "…", "last_snapshot": "…"},
  "health": {"database": "up", "redis": "up|down", "uptime_seconds": 3600, "started_at": "…", "schema_version": 4, "migration": "004_activity"},
  "days": [{"day": "2026-10-28", "api": 12, "mcp": 3}, …14 entries…],
  "responses": {"today": {"ok","unauthorized","rate_limited","client_error","server_error"}, "window": {…}, "window_days": 14},
  "latency": {"push|pull|snapshot|mcp": {"n", "p50", "p95"}},
  "devices_seen": [{"device_id", "rows", "is_mcp", "last_push"}],
  "recent": [{"at", "kind", "device_id", "status", "ms", "detail"}],
  "rejections": [{"at", "device_id", "table", "id", "reason"}],
  "alerts": [{"level": "warn|bad", "text": "…"}] }
```

- **API calls:** authenticated requests to `/v1/*` (sync, MCP, account). Rejected (401/429) requests, `/healthz`, `/` and the
  stats poll itself are not counted. "Today" is the day in `APP_TIMEZONE` (default Africa/Kampala).
- **MCP calls:** `tools/call` executions, including ones that return a tool error. `initialize`, `tools/list` and `ping` are not.
- **Records:** live (non-deleted) rows per table; tombstones are not counted. **Devices:** distinct `device_id`s excluding `mcp`.
- **Responses:** HTTP statuses of `/v1/*` requests (not the stats poll, `/healthz` or `/`), counted per day in `usage_status`.
  `unauthorized` is 401, `rate_limited` 429, `client_error` any other 4xx, `server_error` 5xx. An erase request is not counted.
- **Activity feed** (`activity` table, migration 004; kept 14 days, purged with the tombstones): one row per push (applied /
  skipped / rejected counts, tables touched, and per rejected record its table, id and reason — reasons name a field, never a
  value; `replay` marks an idempotent retry), pull, snapshot, MCP tool call (tool name, read or write, ok, record id for writes),
  refused request (401/429, path only) and failed request (other 4xx/5xx, path and error type). Never amounts, notes or bodies.
  Refused and failed events are capped at 25 per 30 s flush window so a brute-force burst cannot flood the feed (the counters still
  count every one). Events are written in batches every 30 s; the panel also shows the not-yet-written ones.
- **Alerts:** Redis down, any 5xx today, rejected records in the last 24 h, rate-limited requests today, 10+ failed sign-ins today,
  no app sync for over a day.
- **Last synced:** the most recent successful push, pull or snapshot (`kind` says which). A `curl` pull with the token counts as a
  pull, so the panel shows any authenticated sync contact, not only the phone — the `last_push` field is the phone's writes.

### Rate limits (Redis, fixed 60 s window, per token)

120/min on sync endpoints (and on `/v1/stats`, in its own bucket), 60/min on `/v1/mcp`, separate buckets. If Redis is down, limiting is skipped and
logged (never fails closed). Bad-token attempts share one global counter (30/min → 429); a valid token is
never affected by it.

## 5. MCP server

`POST /v1/mcp` — Streamable HTTP, JSON-RPC 2.0, same bearer token. Stateless, plain-JSON responses, no session
IDs; notifications get 202. Implements `initialize` (negotiates 2025-06-18 / 2025-03-26 / 2024-11-05),
`tools/list`, `tools/call`, `ping` only. `GET /v1/mcp` returns `{"tools":[…]}`. Tool failures come back as
`isError: true` results; results carry both text content and `structuredContent`.

**Fourteen tools**: eleven read, three write. v0.3 had ten (the spec's §14 says "seven" but §8 lists seven read +
three write); `list_months` and the three v0.4 read tools (`list_loans`, `list_debts`, `get_category_limits`) were
added later. There is deliberately no SQL or schema tool.

| Tool | Input | Notes |
|---|---|---|
| `get_today` | `date?` | Latest `today` snapshot + `as_of`; if none: `snapshot:null`, a note, `expenses_today`, `bills_due` |
| `get_month_summary` | `year, month` | Plan row, counts, plain sums (spent, income received/expected, bills paid/unpaid), live snapshots |
| `list_expenses` | `year, month, tag?, limit?` | Newest first; limit default 50, max 200 |
| `list_bills` | `year, month, unpaid_only?` | |
| `list_income_events` | `year, month` | |
| `list_goals` | — | Per goal: `allocated_total`, `income_to_savings_received` (received income only), `locked_total` = sum |
| `get_insights` | `year, month` | Latest `insights` snapshot, else raw `tag_totals` |
| `list_months` | — | The plan months that exist, newest first, with `plan_state` (so a client can choose `year`/`month`) |
| `list_loans` | `open_only?` | Money lent: `repaid_total`, `remaining` (floored at 0), `status` (`open`/`repaid`/`written_off`), `late` (open, something owed, `due_on` before today); plus `owed_total` (open loans) and `late_count`. `open_only` hides repaid and written-off |
| `list_debts` | `open_only?` | Per debt: `paid_total` (paid included bills that name it + extra debt payments, as in the PDF), `balance` (floored at 0), `monthly_payment`, `due_day`, `next_unpaid_bill` (earliest unpaid, included bill naming it, with its plan month; else `null`); plus `balance_total`. `open_only` hides cleared debts |
| `get_category_limits` | `year?, month?` (both or neither) | All six tags with `limit` (null if none), `spent`, `remaining` (negative when over) and `over`, for the current month or the one given; `plan:null` and zero spend if the month has no plan |
| `add_expense` | `amount, tag, date, note?` | **Write.** Plan = the one whose calendar month holds `date`; `device_id:"mcp"`; returns row + `device_sync_pending:true` |
| `mark_bill_paid` | `bill_id` | **Write.** Only unpaid, live bills |
| `mark_income_received` | `income_event_id` | **Write.** Only `expected` rows |

Writes are single transactions through the same validation and LWW code as push. The phone sees them on its
next pull; the server never pushes to the device. Per the standing rule, approval happens in the Muse
conversation — a tool existing is not permission.

To connect a client: add `https://intoday.pkasemer.com/v1/mcp` as a custom MCP connector with a bearer token
header, entered in the client's own settings (never pasted into chat or committed).

## 6. Operating it

Run from `/home/pk/budget-mcp` on the VPS.

### First-time deploy (already done for production)

1. Create `.env` from `.env.example`. Generate both secrets yourself (`openssl rand -hex 32`) and never paste
   the production token into chat. Hex only: `DB_PASSWORD` is embedded in `DATABASE_URL`.
2. DNS: `intoday.pkasemer.com` points at this VPS (via Cloudflare; set SSL mode to *Full (strict)*).
3. `docker compose up -d --build` — migrations run on api start.
4. `sudo ./deploy/install-nginx.sh` — installs the nginx site (HTTP only), then `certbot --nginx` adds HTTPS and
   the redirect. It refuses to overwrite an existing site file and rolls back if `nginx -t` fails.
5. `sudo ./deploy/enable-homepage.sh` — routes `/` (exactly) to the home page; everything else outside `/v1/`
   and `/healthz` stays 404. Idempotent, keeps a backup, restores it if `nginx -t` fails.
6. Smoke: `curl -s https://intoday.pkasemer.com/healthz`. Put the token in the app's keychain, then sync.

### Everyday commands

```bash
docker compose ps                         # status
curl -s https://intoday.pkasemer.com/healthz
git pull && docker compose up -d --build  # deploy a change (api is stateless; db untouched)
```

**Watch requests live** (Ctrl+C to stop):

```bash
docker compose logs -f --tail 20 api
docker compose logs -f --tail 50 --no-log-prefix api | grep --line-buffered -v '"/healthz"'   # hide healthchecks
```

Look back with `docker compose logs --since 1h api`. Request lines carry method, path, status and timing only;
non-request events include `sync_push` (counts), `mcp_write` (tool + record ID), `purge`, `account_erased`,
`rate_limiting_disabled` and errors. A one-line-per-request view:

```bash
docker compose logs -f --tail 50 --no-log-prefix api | python3 -u -c '
import sys, json
for l in sys.stdin:
    try: d = json.loads(l)
    except ValueError: continue
    if d.get("msg") == "request" and d["path"] != "/healthz":
        print(d["ts"][11:19], d["method"], d["path"], d["status"], str(d["ms"]) + "ms")
    elif d.get("msg") != "request":
        print(d["ts"][11:19], "EVENT", d["msg"], {k: v for k, v in d.items() if k not in ("ts", "level", "msg")})
'
```

### Testing an authenticated request

Linux / macOS / Git Bash:

```bash
curl -s -H "Authorization: Bearer $API_TOKEN" "https://intoday.pkasemer.com/v1/sync/pull?cursor=czE6MA"
```

Windows PowerShell — plain `curl` is an alias for `Invoke-WebRequest` and rejects `-s`/`-H`, so use `curl.exe`
or `Invoke-RestMethod`:

```powershell
curl.exe -s -H "Authorization: Bearer <your-token>" "https://intoday.pkasemer.com/v1/sync/snapshot"
Invoke-RestMethod -Headers @{ Authorization = "Bearer <your-token>" } -Uri "https://intoday.pkasemer.com/v1/sync/snapshot"
```

### Rotate the token

Edit `API_TOKEN` in `.env`, then `docker compose up -d api` (recreates the container). The old token stops
working immediately (returns 401). Update the app and any MCP connector. Do this whenever a token has been
pasted somewhere it shouldn't be.

### Backups and restore

```bash
./scripts/backup.sh                        # /var/backups/intoday (or $BACKUP_DIR), 7 kept, files 0600
./scripts/restore-check.sh <dump file>     # restores to a scratch DB and compares row counts
```

`backup.sh` is **not scheduled yet**; the cron line is in its header (`/var/backups/intoday` needs root to
create — or set `BACKUP_DIR`). Real restore:
`docker compose exec -T db pg_restore -U intoday -d intoday --clean --if-exists < dump`. Dumps contain all
data; copy offsite only if you choose to (the backend never uploads anything). Target: data loss ≤ 24 h,
rebuild ≤ 1 h from the repo + latest dump.

## 7. Testing

```bash
scripts/dev.sh up -d --wait db redis    # separate project "intoday-dev", throwaway token in .env.dev
scripts/dev.sh build tests && scripts/dev.sh run --rm tests   # 116 tests (rebuild first: the image bakes in the code)
scripts/acceptance.sh                   # live checks: cold start, Redis outage, backup/restore, log hygiene
scripts/dev.sh down -v
```

The dev project publishes no ports at all and never touches production.

- **116 tests pass** (real Postgres + Redis, in-process ASGI): auth, push/pull round trip, paging, idempotent
  retry (Redis and Postgres paths), LWW incl. tie-break, tombstones and purge with an injected clock, snapshot
  round trip, erasure, every validation rule, ownership-based income dates, expense month edges, `start_day`
  ignored, all fourteen MCP tools, per-goal totals, usage counters and `/v1/stats` (incl. restart survival, erasure, the activity feed, rejection listing, response classes, capped auth bursts and alerts), Redis-down behaviour, rate limits, body cap, the home page and
  its headers, log hygiene, and the MCP-write-not-skipped cursor case. `test_v04.py` covers backend spec v0.4 acceptance items 15–20 and 22
  (item 21, an upgrade with a real phone, needs the app).
- **Live acceptance run passed:** cold start from empty volumes, Redis stopped/started under live traffic,
  backup + restore drill (row counts matched), no token or amounts in container logs.
- Spec §14 items 1–10 and 12–14 are covered by the above; item 11 (TLS/redirect/HSTS) was checked on the live
  host. Item 7's "equals the app's Today screen" needs a human with the app (pending).

## 8. Security and privacy

- One token, no accounts; compared in constant time, never logged, never returned, never in URLs.
- Only 127.0.0.1:8100 is published by Compose; Postgres and Redis are reachable only inside the Compose network.
- `.env` and `.env.dev` are git-ignored; the repo ships `.env.example` with names only. The builder did not
  create or hold the production secrets.
- No `/docs`, `/redoc` or `/openapi.json`. The home page is static, carries no data (metrics load only after you enter the token), makes no third-party
  requests, and ships with a strict CSP (own inline style, one hashed inline script, same-origin `/healthz` fetch).
- Logs never contain amounts, notes, labels or the token; 500s log only the exception type (driver messages can
  carry row values).
- Backups are 0600 and stay on the VPS unless you move them.

## 9. Decisions made on judgement (please confirm)

1. **No Caddy** — nginx already owns 80/443; the api binds loopback `:8100` only.
2. **Fourteen tools**: ten from v0.3 (spec §14 says seven, §8 lists ten) plus `list_months` and the three v0.4 read tools.
3. **Cursor ordering** by server sequence instead of `(updated_at, id)` (spec §5.1 and §5.3 conflict): a
   client-set `updated_at` can go backwards and would make an `updated_at` cursor skip late-arriving rows.
4. **Plan membership — resolved (27 Oct, product spec §5.10 rule 3):** by ownership; plans are calendar months;
   `start_day` ignored; income dates unchecked against the plan month; expenses must be in it; bills carry no date.
5. **`list_goals` totals are per goal:** allocations for that goal (`goal_id`) plus *received* income whose
   `savings_goal_id` is that goal — nothing global; spendable and `expected` income never count. Pinned by a
   two-goal test.
6. **Snapshot timestamp fallback:** a snapshot with no envelope `updated_at` (the spec's example has none) uses
   `payload.as_of`, else server time.
7. **Idempotency key is optional** (the spec's shape lists it, but nothing breaks without it).
8. **MCP write clock:** `max(now, row.updated_at + 1ms)` so a phone with a fast clock can't overwrite MCP writes.
9. **Failed-auth limiting:** bad tokens share one global counter (30/min → 429); a valid token is never affected.

### Backend v0.4 decisions

- **Capabilities returns 4**, and the three optional v0.4 MCP read tools (`list_loans`, `list_debts`, `get_category_limits`)
  are built, now that the owner has asked for them. `list_months` is an addition of mine: without it a client has no way to
  learn which `year`/`month` values exist. No new write tools.
- **Missing key vs `null`** is implemented per column, not per table (see Schema 4): v0.3 columns keep their v0.3
  behaviour (an omitted nullable column is written as null).
- **Lenient parents where the spec says "set null":** a loan may name a deleted plan, and a plan may name a deleted goal;
  both are only cleared by a physical delete. Everything else follows the v0.3 rule (a live child of a deleted parent is
  rejected).
- **Migration 003 changes `savings_goals.target_amount`'s check to 0–9,999,999.** Existing rows are unaffected. Rows the
  phone still holds from the 28 Oct rejection (the "Savings" goal and its dependants) are accepted on the upgrade push.

## 10. Open items

- **Backups are not scheduled** — add the cron line from `scripts/backup.sh`, then run `restore-check.sh` once
  against real data.
- **First real sync from the iOS app** and MCP write tools from a real Claude client haven't run against
  production yet.
- **iOS sync client:** not built (separate work item). The two sides must agree on `device_id` format (UUID
  string, stable per install, in the keychain) and the snapshot payload schemas (spec Appendix A; the
  `insights` fields are still to be fixed by the app developer).
- **Cloudflare SSL mode** should be *Full (strict)* since the name is proxied.
- **Spec says build only after v1 ships** — the owner explicitly gave the go-ahead to build now.
- **Git author** on the first commit is `pktest <test@gmail.com>`, not the owner's identity (amend + force-push
  if wanted). Confirm the GitHub repo is private.
- **Docker-image pins** (`postgres:17-alpine`, `redis:7-alpine`, `python:3.13-slim`, pinned Python deps) should
  be re-checked at the next deploy, per the spec.
