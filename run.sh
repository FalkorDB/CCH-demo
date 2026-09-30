#!/usr/bin/env bash
#
# run.sh — build (if needed) and serve the California FalkorDB CCH road-network demo.
#
#   ./run.sh         build the graph + CCH the first time, then serve the web map
#   ./run.sh stop    stop the web app and remove the FalkorDB container
#
# The heavy lifting (parse, load, CCH build) lives in setup_us.sh, which starts a
# FalkorDB Docker container (falkordb/falkordb:v4.22.0) on DB_PORT and is idempotent —
# a re-run reuses an already-loaded graph. run.sh calls it, then serves app.py on that
# DB port (saved to data/us/.dbport). Safe to re-run.

set -euo pipefail
cd "$(dirname "$0")"

APP_PORT="${PORT:-8082}"
DB_PORT="${FALKOR_PORT:-6500}"          # host port for this demo's dedicated FalkorDB container
OUT="data/us"
VPY="$PWD/venv/bin/python"

# ---- teardown -------------------------------------------------------------
if [ "${1:-}" = "stop" ]; then
  pkill -f "$PWD/venv/bin/python app.py" 2>/dev/null && echo "stopped web app" || true
  lsof -ti:"$APP_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
  FALKOR_PORT="$DB_PORT" ./setup_us.sh stop || true   # remove the FalkorDB container
  exit 0
fi

# ---- build the California graph + CCH on first run -------------------------
# setup_us.sh starts the FalkorDB container, bulk-loads the graph, and builds the CCH index.
# It's idempotent: if the graph + CCH are already loaded on DB_PORT it just re-serves.
FALKOR_PORT="$DB_PORT" ./setup_us.sh

DB_PORT="$(cat "$OUT/.dbport" 2>/dev/null || echo "$DB_PORT")"
[ -n "$DB_PORT" ] || { echo "could not read $OUT/.dbport — run ./setup_us.sh"; exit 1; }

# ---- serve the web map ----------------------------------------------------
lsof -ti:"$APP_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true   # free a stale port

printf '\n\033[1;32mCalifornia road-network CCH demo → http://localhost:%s  (DB port %s)\033[0m\n' \
  "$APP_PORT" "$DB_PORT"
printf '   Ctrl-C stops the web app; "./run.sh stop" tears everything down.\n\n'

FALKOR_PORT="$DB_PORT" PROFILE=us PORT="$APP_PORT" exec "$VPY" app.py
