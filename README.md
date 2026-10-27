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
- `GET /` → 200, the public home page. `/docs` and any other non-API path → 404.
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
| `api/app/mcp.py`, `tools.py` | MCP JSON-RPC server and the ten tools |
| `api/app/guards.py` | Bearer auth + Redis rate limiting |
| `api/app/purge.py` | Tombstone and idempotency-key retention |
| `api/app/static/index.html` | Public home page at `/` (static, strict CSP, no external requests) |
| `api/migrations/` | Versioned SQL |
| `api/tests/` | 51 pytest tests (need Postgres + Redis; refuse to run outside the test project) |
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
| `GET /v1/sync/snapshot` | Only on a fresh install or explicit re-provision. Returns everything plus a starting cursor; the app confirms before replacing a non-empty local store. |

Flow: first sync is `/v1/sync/snapshot` → store its `cursor` → from then on push and pull with that cursor
(send it as the optional `cursor` field in the push body), storing each new cursor and paging while
`has_more`. Every push needs a fresh UUID `idempotency_key` (also sendable as the `Idempotency-Key`
header). A delete is a normal record with `deleted_at` set. Muse uses `POST /v1/mcp` instead; the app doesn't.

### Records

Every synced record carries `id` (UUID string), `updated_at` (RFC 3339 UTC), `device_id` (string, 1–128
chars; use a stable per-install UUID), `deleted_at` (null or timestamp). Tables, in dependency order:
`month_plans`, `savings_goals`, `bill_templates`, `bills`, `expenses`, `income_events`,
`savings_allocations`. Fields mirror spec §4 field-for-field. Money is a whole-number integer.

Validation (per record; an invalid record is rejected alone, never the batch):

- Amounts are integers 1–9,999,999 (`buffer_amount` allows 0). Floats, booleans, zero, negatives and 8+
  digits are rejected.
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
`{"cursor","has_more","records":{…all seven tables…},"deleted":[{"table","id","deleted_at","updated_at","device_id"}],"snapshots":[…]}`.
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

### Other endpoints

- `DELETE /v1/account` with header `X-Confirm: delete-all` erases every row (the sequence isn't reset, so old
  cursors stay valid and return nothing). Without the header: 422, nothing deleted. No undo beyond the 7-day
  backup window.
- `GET /healthz` (no auth): `ok`, `degraded` (Redis down, still serving) or 503 `down` (database down).
- `GET /` (no auth): the static home page.

### Rate limits (Redis, fixed 60 s window, per token)

120/min on sync endpoints, 60/min on `/v1/mcp`, separate buckets. If Redis is down, limiting is skipped and
logged (never fails closed). Bad-token attempts share one global counter (30/min → 429); a valid token is
never affected by it.

## 5. MCP server

`POST /v1/mcp` — Streamable HTTP, JSON-RPC 2.0, same bearer token. Stateless, plain-JSON responses, no session
IDs; notifications get 202. Implements `initialize` (negotiates 2025-06-18 / 2025-03-26 / 2024-11-05),
`tools/list`, `tools/call`, `ping` only. `GET /v1/mcp` returns `{"tools":[…]}`. Tool failures come back as
`isError: true` results; results carry both text content and `structuredContent`.

**Ten tools** (the spec's §14 says "seven" but §8 lists seven read + three write; all ten are exposed). There is
deliberately no SQL or schema tool.

| Tool | Input | Notes |
|---|---|---|
| `get_today` | `date?` | Latest `today` snapshot + `as_of`; if none: `snapshot:null`, a note, `expenses_today`, `bills_due` |
| `get_month_summary` | `year, month` | Plan row, counts, plain sums (spent, income received/expected, bills paid/unpaid), live snapshots |
| `list_expenses` | `year, month, tag?, limit?` | Newest first; limit default 50, max 200 |
| `list_bills` | `year, month, unpaid_only?` | |
| `list_income_events` | `year, month` | |
| `list_goals` | — | Per goal: `allocated_total`, `income_to_savings_received` (received income only), `locked_total` = sum |
| `get_insights` | `year, month` | Latest `insights` snapshot, else raw `tag_totals` |
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
scripts/dev.sh run --rm tests           # 51 tests
scripts/acceptance.sh                   # live checks: cold start, Redis outage, backup/restore, log hygiene
scripts/dev.sh down -v
```

The dev project publishes no ports at all and never touches production.

- **51 tests pass** (real Postgres + Redis, in-process ASGI): auth, push/pull round trip, paging, idempotent
  retry (Redis and Postgres paths), LWW incl. tie-break, tombstones and purge with an injected clock, snapshot
  round trip, erasure, every validation rule, ownership-based income dates, expense month edges, `start_day`
  ignored, all ten MCP tools, per-goal totals, Redis-down behaviour, rate limits, body cap, the home page and
  its headers, log hygiene, and the MCP-write-not-skipped cursor case.
- **Live acceptance run passed:** cold start from empty volumes, Redis stopped/started under live traffic,
  backup + restore drill (row counts matched), no token or amounts in container logs.
- Spec §14 items 1–10 and 12–14 are covered by the above; item 11 (TLS/redirect/HSTS) was checked on the live
  host. Item 7's "equals the app's Today screen" needs a human with the app (pending).

## 8. Security and privacy

- One token, no accounts; compared in constant time, never logged, never returned, never in URLs.
- Only 127.0.0.1:8100 is published by Compose; Postgres and Redis are reachable only inside the Compose network.
- `.env` and `.env.dev` are git-ignored; the repo ships `.env.example` with names only. The builder did not
  create or hold the production secrets.
- No `/docs`, `/redoc` or `/openapi.json`. The home page is static, carries no data, makes no third-party
  requests, and ships with a strict CSP (own inline style, one hashed inline script, same-origin `/healthz` fetch).
- Logs never contain amounts, notes, labels or the token; 500s log only the exception type (driver messages can
  carry row values).
- Backups are 0600 and stay on the VPS unless you move them.

## 9. Decisions made on judgement (please confirm)

1. **No Caddy** — nginx already owns 80/443; the api binds loopback `:8100` only.
2. **Ten tools**, not seven (spec §14 vs §8).
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
