#!/usr/bin/env bash
# Let nginx pass "/" (exactly) to the api, which serves the static home page. Run with sudo:
#   sudo ./deploy/enable-homepage.sh
# Edits only the intoday site file: inserts `location = / { ... }` before each existing
# `location / { return 404; }`. Idempotent, keeps a backup, runs `nginx -t` first, and restores
# the backup (no reload) if the test fails.
set -euo pipefail
SITE=/etc/nginx/sites-available/intoday.pkasemer.com

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -f "$SITE" ] || { echo "$SITE not found; run install-nginx.sh first" >&2; exit 1; }
if grep -qE 'location[[:space:]]*=[[:space:]]*/[[:space:]]*\{' "$SITE"; then
  echo "home page route already present; nothing to do"; exit 0
fi
grep -q 'location / { return 404; }' "$SITE" || { echo "expected 'location / { return 404; }' not found; edit $SITE by hand" >&2; exit 1; }

backup="$SITE.bak.$(date +%s)"
cp -p "$SITE" "$backup"
python3 - "$SITE" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
block = (
    "location = / {\n"
    "        proxy_pass http://127.0.0.1:8100;\n"
    "        proxy_http_version 1.1;\n"
    "        proxy_set_header Host $host;\n"
    "        proxy_set_header X-Forwarded-Proto $scheme;\n"
    "    }\n"
    "    location / { return 404; }"
)
open(path, "w").write(text.replace("location / { return 404; }", block))
PY
if ! nginx -t; then
  cp -p "$backup" "$SITE"
  echo "nginx -t failed; restored the original, nothing reloaded" >&2
  exit 1
fi
systemctl reload nginx
echo "done. backup kept at $backup"
