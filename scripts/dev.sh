#!/usr/bin/env bash
# Run docker compose against the local dev project (separate name, dev token, no published ports).
# Usage: scripts/dev.sh up -d db redis | scripts/dev.sh run --rm tests | scripts/dev.sh down -v
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose -p intoday-dev --env-file .env.dev -f docker-compose.yml -f docker-compose.test.yml "$@"
