#!/usr/bin/env bash
#
# setup_us.sh — build the US road-network graph for the "us" profile.
#
# The full 50-state US drive network (~20M nodes, ~100M+ CCH shortcuts) does NOT
# fit this Mac's FalkorDB container: Docker Desktop's VM caps the container's RAM
# (7.65 GB by default; ~24 GB even if you max it on a 36 GB host), and the CCH
# build peak alone would need tens of GB. Measured footprint (edge-c, with CCH):
# ~757 B/node used_memory, ~1.5 KB/node RSS, ~3.5 shortcuts/node. That puts the
# ceiling at ~2.5-3M nodes on the stock 7.65 GB VM and ~10-12M on a bumped 24 GB.
#
# So this builds the LARGEST US region that fits: California by default (~1.25M
# nodes). Grow it by listing more Geofabrik US state slugs in STATES (they're
# merged with `osmium merge`, which needs osmium-tool) — but keep the node total
# under your VM's ceiling or the CCH build will be OOM-killed.
#
# Downloads the Geofabrik extract(s), runs the parse pipeline into data/us/, starts
# a dedicated FalkorDB container (edge-c, with CCH) on a RANDOM host port, bulk-loads
# graph 'us_roads', and builds the CCH. The chosen DB port is written to
# data/us/.dbport and printed with the ready-to-run serve command. Idempotent: skips
# download/parse if artifacts exist.
#
#   ./setup_us.sh                     California (default)
#   STATES="california nevada" ./setup_us.sh   merge CA+NV (needs osmium-tool)
#   ./setup_us.sh --reparse           force re-parsing the PBF
#   ./setup_us.sh stop                remove the US container
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="falkordb/falkordb:edge-c"
CONTAINER="falkordb-us"
GRAPH="us_roads"
APP_PORT="${APP_PORT:-8082}"
STATES="${STATES:-california}"            # space-separated Geofabrik US state slugs
GEO_BASE="https://download.geofabrik.de/north-america/us"
OUT="data/us"
VPY="$PWD/venv/bin/python"
step(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info(){ printf '    %s\n' "$*"; }

if [ "${1:-}" = "stop" ]; then docker rm -f "$CONTAINER" >/dev/null 2>&1 && echo "removed $CONTAINER" || echo "no container"; exit 0; fi
REPARSE=0; [ "${1:-}" = "--reparse" ] && REPARSE=1

# prerequisites: Docker must be installed and its daemon running
command -v docker >/dev/null 2>&1 || { echo "FATAL: docker not found — install Docker Desktop"; exit 1; }
docker info >/dev/null 2>&1 || { echo "FATAL: docker daemon not running — start Docker Desktop"; exit 1; }

# bootstrap the Python venv + deps if missing, so setup_us.sh is self-contained
if [ ! -x "$VPY" ]; then
  step "Creating Python venv + installing deps (pyosmium, redis, falkordb, bulk-loader, flask, numpy)"
  PY="$(command -v python3.12 || command -v python3 || true)"
  [ -n "$PY" ] || { echo "FATAL: python3 not found"; exit 1; }
  "$PY" -m venv venv
  "$VPY" -m pip install -q --upgrade pip
  "$VPY" -m pip install -q osmium redis falkordb falkordb-bulk-loader flask numpy
fi

# pick a free RANDOM host port (never the default 6379). Guard against an empty
# value: an empty port makes redis://localhost: silently fall back to 6379 and
# clobber whatever graph lives there.
PORT="$("$VPY" -c 'import socket;s=socket.socket();s.bind(("",0));p=s.getsockname()[1];s.close();print(p)')"
[ -n "$PORT" ] || { echo "FATAL: could not pick a random port"; exit 1; }

# -- resolve the source PBF: one state = its extract; several = an osmium merge --
read -r -a STATE_ARR <<< "$STATES"
if [ "${#STATE_ARR[@]}" -eq 1 ]; then
  PBF="us-${STATE_ARR[0]}-latest.osm.pbf"
else
  PBF="us-$(IFS=-; echo "${STATE_ARR[*]}")-latest.osm.pbf"   # e.g. us-california-nevada-latest.osm.pbf
fi

step "Fetching US extract(s): ${STATES}"
declare -a PARTS=()
for st in "${STATE_ARR[@]}"; do
  f="us-${st}-latest.osm.pbf"
  if [ ! -f "$f" ] || [ "$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)" -lt 1000000 ]; then
    info "downloading $st ..."
    curl -L --fail -o "$f" "$GEO_BASE/${st}-latest.osm.pbf"
  else info "using existing $f"; fi
  PARTS+=("$f")
done

if [ "${#PARTS[@]}" -gt 1 ]; then
  if ! command -v osmium >/dev/null 2>&1; then
    echo "FATAL: merging ${#PARTS[@]} states needs osmium-tool. Install it with:"
    echo "         brew install osmium-tool"
    echo "       (or set STATES to a single state, e.g. STATES=california)"
    exit 1
  fi
  if [ "$REPARSE" = 1 ] || [ ! -f "$PBF" ]; then
    step "Merging ${#PARTS[@]} state extracts -> $PBF"
    osmium merge "${PARTS[@]}" -o "$PBF" --overwrite
  else info "using existing merged $PBF"; fi
fi

step "Parsing road graph + places -> $OUT/"
mkdir -p "$OUT"
if [ "$REPARSE" = 1 ] || [ ! -f "$OUT/nodes.csv" ] || [ ! -f "$OUT/geom.json" ]; then
  "$VPY" parse_pbf.py "$PBF" "$OUT"
else info "reusing $OUT/{nodes,roads}.csv, geom.json, names.json"; fi
if [ "$REPARSE" = 1 ] || [ ! -f "$OUT/places.json" ]; then
  "$VPY" parse_places.py "$PBF" "$OUT/places.json"
else info "reusing $OUT/places.json"; fi

step "Starting FalkorDB ($IMAGE) on random port :$PORT"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
# Raise TIMEOUT_MAX (so explicit long timeouts are honoured) but do NOT set a low
# TIMEOUT_DEFAULT — that would kill the CCH build and big-graph reads.
docker run -d --rm --name "$CONTAINER" -p "$PORT:6379" \
  -e FALKORDB_ARGS="THREAD_COUNT 8 TIMEOUT_MAX 1200000" "$IMAGE" >/dev/null
echo "$PORT" > "$OUT/.dbport"
for i in $(seq 1 60); do
  [ "$(docker exec "$CONTAINER" redis-cli ping 2>/dev/null)" = "PONG" ] && { info "ready after ${i}s"; break; }
  sleep 1; [ "$i" = 60 ] && { echo "container not ready"; exit 1; }
done
docker exec "$CONTAINER" redis-cli GRAPH.CONFIG SET RESULTSET_SIZE -1 >/dev/null

step "Adding per-edge drive time (roads.csv gains a 'time' column)"
"$VPY" add_traveltime.py "$OUT/roads.csv" "$OUT/names.json"

step "Bulk-loading graph '$GRAPH'"
"$PWD/venv/bin/falkordb-bulk-insert" "$GRAPH" -u "redis://localhost:$PORT" \
  -N Intersection "$OUT/nodes.csv" -R ROAD "$OUT/roads.csv" -j INTEGER -i Intersection:osmid
# CCH is its own graph-level path index; the bulk loader already created the osmid
# index, so no extra graph index is needed for routing.

step "Building CCH index (CREATE CCH INDEX)"
# run_cch.py builds the index (generous CCH_TIMEOUT for big graphs, well under
# TIMEOUT_MAX) via DDL: CREATE CCH INDEX FOR ()-[e:ROAD]->() ON (e.time).
# If this step gets OOM-killed, the region is too big for your Docker VM: raise
# Docker Desktop's memory (Settings > Resources) or drop states from STATES.
GRAPH="$GRAPH" FALKOR_PORT="$PORT" CCH_WEIGHT_PROP=time "$VPY" run_cch.py

printf '\n\033[1;32mUS (%s) ready on DB port %s.\033[0m\n' "$STATES" "$PORT"
printf '\033[1;32mServe with:  FALKOR_PORT=%s PROFILE=us PORT=%s %s app.py\033[0m\n' "$PORT" "$APP_PORT" "$VPY"
printf '    (DB port also saved to %s)\n' "$OUT/.dbport"
