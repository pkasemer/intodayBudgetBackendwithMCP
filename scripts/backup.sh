#!/usr/bin/env bash
# Nightly pg_dump (custom format), keeping the newest 7 dumps. Dumps hold the whole database
# including snapshots and tombstones, so they are written 0600 and never leave this host.
#
# Env: BACKUP_DIR (default /var/backups/intoday), COMPOSE (default "docker compose")
# Cron example (as the user that runs docker):
#   15 2 * * * cd /home/pk/budget-mcp && ./scripts/backup.sh >> /var/log/intoday-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-/var/backups/intoday}"
COMPOSE="${COMPOSE:-docker compose}"
KEEP=7

umask 077
mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
tmp="$BACKUP_DIR/.intoday-$stamp.dump.partial"
$COMPOSE exec -T db pg_dump -U intoday -d intoday --format=custom > "$tmp"
[ -s "$tmp" ] || { echo "backup empty, aborting" >&2; rm -f "$tmp"; exit 1; }
mv "$tmp" "$BACKUP_DIR/intoday-$stamp.dump"

# Rotate: keep the newest $KEEP.
ls -1t "$BACKUP_DIR"/intoday-*.dump | tail -n +$((KEEP + 1)) | xargs -r rm -f --
echo "backup ok: intoday-$stamp.dump ($(ls -1 "$BACKUP_DIR"/intoday-*.dump | wc -l) kept)"
