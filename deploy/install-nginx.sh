#!/usr/bin/env bash
# One-time nginx + Let's Encrypt setup for intoday.pkasemer.com. Run with sudo:
#   sudo ./deploy/install-nginx.sh
# Touches only its own site file and symlink; refuses to overwrite an existing one, runs
# `nginx -t` before any reload, and rolls its own change back if the test fails.
set -euo pipefail
DOMAIN=intoday.pkasemer.com
SRC="$(cd "$(dirname "$0")" && pwd)/$DOMAIN.conf"
AVAIL="/etc/nginx/sites-available/$DOMAIN"
ENABLED="/etc/nginx/sites-enabled/$DOMAIN"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ ! -e "$AVAIL" ] && [ ! -e "$ENABLED" ] || { echo "$DOMAIN is already installed; not overwriting" >&2; exit 1; }
curl -fsS --max-time 5 http://127.0.0.1:8100/healthz >/dev/null \
  || echo "warning: the api is not answering on 127.0.0.1:8100 yet (start the stack before testing the site)" >&2

install -m 644 "$SRC" "$AVAIL"
ln -s "$AVAIL" "$ENABLED"
if ! nginx -t; then
  rm -f "$ENABLED" "$AVAIL"
  echo "nginx -t failed; removed the new site, nothing reloaded" >&2
  exit 1
fi
systemctl reload nginx
echo "HTTP site live. Requesting certificate..."
certbot --nginx -d "$DOMAIN" --redirect
nginx -t && systemctl reload nginx
echo "Done. Check: curl -s https://$DOMAIN/healthz"
