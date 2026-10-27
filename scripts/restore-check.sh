#!/usr/bin/env bash
# Restore drill: load a dump into a scratch database inside the db container and compare
# per-table row counts with the live database. Never touches the live data.
# Usage: scripts/restore-check.sh /var/backups/intoday/intoday-<stamp>.dump
# Env: COMPOSE (default "docker compose")
set -euo pipefail
cd "$(dirname "$0")/.."
DUMP="${1:?usage: restore-check.sh <dump file>}"
COMPOSE="${COMPOSE:-docker compose}"
SCRATCH=intoday_restore_check
TABLES="month_plans savings_goals bill_templates bills expenses income_events savings_allocations snapshots idempotency_keys"

psql() { $COMPOSE exec -T db psql -U intoday -d "$1" -v ON_ERROR_STOP=1 -At -c "$2"; }
counts() {
  for t in $TABLES; do echo "$t $(psql "$1" "SELECT count(*) FROM $t")"; done
}

trap 'psql postgres "DROP DATABASE IF EXISTS $SCRATCH" >/dev/null 2>&1 || true' EXIT
psql postgres "DROP DATABASE IF EXISTS $SCRATCH" >/dev/null
psql postgres "CREATE DATABASE $SCRATCH" >/dev/null
$COMPOSE exec -T db pg_restore -U intoday -d "$SCRATCH" --no-owner --exit-on-error < "$DUMP"

live="$(counts intoday)"
restored="$(counts $SCRATCH)"
echo "--- live";     echo "$live"
echo "--- restored"; echo "$restored"
if [ "$live" = "$restored" ]; then
  echo "RESTORE CHECK PASSED: record counts match"
else
  echo "RESTORE CHECK DIFFERS (expected if data changed since the dump was taken)" >&2
  diff <(echo "$live") <(echo "$restored") >&2 || true
  exit 1
fi
