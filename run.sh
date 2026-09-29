#!/usr/bin/env bash
#
# run.sh — build (if needed) and serve the California FalkorDB CCH road-network demo.
#
#   ./run.sh         build the graph + CCH the first time, then serve the web map
#   ./run.sh stop    stop the web app and remove the DB container
#
# The heavy lifting (download, parse, load, CCH build) lives in setup_us.sh; run.sh
# just calls it when the graph isn't built yet, then serves app.py on the DB port
# that setup_us.sh chose (saved to data/us/.dbport). Safe to re-run.

set -euo pipefail
cd "$(dirname "$0")"

APP_PORT="${PORT:-8082}"
OUT="data/us"
CONTAINER="falkordb-us"
VPY="$PWD/venv/bin/python"

# ---- teardown -------------------------------------------------------------
if [ "${1:-}" = "stop" ]; then
  pkill -f "$PWD/venv/bin/python app.py" 2>/dev/null && echo "stopped web app" || true
  lsof -ti:"$APP_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
  ./setup_us.sh stop || true
  exit 0
fi

# ---- build the California graph + CCH on first run -------------------------
# (setup_us.sh creates the venv, downloads the extract, starts FalkorDB edge-c,
#  bulk-loads, and builds the CCH index — see setup_us.sh)
if [ ! -f "$OUT/roads.csv" ] || [ ! -f "$OUT/.dbport" ] || \
   ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^$CONTAINER$"; then
  ./setup_us.sh
fi

DB_PORT="$(cat "$OUT/.dbport" 2>/dev/null || true)"
[ -n "$DB_PORT" ] || { echo "could not read $OUT/.dbport — run ./setup_us.sh"; exit 1; }

# ---- serve the web map ----------------------------------------------------
lsof -ti:"$APP_PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true   # free a stale port

printf '\n\033[1;32mCalifornia road-network CCH demo → http://localhost:%s  (DB port %s)\033[0m\n' \
  "$APP_PORT" "$DB_PORT"
printf '   Ctrl-C stops the web app; "./run.sh stop" tears everything down.\n\n'

FALKOR_PORT="$DB_PORT" PROFILE=us PORT="$APP_PORT" exec "$VPY" app.py
