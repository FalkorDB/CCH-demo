#!/usr/bin/env bash
#
# setup_us.sh — build the US road-network graph for the "us" profile.
#
# The full 50-state US drive network (~20M nodes, ~100M+ CCH shortcuts) does NOT
# fit a typical FalkorDB VM: the CCH build peak alone would need tens of GB. Measured
# footprint (with CCH): ~757 B/node used_memory, ~1.5 KB/node RSS, ~3.5 shortcuts/
# node. On ~8-12 GB that puts the ceiling around ~2.5-4M nodes.
#
# So this builds the LARGEST US region that comfortably fits: California by default
# (~1.25M nodes, ~3 GB CCH build-peak). Grow it by listing more Geofabrik US state
# slugs in STATES (they're merged with `osmium merge`, which needs osmium-tool) — but
# keep the node total under your RAM ceiling or the CCH build will be OOM-killed.
#
# Downloads the Geofabrik extract(s), runs the parse pipeline into data/us/, starts a
# dedicated FalkorDB Docker container (falkordb/falkordb:v4.22.0 — ships the CCH path
# index) on DB_PORT (default 6500), generously resourced, bulk-loads graph 'us_roads',
# and builds the CCH. The DB port is written to data/us/.dbport and printed with the
# ready-to-run serve command. Idempotent: skips download/parse if artifacts exist, and
# skips load+CCH if the graph is already loaded.
#
#   ./setup_us.sh                     California (default)
#   STATES="california nevada" ./setup_us.sh   merge CA+NV (needs osmium-tool)
#   ./setup_us.sh --reparse           force re-parsing the PBF
#   ./setup_us.sh stop                remove the FalkorDB container
set -euo pipefail
cd "$(dirname "$0")"

