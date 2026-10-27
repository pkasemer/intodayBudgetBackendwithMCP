# InToday backend

Sync + MCP service for the InToday iOS app (backend spec v0.1, 27 Oct 2026). One user, one bearer
token. FastAPI + Postgres 17 + Redis 7 in Docker Compose. The app is the only money-maths engine;
this service mirrors records, stores the app's computed snapshots, and exposes domain MCP tools.

**Live at `https://intoday.pkasemer.com`** (home page at `/`, API under `/v1`). For the full API and MCP
contract, decisions and open items, see [HANDOFF.md](HANDOFF.md) — the two documents are kept in step.

## Layout

| Path | What |
|---|---|
| `docker-compose.yml` | `api`, `db`, `redis`. `api` publishes **127.0.0.1 only** (default `:8100`). |
| `docker-compose.test.yml` | test overlay (no published ports) + `tests` runner |
| `api/app/` | FastAPI app: `sync.py` (LWW, push/pull/snapshot), `mcp.py` + `tools.py` (MCP), `guards.py` (auth, rate limits), `validation.py` (shared by push and MCP writes) |
| `api/migrations/` | Versioned SQL, applied by the api on start, once each (`schema_migrations`) |
| `api/tests/` | pytest suite (needs Postgres + Redis; refuses to run outside the test project) |
| `scripts/` | `dev.sh`, `acceptance.sh`, `backup.sh`, `restore-check.sh`, `live_check.py` |
| `api/app/static/index.html` | The public home page served at `/` (static, no data, no third-party requests) |
| `deploy/intoday.pkasemer.com.conf`, `deploy/install-nginx.sh`, `deploy/enable-homepage.sh` | HTTP-only nginx block, the sudo script that installs it and runs certbot, and the sudo script that routes `/` to the home page |

## Deviation from the spec: no Caddy

This VPS already runs nginx on 80/443, so the stack has three services, not four. TLS, HSTS and
the redirect are nginx's job; see `deploy/`. Everything else follows the spec
(only the proxy-facing port is published, Postgres/Redis publish nothing).

## Deploy (on the VPS)

This is the procedure used for the current production deployment (live since 27 Oct 2026).

1. Create `.env` from `.env.example`. Generate both secrets yourself (`openssl rand -hex 32`) and
   never paste the production token into chat. `.env` is git-ignored.
2. DNS: `intoday.pkasemer.com` must point at this VPS (it does, via Cloudflare). Start the stack first:
   `docker compose up -d --build` — migrations run on api start.
3. `sudo ./deploy/install-nginx.sh` — installs the nginx site (HTTP only), then `certbot --nginx` adds
   HTTPS and the redirect. If Cloudflare proxies the name, set its SSL mode to *Full (strict)* afterwards.
4. `sudo ./deploy/enable-homepage.sh` — routes `/` (exactly) to the api's static home page. Everything else
   outside `/v1/` and `/healthz` stays a 404.
5. Smoke: `curl -s https://intoday.pkasemer.com/healthz` → `{"status":"ok","db":"up","redis":"up"}`.
   (`degraded` = Redis down; sync and MCP still work. `503`/`down` = database down.)
6. Put the token in the app's keychain, then do the first sync.

Logs: `docker compose logs -f api` (JSON, record IDs and counts only — never amounts, notes, labels
or the token). Health: `/healthz`.

### Rotate the token

Generate a new one, edit `API_TOKEN` in `.env`, then `docker compose up -d api` (recreates the
container with the new value). The old token stops working immediately. Update the app.

### Backups and restore

```bash
./scripts/backup.sh                        # /var/backups/intoday, 7 kept, files 0600
./scripts/restore-check.sh <dump file>     # restores to a scratch DB and compares row counts
```

`backup.sh` is not scheduled yet; the cron line is in its header. Restore for real:
`docker compose exec -T db pg_restore -U intoday -d intoday --clean --if-exists < dump`.
Dumps contain all data; copy offsite only if you choose to (the backend never uploads anything).

### Erase everything

`DELETE /v1/account` with the token **and** header `X-Confirm: delete-all`. No undo (beyond the
7-day backup window).

## Secrets

`.env` and `.env.dev` are git-ignored. The builder did not create or hold the production secrets; the owner
created `.env` on the server. The production token was later pasted into chat once to run a single read-only
pull — **rotate it** (see above) if that transcript might be shared.

## Develop and test

```bash
scripts/dev.sh up -d --wait db redis    # separate project "intoday-dev", dev token from .env.dev
scripts/dev.sh run --rm tests           # 51 tests
scripts/acceptance.sh                   # live checks: cold start, Redis outage, backup/restore, log hygiene
scripts/dev.sh down -v
```

`.env.dev` holds a throwaway dev token and DB password (git-ignored). The dev project publishes no
ports at all.

## Contract notes for the iOS sync client

- Push returns a `cursor`; send your stored cursor back as optional `cursor` in the push body so
  the server can advance past your own writes without ever skipping an MCP write. The push cursor
  is computed *before* the batch applies, so your next pull echoes your own just-pushed rows back
  (harmless: same LWW rule). Pull cursors are opaque; pull requires one (first sync uses
  `/v1/sync/snapshot`, which also returns a cursor).
- Timestamps are RFC 3339 UTC with a `Z`. Fractional seconds appear only when non-zero, and MCP
  writes carry milliseconds — the client must parse both with and without fractions.
- Tombstones come back in `deleted` (with `deleted_at`, `updated_at`, `device_id`), not in `records`.
- Push responses include a `snapshots` key in `applied`/`skipped`; rejected snapshots use
  `"<kind>/<year>/<month>"` as their `id`.

## Spec points resolved by judgement (please confirm)

- **Tool count.** §14 says `tools/list` returns "exactly the seven tools in §8", but §8 lists seven
  read tools plus three write tools. All ten are exposed; there is no SQL or schema tool.
- **Cursor ordering.** §5.3 says pull is ordered by `(updated_at, id)`, §5.1 says the server clock is
  authoritative for cursors. A client-set `updated_at` can go backwards, which would make a
  `(updated_at, id)` cursor skip late-arriving rows, so cursors walk a server-assigned `seq` column.
- **Plan membership (resolved 27 Oct).** Records belong to a plan by ownership (`plan_id`), never by date (product spec §5.10 rule 3).
  Plans are calendar months and `start_day` is stored but never used in validation. The only date rule is that an
  *expense* must be dated inside its plan's calendar month; *income* needs just a real date and an existing plan
  (so salary dated 30 Oct can fund the November plan). `add_expense` picks the plan whose calendar month holds the date.
- **Goal totals.** `list_goals` totals are per goal: allocations for that goal (`goal_id`) plus *received* income
  whose `savings_goal_id` is that goal, nothing global; spendable and `expected` income never count. Parts are
  exposed separately (`allocated_total`, `income_to_savings_received`, `locked_total`). Pinned by a two-goal test.
- **Snapshot timestamp fallback.** A snapshot with no envelope `updated_at` (the spec's push example has none)
  uses `payload.as_of`, else server time.
- **Idempotency key is optional.** The spec's shape lists it, but nothing breaks without it.
- **MCP write clock.** MCP writes use `max(now, row.updated_at + 1ms)` so a phone with a fast clock
  can't silently overwrite them.
- **Failed-auth limiting.** Bad tokens share one global counter (30/min → `429`); a valid token is
  never affected.
