#!/usr/bin/env bash
# Live acceptance run against a fresh Compose stack (project intoday-dev, dev token, no ports).
# Covers backend spec §14 items 1, 9, 12 plus log hygiene; the rest run in the pytest suite
# (scripts/dev.sh run --rm tests). TLS (item 11) is nginx's job and is checked at deploy time.
set -euo pipefail
cd "$(dirname "$0")/.."
DEV="scripts/dev.sh"
live() { $DEV exec -T api python - "$@" < scripts/live_check.py; }
step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

step "1. cold start from empty volumes"
$DEV down -v >/dev/null 2>&1 || true
$DEV up -d --build --wait api >/dev/null
out="$(live health)"; echo "$out"
[ "$out" = '[200, {"status": "ok", "db": "up", "redis": "up"}]' ] || fail "healthz not ok"
if $DEV logs api | grep -E '"level": "(error|warning)"'; then fail "errors/warnings in api logs after cold start"; fi
echo "PASS: healthz ok, no errors in logs"

step "9. redis down: sync + MCP keep working, limiting reports disabled, then resumes"
$DEV stop redis >/dev/null
live health
live smoke || fail "api broken while redis is down"
$DEV logs api | grep -q 'rate_limiting_disabled' || fail "expected rate_limiting_disabled in logs"
$DEV start redis >/dev/null
sleep 3
out="$(live health)"; echo "$out"
[ "$out" = '[200, {"status": "ok", "db": "up", "redis": "up"}]' ] || fail "redis not back"
live smoke >/dev/null || fail "api broken after redis restart"
echo "PASS: redis outage tolerated and recovered"

step "12. backup + restore drill"
BACKUPS="$(mktemp -d)"
COMPOSE="$DEV" BACKUP_DIR="$BACKUPS" scripts/backup.sh
dump="$(ls -1 "$BACKUPS"/intoday-*.dump | head -1)"
[ "$(stat -c %a "$dump")" = "600" ] || fail "dump is not 0600"
COMPOSE="$DEV" scripts/restore-check.sh "$dump"
rm -rf "$BACKUPS"
echo "PASS: restore matches live counts"

step "log hygiene: token and amounts absent from container logs"
token="$(grep '^API_TOKEN=' .env.dev | cut -d= -f2)"
if $DEV logs api | grep -qF "$token"; then fail "token found in logs"; fi
if $DEV logs api | grep -q '15000'; then fail "amount found in logs"; fi
echo "PASS: no token, no amounts in logs"

step "exposure: published ports"
$DEV ps --format '{{.Name}} {{.Ports}}'
echo
echo "ALL LIVE CHECKS PASSED"