GRAPH="us_roads"
APP_PORT="${APP_PORT:-8082}"
DB_PORT="${FALKOR_PORT:-6500}"            # host port for this demo's dedicated FalkorDB container
STATES="${STATES:-california}"            # space-separated Geofabrik US state slugs
GEO_BASE="https://download.geofabrik.de/north-america/us"
OUT="data/us"
VPY="$PWD/venv/bin/python"
step(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info(){ printf '    %s\n' "$*"; }

# --- FalkorDB in Docker (self-contained; the CCH index ships in the image) ----------
IMAGE="${FALKORDB_IMAGE:-falkordb/falkordb:v4.22.0}"
CONTAINER="${CONTAINER:-falkordb-cch-demo}"
CPUS="${FALKORDB_CPUS:-8}"                 # generous: the CCH build is multi-threaded
MEMORY="${FALKORDB_MEMORY:-6g}"            # generous: California's CCH build peaks ~3 GB
db_running(){ [ "$(redis-cli -p "$1" ping 2>/dev/null)" = "PONG" ] && \
              redis-cli -p "$1" module list 2>/dev/null | grep -qi '\bgraph\b'; }
db_start(){                                # $1=host port; rest -> module FALKORDB_ARGS
  local port="$1"; shift || true; local margs="$*"
  if db_running "$port"; then info "reusing FalkorDB on :$port"; return 0; fi
  command -v docker >/dev/null 2>&1 || { echo "FATAL: docker not found — install Docker Desktop"; exit 1; }
  docker info      >/dev/null 2>&1 || { echo "FATAL: docker daemon not running — start Docker Desktop"; exit 1; }
  docker image inspect "$IMAGE" >/dev/null 2>&1 || { step "Pulling $IMAGE"; docker pull "$IMAGE"; }
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  step "Starting $IMAGE as '$CONTAINER' on :$port (cpus=$CPUS mem=$MEMORY)"
  docker run -d --rm --name "$CONTAINER" -p "127.0.0.1:$port:6379" \
    --cpus "$CPUS" --memory "$MEMORY" --memory-swap "$MEMORY" \
    ${margs:+-e FALKORDB_ARGS="$margs"} "$IMAGE" >/dev/null
  for i in $(seq 1 120); do db_running "$port" && { info "ready after ~$((i/2))s"; return 0; }; sleep 0.5; done
  echo "FATAL: container '$CONTAINER' not ready"; docker logs "$CONTAINER" 2>&1 | tail -20; exit 1
}

if [ "${1:-}" = "stop" ]; then docker rm -f "$CONTAINER" >/dev/null 2>&1 && echo "removed $CONTAINER" || echo "no container"; exit 0; fi
REPARSE=0; [ "${1:-}" = "--reparse" ] && REPARSE=1

# bootstrap the Python venv + deps if missing, so setup_us.sh is self-contained
if [ ! -x "$VPY" ]; then
  step "Creating Python venv + installing deps (pyosmium, redis, falkordb, bulk-loader, flask, numpy)"
  PY="$(command -v python3.12 || command -v python3 || true)"
  [ -n "$PY" ] || { echo "FATAL: python3 not found"; exit 1; }
  "$PY" -m venv venv
  "$VPY" -m pip install -q --upgrade pip
  "$VPY" -m pip install -q osmium redis falkordb falkordb-bulk-loader flask numpy
fi

# a dedicated container on DB_PORT (default 6500) keeps the big California graph off
# the default 6379, so it won't clash with another FalkorDB on the default port.
PORT="$DB_PORT"

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

# THREAD_COUNT 8 speeds the CCH build; TIMEOUT_MAX raises the ceiling so explicit long
# timeouts (run_cch.py's CCH_TIMEOUT) are honoured, without a low default that would
# kill the CCH build and big-graph reads.
db_start "$PORT" THREAD_COUNT 8 TIMEOUT_MAX 1200000
echo "$PORT" > "$OUT/.dbport"
redis-cli -p "$PORT" GRAPH.CONFIG SET RESULTSET_SIZE -1 >/dev/null

# idempotency: skip the (slow) bulk-load + CCH build if this instance already holds the
# graph with a CCH index — a re-run then just re-serves. FORCE=1 rebuilds from scratch.
already_built() {
  [ "${FORCE:-0}" = "1" ] && return 1
  "$VPY" - "$PORT" "$GRAPH" <<'PY'
import sys
from falkordb import FalkorDB
port, graph = int(sys.argv[1]), sys.argv[2]
try:
    g = FalkorDB(host="localhost", port=port).select_graph(graph)
    n = g.query("MATCH (n:Intersection) RETURN count(n)").result_set[0][0]
    idx = g.query("CALL db.indexes() YIELD types RETURN types").result_set
    has_cch = any("CCH" in str(row) for row in idx)
    sys.exit(0 if (n and n > 0 and has_cch) else 1)
except Exception:
    sys.exit(1)
PY
}

if already_built; then
  info "graph '$GRAPH' + CCH already loaded on :$PORT — skipping load/build (FORCE=1 to rebuild)"
else
  step "Adding per-edge drive time (roads.csv gains a 'time' column)"
  "$VPY" add_traveltime.py "$OUT/roads.csv" "$OUT/names.json"

  step "Bulk-loading graph '$GRAPH'"
  redis-cli -p "$PORT" GRAPH.DELETE "$GRAPH" >/dev/null 2>&1 || true   # clean slate for the loader
  "$PWD/venv/bin/falkordb-bulk-insert" "$GRAPH" -u "redis://localhost:$PORT" \
    -N Intersection "$OUT/nodes.csv" -R ROAD "$OUT/roads.csv" -j INTEGER -i Intersection:osmid
  # CCH is its own graph-level path index; the bulk loader already created the osmid
  # index, so no extra graph index is needed for routing.

  step "Building CCH index (CREATE CCH INDEX)"
  # run_cch.py builds the index (generous CCH_TIMEOUT for big graphs, well under
  # TIMEOUT_MAX) via DDL: CREATE CCH INDEX FOR ()-[e:ROAD]->() ON (e.time).
  # If this step gets OOM-killed, the region is too big for available RAM: drop states
  # from STATES (California alone needs ~3 GB build-peak).
  GRAPH="$GRAPH" FALKOR_PORT="$PORT" CCH_WEIGHT_PROP=time "$VPY" run_cch.py
fi

printf '\n\033[1;32mUS (%s) ready on DB port %s.\033[0m\n' "$STATES" "$PORT"
printf '\033[1;32mServe with:  FALKOR_PORT=%s PROFILE=us PORT=%s %s app.py\033[0m\n' "$PORT" "$APP_PORT" "$VPY"
printf '    (DB port also saved to %s)\n' "$OUT/.dbport"
