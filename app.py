#!/usr/bin/env python3
"""
California road-network explorer backend (FalkorDB + CCH).

Serves:
  GET /                      -> the map UI (static/index.html)
  GET /roads.bin             -> all ROAD polylines, packed binary (for deck.gl)
  GET /meta                  -> graph stats + map center
  GET /nearest?lat=&lng=     -> nearest Intersection {osmid, lat, lng}
  GET /route?src=&dst=       -> best route via db.idx.cch.query (+ CCH compute time) plus
                                up to K-1 diverse alternatives via A* (K=3), each timed
  GET /labels?bbox=&zoom=    -> zoom-aware orientation labels (place names dominate, a few
                                numbered routes, street names only when zoomed right in)
  GET /nearest_edge?lat=&lng= -> nearest ROAD segment {u,v,name,path} (for cmd-click select)
  POST /congestion           -> (time profiles) slow arcs (time*=factor) by edge list or bbox;
                                the CCH index recustomizes and reroutes. Body {edges|bbox,factor,src,dst}
  POST /clear_congestion     -> restore given edges (or all) to base time (index recustomizes)

CCH is a graph-level path *index*: its shortcut arcs and node ranks live inside the
index, never as SHORTCUT edges or rank properties on the graph. It's created/dropped
with DDL —  CREATE/DROP CCH INDEX FOR ()-[e:ROAD]->() ON (e.<metric>)  — and
maintained incrementally as edge weights change; routing is answered by the
db.idx.cch.query procedure, which returns the fully-unpacked road path. The metric is
distance (weight) or drive time — see cch_weight_prop in profiles.py; changing edge
`time` (congestion) is picked up by the index's incremental maintenance, so the next
query reroutes with no manual rebuild.
"""
import os, sys, csv, json, struct, math, heapq, time, threading
import urllib.request, urllib.parse
from flask import Flask, request, jsonify, Response, send_from_directory
import numpy as np
from falkordb import FalkorDB
from profiles import get_profile

HERE  = os.path.dirname(os.path.abspath(__file__))
INF = float("inf")
sys.setrecursionlimit(1_000_000)

# ---------------------------------------------------------------------------
# map profile: everything network-specific (graph/port, schema, files, UI) is
# resolved from profiles.py. Select with PROFILE (default "us"); FALKOR_PORT
# / GRAPH still override the profile's DB target if set.
# ---------------------------------------------------------------------------
PROFILE_NAME = os.environ.get("PROFILE", "us")
PROF = get_profile(PROFILE_NAME)
GRAPH = os.environ.get("GRAPH", PROF["graph"])
REDIS_PORT = int(os.environ.get("FALKOR_PORT", str(PROF["port"])))
SCHEMA = PROF["schema"]
UNIT   = PROF["unit"]
FEAT   = PROF["features"]
FILES  = PROF["files"]
UI     = PROF["ui"]
NODE_LABEL = SCHEMA["node_label"]; ID_PROP = SCHEMA["id_prop"]; ID_TYPE = SCHEMA["id_type"]
ROAD_TYPE  = SCHEMA["road_type"];  WEIGHT_PROP = SCHEMA["weight_prop"]
# metric the CCH minimizes: distance (WEIGHT_PROP) by default, or drive time when the
# profile sets cch_weight_prop='time' (see add_traveltime.py). ROUTE_BY_TIME gates the
# real-ETA + congestion behaviour.
CCH_WEIGHT   = SCHEMA.get("cch_weight_prop", WEIGHT_PROP)
ROUTE_BY_TIME = (CCH_WEIGHT == "time")

# ambient traffic: seed a realistic congestion baseline at boot so a fresh map already
# shows traffic (routes coloured green/amber/red, rerouting meaningful) instead of being
# all-green. Deterministic (AMBIENT_SEED) so the pattern is stable across restarts.
# Default mix: 70% free / 20% slow / 10% heavy; factors land in the amber/red colour bands.
AMBIENT_TRAFFIC     = (os.environ.get("AMBIENT_TRAFFIC", "1") == "1")
AMBIENT_SEED        = int(os.environ.get("AMBIENT_SEED", "1234"))
AMBIENT_SLOW_FRAC   = float(os.environ.get("AMBIENT_SLOW_FRAC", "0.20"))
AMBIENT_HEAVY_FRAC  = float(os.environ.get("AMBIENT_HEAVY_FRAC", "0.10"))
AMBIENT_SLOW_FACTOR = float(os.environ.get("AMBIENT_SLOW_FACTOR", "1.8"))   # -> amber
AMBIENT_HEAVY_FACTOR= float(os.environ.get("AMBIENT_HEAVY_FACTOR", "3.5"))  # -> red

def parse_id(x):
    """coerce a request/CSV id to the profile's node-id type."""
    return int(x) if ID_TYPE == "int" else str(x)

def fpath(key):
    """absolute path to a profile artifact file, or None if the profile omits it."""
    rel = FILES.get(key)
    return os.path.join(HERE, rel) if rel else None

print(f"[boot] profile={PROFILE_NAME!r} graph={GRAPH!r} port={REDIS_PORT}", flush=True)
_db = FalkorDB(host="localhost", port=REDIS_PORT)
# lift the default 10000-row result cap so the full 285k id-map loads
try:
    _db.config_set("RESULTSET_SIZE", -1)
except Exception as e:
    print("[boot] warn: could not set RESULTSET_SIZE:", e, flush=True)
app = _db.select_graph(GRAPH)

# Some FalkorDB deployments set an aggressive per-query TIMEOUT_DEFAULT, which makes
# big-graph reads like the /meta counts fail with a 500. Give every read a generous
# explicit budget so it never spuriously times out.
DB_TIMEOUT = int(os.environ.get("DB_TIMEOUT_MS", "120000"))
def cy(q, **p):
    return app.query(q, p, timeout=DB_TIMEOUT) if p else app.query(q, timeout=DB_TIMEOUT)

# ---------------------------------------------------------------------------
# load data once at startup
# ---------------------------------------------------------------------------
print("[boot] loading node coords ...", flush=True)
id_arr, lat_arr, lon_arr = [], [], []
coord = {}                                   # id -> (lat, lon)
with open(fpath("nodes")) as f:
    r = csv.reader(f); next(r)
    for oid, la, lo in r:
        oid = parse_id(oid); la = float(la); lo = float(lo)
        coord[oid] = (la, lo)
        id_arr.append(oid); lat_arr.append(la); lon_arr.append(lo)
lat_np   = np.array(lat_arr,   dtype=np.float64)
lon_np   = np.array(lon_arr,   dtype=np.float64)
print(f"[boot] {len(coord)} nodes", flush=True)

# per-edge geometry (curved polylines). straight_geometry profiles omit it and
# fall back to straight node-to-node lines everywhere road_line() is used.
geom_file = fpath("geom")
if geom_file and os.path.exists(geom_file):
    print("[boot] loading edge geometry ...", flush=True)
    with open(geom_file) as f:
        GEOM = json.load(f)                   # "min-max" -> [[lat,lon],...]
else:
    GEOM = {}
print(f"[boot] {len(GEOM)} road polylines", flush=True)

print("[boot] loading edge names ...", flush=True)
NAMES = {}
names_file = fpath("names")
if names_file and os.path.exists(names_file):
    with open(names_file) as f:
        NAMES = json.load(f)                  # "min-max" -> street name / road ref
print(f"[boot] {len(NAMES)} named edges", flush=True)

print("[boot] loading place names ...", flush=True)
PLACES = []
places_file = fpath("places")
if places_file and os.path.exists(places_file):
    with open(places_file) as f:
        PLACES = json.load(f)                 # [{name,lat,lon,place,pop}], OSM place nodes
print(f"[boot] {len(PLACES)} places", flush=True)

# directed adjacency for the alternative-route search, loaded from the DIRECTED
# roads.csv so alternatives respect one-way streets too. ADJ holds the CCH *metric*
# (drive time when routing by time, else distance); DIST holds metres for the km
# readout (empty when the metric already is distance -> _dist falls back to ADJ).
# Skipped when the profile disables alternatives (avoids holding a huge graph).
ADJ  = {}                          # id -> {id: base metric (time or weight)}
DIST = {}                          # id -> {id: distance metres} (only when metric=time)
_cong = {}                         # (u, v) -> congestion factor currently in effect
_ambient = {}                      # (u, v) -> ambient baseline factor (persistent traffic)
_user_arcs = set()                 # arcs the user changed (so "clear" restores to ambient)
roads_file = fpath("roads")
if FEAT.get("alternatives") and roads_file and os.path.exists(roads_file):
    print("[boot] loading directed adjacency ...", flush=True)
    with open(roads_file) as f:
        r = csv.reader(f); header = next(r)
        wi = header.index(WEIGHT_PROP)
        mi = header.index(CCH_WEIGHT) if CCH_WEIGHT in header else wi
        for row in r:
            s_ = parse_id(row[0]); d_ = parse_id(row[1])
            ADJ.setdefault(s_, {})[d_] = float(row[mi])          # routing metric
            if mi != wi:
                DIST.setdefault(s_, {})[d_] = float(row[wi])     # distance metres
    print(f"[boot] adjacency over {len(ADJ)} source nodes "
          f"(metric={CCH_WEIGHT!r}{', +distance' if DIST else ''})", flush=True)

def edge_metric(u, v):
    """routing-metric weight of arc u->v with any congestion applied (or None)."""
    base = ADJ.get(u, {}).get(v)
    return None if base is None else base * _cong.get((u, v), 1.0)

def edge_dist(u, v):
    """distance (metres) of arc u->v; falls back to the metric when it already is
    distance (DIST empty)."""
    d = DIST.get(u, {}).get(v) if DIST else None
    return d if d is not None else ADJ.get(u, {}).get(v)

def ekey(a, b):
    return f"{a}-{b}" if a < b else f"{b}-{a}"

def edge_name(a, b):
    return NAMES.get(ekey(a, b))

def flip(line):
    """[[lat,lon],...] -> [[lng,lat],...] for deck.gl"""
    return [[lo, la] for la, lo in line]

def road_line(a, b):
    """oriented polyline [[lat,lon],...] for the base ROAD edge a->b (or None)."""
    pts = GEOM.get(ekey(a, b))
    if pts is None:
        return None
    return pts if a < b else pts[::-1]

# The CCH path index exposes no arc/entity count (CREATE CCH INDEX is DDL, and
# db.indexes() doesn't report a size), so /meta advertises the index's presence
# rather than a number — the stats panel shows a ✓ (see _cch_index_present).
CCH_ARCS = None

# ensure the id index for fast /route point lookups (idempotent; also created by
# the bulk loader). CCH itself needs no graph index — it's its own path index.
def ensure_index(prop):
    try:
        cy(f"CREATE INDEX FOR (n:{NODE_LABEL}) ON (n.{prop})")
        print(f"[boot] created index {NODE_LABEL}.{prop}", flush=True)
    except Exception as e:
        if "already" in str(e).lower() or "exist" in str(e).lower():
            print(f"[boot] index {NODE_LABEL}.{prop} already present", flush=True)
        else:
            print(f"[boot] index {NODE_LABEL}.{prop}: {e}", flush=True)
ensure_index(ID_PROP)

# road class -> line-width tier from free-flow speed (distance/time): highways drawn
# thicker for orientation. Works for every profile (all carry weight+time; time was
# derived from the road class, so speed encodes it). 2=highway, 1=arterial, 0=local.
def _road_tier(u, v):
    tval = ADJ.get(u, {}).get(v) or ADJ.get(v, {}).get(u)
    dval = (DIST.get(u, {}).get(v) or DIST.get(v, {}).get(u)) if DIST else tval
    if not (ROUTE_BY_TIME and tval and dval):
        return 1
    kmh = (dval / tval) * 3.6
    return 2 if kmh >= 75 else 1 if kmh >= 45 else 0

# pack all road polylines into a deck.gl-friendly binary blob (cached):
# [u32 nPaths][u32 nPts][u32 startIndices x (nPaths+1)][f32 lon,lat x nPts][u8 tier x nPaths]
# In the same pass, build a picking index for /nearest_edge: every DRAWN geometry
# vertex tagged with its edge, so a map click snaps to the road actually under the
# cursor — long contracted arcs included (nearest-node picking missed those).
print("[boot] packing roads.bin + geometry picking index ...", flush=True)
starts = [0]; flat = []
g_lat = []; g_lon = []; g_eidx = []          # per-vertex: lat, lon, edge index
e_u = []; e_v = []; e_tier = []              # per-edge: endpoint node ids, width tier
for e, (key, pts) in enumerate(GEOM.items()):
    us, vs = key.split("-", 1); u = parse_id(us); v = parse_id(vs)
    e_u.append(u); e_v.append(v); e_tier.append(_road_tier(u, v))
    for la, lo in pts:
        flat.append(lo); flat.append(la)       # deck.gl wants [lng, lat]
        g_lat.append(la); g_lon.append(lo); g_eidx.append(e)
    starts.append(len(flat) // 2)
positions = np.array(flat, dtype=np.float32)
start_np  = np.array(starts, dtype=np.uint32)
tier_np   = np.array(e_tier, dtype=np.uint8)
header = struct.pack("<II", len(GEOM), len(positions) // 2)
ROADS_BIN = header + start_np.tobytes() + positions.tobytes() + tier_np.tobytes()
print(f"[boot] roads.bin = {len(ROADS_BIN)/1e6:.1f} MB "
      f"({len(GEOM)} paths, {len(positions)//2} pts; "
      f"tiers hw/art/loc={int((tier_np==2).sum())}/{int((tier_np==1).sum())}/{int((tier_np==0).sum())})",
      flush=True)

# geometry picking arrays (float32 coords keep it light; ids kept exact)
_geo_lat  = np.array(g_lat, dtype=np.float32)
_geo_lon  = np.array(g_lon, dtype=np.float32)
_geo_eidx = np.array(g_eidx, dtype=np.int64)
_edge_u   = np.array(e_u, dtype=np.int64) if ID_TYPE == "int" else np.array(e_u, dtype=object)
_edge_v   = np.array(e_v, dtype=np.int64) if ID_TYPE == "int" else np.array(e_v, dtype=object)
del g_lat, g_lon, g_eidx, e_u, e_v, flat
print(f"[boot] picking index: {_geo_lat.size} vertices over {len(GEOM)} edges", flush=True)

CENTER = (float(lat_np.mean()), float(lon_np.mean()))

# ---------------------------------------------------------------------------
# driving directions (turn-by-turn) + alternative routes (K shortest, diverse)
# ---------------------------------------------------------------------------
AVG_MPS = 65_000 / 60.0          # ~65 km/h -> metres per minute, for a synthetic ETA
                                 # (distance profiles only; time profiles use real time)
# A* over the time metric needs its heuristic in the TIME domain: straight-line
# distance / MAX_MPS is a lower bound on drive time, admissible as long as MAX_MPS is
# >= the fastest road speed (keep MAX_KMH in sync with add_traveltime.py's top speed).
MAX_KMH = 110.0
MAX_MPS = MAX_KMH / 3.6

def _pt_hav(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))

def _bearing(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0

_COMPASS = ["north","northeast","east","southeast","south","southwest","west","northwest"]
def _compass(b): return _COMPASS[int((b + 22.5) // 45) % 8]

def _entry_bearing(pts):
    for i in range(1, len(pts)):
        if pts[i] != pts[0]:
            return _bearing(pts[0][0], pts[0][1], pts[i][0], pts[i][1])
    return 0.0
def _exit_bearing(pts):
    for i in range(len(pts)-2, -1, -1):
        if pts[i] != pts[-1]:
            return _bearing(pts[i][0], pts[i][1], pts[-1][0], pts[-1][1])
    return 0.0

def _turn(prev_b, next_b):
    d = (next_b - prev_b + 540.0) % 360.0 - 180.0     # (-180,180],  + = right
    ad = abs(d)
    if ad < 20:   return "straight", "Continue straight"
    side = "right" if d > 0 else "left"
    if ad < 45:   return f"slight-{side}", f"Slight {side}"
    if ad < 135:  return f"turn-{side}",  f"Turn {side}"
    if ad <= 160: return f"sharp-{side}", f"Sharp {side}"
    return "uturn", "Make a U-turn"

def build_steps(seq):
    """Turn-by-turn directions for a node-osmid sequence: merge consecutive
    same-street edges into legs, emit a maneuver at each street change."""
    if len(seq) < 2:
        return []
    legs = []                                          # {name, pts:[[lat,lon]], dist}
    for a, b in zip(seq, seq[1:]):
        rl = road_line(a, b) or [list(coord[a]), list(coord[b])]
        nm = edge_name(a, b)
        d  = _hav_len(rl)
        if legs and legs[-1]["name"] == nm:
            legs[-1]["pts"].extend(rl[1:])             # edges share the junction node
            legs[-1]["dist"] += d
        else:
            legs.append({"name": nm, "pts": list(rl), "dist": d})
    steps = []
    for i, leg in enumerate(legs):
        road = leg["name"] or "the road"
        if i == 0:
            man = "depart"
            instr = f"Head {_compass(_entry_bearing(leg['pts']))}"
            if leg["name"]:
                instr += f" on {road}"
        else:
            man, phrase = _turn(_exit_bearing(legs[i-1]["pts"]), _entry_bearing(leg["pts"]))
            instr = f"{phrase} onto {road}" if leg["name"] else \
                    (phrase if man != "straight" else "Continue")
        steps.append({"maneuver": man, "instruction": instr,
                      "name": leg["name"], "distance": round(leg["dist"], 1)})
    steps.append({"maneuver": "arrive", "instruction": "Arrive at your destination",
                  "name": None, "distance": 0.0})
    return steps

def astar(s, t, penalty):
    """Directed A* over ADJ with a per-arc penalty map, minimizing the CCH metric
    (drive time or distance). The heuristic is straight-line distance, converted to
    a time lower-bound (dist / MAX_MPS) when routing by time so it stays admissible."""
    if s == t:
        return [s]
    if s not in ADJ or t not in coord:
        return None
    tla, tlo = coord[t]
    def h(n):
        la, lo = coord[n]; d = _pt_hav(la, lo, tla, tlo)
        return d / MAX_MPS if ROUTE_BY_TIME else d
    openh = [(h(s), 0.0, s)]
    g = {s: 0.0}; came = {}; done = set()
    while openh:
        _, gc, u = heapq.heappop(openh)
        if u == t:
            path = [t]
            while path[-1] != s:
                path.append(came[path[-1]])
            path.reverse(); return path
        if u in done:
            continue
        done.add(u)
        for v in ADJ.get(u, {}):
            w = edge_metric(u, v)                       # base metric * congestion
            nd = gc + w * (1.0 + penalty.get((u, v), 0.0))
            if nd < g.get(v, INF):
                g[v] = nd; came[v] = u
                heapq.heappush(openh, (nd + h(v), nd, v))
    return None

def route_len(seq):
    """total CCH-metric cost of a node sequence (time or distance), congestion-aware."""
    tot = 0.0
    for a, b in zip(seq, seq[1:]):
        w = edge_metric(a, b)
        tot += w if w is not None else _pt_hav(coord[a][0], coord[a][1], coord[b][0], coord[b][1])
    return tot

def route_dist(seq):
    """total distance (metres) of a node sequence, for the km readout."""
    tot = 0.0
    for a, b in zip(seq, seq[1:]):
        d = edge_dist(a, b)
        tot += d if d is not None else _pt_hav(coord[a][0], coord[a][1], coord[b][0], coord[b][1])
    return tot

def _overlap(a, b):
    ea, eb = set(zip(a, a[1:])), set(zip(b, b[1:]))
    if not ea or not eb:
        return 0.0
    return len(ea & eb) / min(len(ea), len(eb))

# CCH query against the graph-level CCH path index (db.idx.cch.query). Yields, in
# one round-trip, the total weight and the FULLY-UNPACKED road path — the shortcut
# hierarchy is expanded inside the index, so we get plain node ids + per-edge road
# weights with nothing shortcut-specific leaking into the graph. Params are just the
# index's (relTypes, weightProp) plus the endpoints.
_CCH_Q = (
    f"MATCH (a:{NODE_LABEL} {{{ID_PROP}:$s}}), (b:{NODE_LABEL} {{{ID_PROP}:$t}}) "
    f"CALL db.idx.cch.query({{sourceNode:a, targetNode:b, "
    f"relTypes:['{ROAD_TYPE}'], weightProp:'{CCH_WEIGHT}'}}) "
    f"YIELD pathWeight, path "
    f"RETURN pathWeight, [n IN nodes(path) | n.{ID_PROP}], "
    f"[r IN relationships(path) | r.{WEIGHT_PROP}]"
)

def cch_route(s, t):
    """Primary best path via the FalkorDB CCH index (minimizing CCH_WEIGHT).
    Returns (metric_total, [id,...], [edge_distance_m,...], db_ms) where metric_total
    is drive time (s) or distance (m) depending on the metric, and db_ms is the DB's
    own execution time. (None, None, None, db_ms) if no path."""
    qr = app.query(_CCH_Q, {"s": s, "t": t}, timeout=DB_TIMEOUT)
    db_ms = float(getattr(qr, "run_time_ms", 0.0) or 0.0)
    res = qr.result_set
    if not res or res[0][0] is None:
        return None, None, None, db_ms
    ids    = [parse_id(x) for x in res[0][1]]
    dist_w = [float(x) if x is not None else None for x in res[0][2]]
    return float(res[0][0]), ids, dist_w, db_ms

def _mk_route(seq, algo, ms, primary, dist_m=None, time_s=None):
    """Build a route dict carrying both distance (m) and drive time (s, or None when
    the profile routes by distance) so the card shows km + a real/synthetic ETA."""
    if dist_m is None:
        dist_m = route_dist(seq)
    if time_s is None and ROUTE_BY_TIME:
        time_s = route_len(seq)                        # metric == time here
    return {"seq": seq, "algo": algo, "ms": ms, "primary": primary,
            "dist_m": dist_m, "time_s": time_s}


def primary_route(s, t):
    """The best route ONLY: FalkorDB CCH (or an A* fallback if CCH finds nothing).
    Fast — this is what the UI shows first, so the CCH speed is visible before the
    slower A* alternatives arrive. Returns (route_dict, seq, opt), where opt is the
    metric cost (time or distance) alt_routes searches around; (None, None, None) if
    s and t are disconnected."""
    metric, seq, dist_w, cch_ms = cch_route(s, t)
    if seq is None:                                    # CCH found nothing; fall back to A*
        t0 = time.perf_counter()
        seq = astar(s, t, {})
        fb_ms = (time.perf_counter() - t0) * 1000.0
        if seq is None:
            return None, None, None
        opt = route_len(seq)
        return _mk_route(seq, "A*", fb_ms, True), seq, opt
    dist_m = sum(d for d in dist_w if d is not None) if dist_w else route_dist(seq)
    r = _mk_route(seq, "CCH", cch_ms, True, dist_m=dist_m,
                  time_s=(metric if ROUTE_BY_TIME else None))
    return r, seq, metric


def alt_routes(s, t, primary_seq, opt, K=3):
    """Up to K-1 diverse alternatives via penalty-A* around an already-known best
    route (primary_seq, metric cost opt). The CCH result is never recomputed by A*;
    A* only fills the alternatives. Returns (alt_route_dicts, alt_ms)."""
    seen = [primary_seq]                               # dedup / overlap vs primary + accepted alts
    penalty = {}
    def penalize(sq, amt=1.0):
        for a, b in zip(sq, sq[1:]):
            penalty[(a, b)] = penalty.get((a, b), 0.0) + amt
    penalize(primary_seq)
    alts, attempts, alt_ms = [], 0, 0.0                # alt_ms = total A* time on alternatives
    while len(alts) < K - 1 and attempts < 8:
        attempts += 1
        t0 = time.perf_counter()
        alt = astar(s, t, penalty)                     # alternatives are A*, not CCH
        dt = (time.perf_counter() - t0) * 1000.0
        alt_ms += dt
        if alt is None:
            break
        penalize(alt)                                  # keep pushing subsequent search away
        if any(alt == sq for sq in seen):
            continue
        if route_len(alt) > opt * 1.6:                 # too much of a detour to be useful
            continue
        if max(_overlap(alt, sq) for sq in seen) > 0.8:
            continue                                   # too similar to one we already have
        seen.append(alt)
        alts.append(_mk_route(alt, "A*", dt, False))
    return alts, alt_ms

# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------
srv = Flask(__name__, static_folder=None)

@srv.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")

@srv.get("/roads.bin")
def roads_bin():
    return Response(ROADS_BIN, mimetype="application/octet-stream")

def _cch_index_present():
    """True if a CCH path index over (ROAD_TYPE, CCH_WEIGHT) is registered."""
    try:
        for row in cy("CALL db.indexes() YIELD types RETURN types").result_set:
            types = row[0] or {}
            if "CCH" in (types.get(CCH_WEIGHT) or []):
                return True
    except Exception as e:
        print("[meta] warn: db.indexes() failed:", e, flush=True)
    return False

_meta_counts = None
@srv.get("/meta")
def meta():
    # node/road totals + CCH-index facts never change at runtime — resolve once,
    # then cache. The CCH is a graph-level path index with no exposed size, so
    # `arcs` is always None (CCH_ARCS); the frontend shows presence (✓) instead.
    # `cch_index` reports whether the index is currently registered.
    global _meta_counts
    if _meta_counts is None:
        _meta_counts = (
            cy(f"MATCH (n:{NODE_LABEL}) RETURN count(n)").result_set[0][0],
            cy(f"MATCH ()-[r:{ROAD_TYPE}]->() RETURN count(r)").result_set[0][0],
            _cch_index_present(),
        )
    n, e, cch = _meta_counts
    # ship the profile's UI framing so the frontend isn't hard-wired to one country
    return jsonify(nodes=n, roads=e, arcs=CCH_ARCS, cch_index=cch,
                   center=dict(lat=CENTER[0], lng=CENTER[1]),
                   profile=PROFILE_NAME, unit=UNIT, features=FEAT,
                   route_by_time=ROUTE_BY_TIME, congested=len(_cong),
                   title=UI["title"], subtitle=UI["subtitle"],
                   view=UI["view"], cities=UI["cities"])

@srv.get("/nearest")
def nearest():
    la = float(request.args["lat"]); lo = float(request.args["lng"])
    # planar approx is fine at this scale for nearest-neighbour ranking
    dlat = (lat_np - la)
    dlon = (lon_np - lo) * math.cos(math.radians(la))
    i = int(np.argmin(dlat * dlat + dlon * dlon))
    return jsonify(osmid=id_arr[i], lat=float(lat_np[i]), lng=float(lon_np[i]))

GEO_BBOX = tuple(UI["geocode_bbox"])   # minlng, minlat, maxlng, maxlat
GEO_BIAS = UI["geocode_bias"]
_geo_cache = {}

@srv.get("/geocode")
def geocode():
    """address autocomplete via Photon (komoot), biased/filtered to the extract."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify(suggestions=[])
    if q in _geo_cache:
        return jsonify(suggestions=_geo_cache[q])
    params = urllib.parse.urlencode({"q": q, "limit": 7,
                                     "lat": GEO_BIAS["lat"], "lon": GEO_BIAS["lon"]})
    url = "https://photon.komoot.io/api/?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "falkordb-cch-demo/1.0"})
    sugg = []
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        for feat in data.get("features", []):
            geom = feat.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            lon, lat = geom["coordinates"][:2]
            if not (GEO_BBOX[0] <= lon <= GEO_BBOX[2] and GEO_BBOX[1] <= lat <= GEO_BBOX[3]):
                continue
            p = feat.get("properties", {})
            head = p.get("name") or p.get("street") or ""
            if p.get("housenumber") and p.get("street"):
                head = f"{p['street']} {p['housenumber']}"
            loc = p.get("city") or p.get("county") or p.get("state") or p.get("country") or ""
            label = ", ".join([x for x in (head, loc) if x]) or (p.get("country") or q)
            sugg.append({"label": label, "lat": float(lat), "lng": float(lon)})
    except Exception as e:
        return jsonify(suggestions=[], error=str(e))
    # de-dup identical labels, keep order
    seen, uniq = set(), []
    for s in sugg:
        if s["label"] in seen:
            continue
        seen.add(s["label"]); uniq.append(s)
    _geo_cache[q] = uniq
    return jsonify(suggestions=uniq)

@srv.get("/reverse")
def reverse():
    """nearest human-readable label for a lat/lng (Photon reverse); coords fallback."""
    lat = float(request.args["lat"]); lng = float(request.args["lng"])
    key = f"r:{lat:.5f},{lng:.5f}"
    if key in _geo_cache:
        return jsonify(label=_geo_cache[key])
    fallback = f"{lat:.5f}, {lng:.5f}"
    params = urllib.parse.urlencode({"lat": lat, "lon": lng})
    url = "https://photon.komoot.io/reverse?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "falkordb-cch-demo/1.0"})
    label = fallback
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.load(r)
        feats = data.get("features", [])
        if feats:
            p = feats[0].get("properties", {})
            head = p.get("name") or p.get("street") or ""
            if p.get("housenumber") and p.get("street"):
                head = f"{p['street']} {p['housenumber']}"
            loc = p.get("city") or p.get("county") or p.get("state") or ""
            label = ", ".join([x for x in (head, loc) if x]) or fallback
    except Exception:
        pass
    _geo_cache[key] = label
    return jsonify(label=label)

@srv.get("/labels")
def labels():
    """De-cluttered orientation labels for the viewport, scaled to the zoom level.
    Real place names (city/town/village/suburb) are the priority; a few numbered
    routes and — only when zoomed right in — some street names round them out."""
    minlat = float(request.args["minlat"]); maxlat = float(request.args["maxlat"])
    minlng = float(request.args["minlng"]); maxlng = float(request.args["maxlng"])
    zoom   = float(request.args.get("zoom", 11))
    if zoom < 6.5:
        return jsonify(labels=[])
    # zoom policy: which places qualify (importance >= p_thresh), total label
    # budget, the *reserved* caps for numbered routes and street names, and the
    # minimum lat/lng spacing used to de-cluster so labels never pile up. Places
    # get the rest of the budget (and reclaim any road slots left unused), so
    # real place names always dominate the map.
    if   zoom < 8:   p_thresh, cap, route_cap, street_cap, sep = 120_000, 12, 0, 0,  0.135
    elif zoom < 9:   p_thresh, cap, route_cap, street_cap, sep = 60_000,  16, 0, 0,  0.075
    elif zoom < 10:  p_thresh, cap, route_cap, street_cap, sep = 30_000,  22, 3, 0,  0.035
    elif zoom < 11:  p_thresh, cap, route_cap, street_cap, sep = 12_000,  30, 4, 0,  0.020
    elif zoom < 12:  p_thresh, cap, route_cap, street_cap, sep = 6_000,   40, 5, 0,  0.012
    elif zoom < 13:  p_thresh, cap, route_cap, street_cap, sep = 2_500,   52, 5, 8,  0.008
    elif zoom < 14:  p_thresh, cap, route_cap, street_cap, sep = 800,     64, 6, 12, 0.005
    else:            p_thresh, cap, route_cap, street_cap, sep = 0,       80, 6, 16, 0.003
    streets   = street_cap > 0
    place_cap = max(1, cap - route_cap - street_cap)

    cands = []          # (tier, sortkey, lat, lon, name, kind); tier 0=place,1=route,2=street

    # -- places (priority) --
    if _pl_lat is not None and _pl_lat.size:
        pm = ((_pl_lat >= minlat) & (_pl_lat <= maxlat) &
              (_pl_lon >= minlng) & (_pl_lon <= maxlng) & (_pl_score >= p_thresh))
        for i in np.nonzero(pm)[0]:
            i = int(i)
            cands.append((0, -_pl_score[i], float(_pl_lat[i]), float(_pl_lon[i]),
                          PL_NAME[i], PL_KIND[i]))

    # -- roads: a few numbered routes, plus streets only when zoomed right in --
    if _lbl_lat is not None and (route_cap or streets):
        rm = ((_lbl_lat >= minlat) & (_lbl_lat <= maxlat) &
              (_lbl_lon >= minlng) & (_lbl_lon <= maxlng))
        best = {}                               # name -> (visible_len, index)
        for i in np.nonzero(rm)[0]:
            i = int(i)
            is_route = LBL_ROUTE[i]
            if not is_route and not streets:
                continue
            if is_route and not route_cap:
                continue
            nm = LBL_NAME[i]; L = _lbl_len[i]
            cur = best.get(nm)
            if cur is None or L > cur[0]:
                best[nm] = (L, i)
        for nm, (L, i) in best.items():
            tier = 1 if LBL_ROUTE[i] else 2
            cands.append((tier, -L, float(_lbl_lat[i]), float(_lbl_lon[i]),
                          nm, "route" if tier == 1 else "street"))

    # priority: places, then routes, then streets; greedy spatial de-cluster.
    cands.sort(key=lambda c: (c[0], c[1]))
    tcap = {0: place_cap, 1: route_cap, 2: street_cap}
    out, placed, cnt, taken = [], [], {0: 0, 1: 0, 2: 0}, set()

    def collides(la, lo):
        return any(abs(la - pa) < sep and abs(lo - po) < sep for pa, po in placed)

    # round 1: honour each tier's reserve so roads keep a guaranteed slice
    for j, (tier, _sk, la, lo, nm, kind) in enumerate(cands):
        if cnt[tier] >= tcap[tier] or collides(la, lo):
            continue
        placed.append((la, lo)); out.append({"name": nm, "lat": la, "lng": lo, "kind": kind})
        cnt[tier] += 1; taken.add(j)
        if len(out) >= cap:
            break
    # round 2: hand any leftover budget back to places (the priority layer)
    if len(out) < cap:
        for j, (tier, _sk, la, lo, nm, kind) in enumerate(cands):
            if tier != 0 or j in taken or collides(la, lo):
                continue
            placed.append((la, lo)); out.append({"name": nm, "lat": la, "lng": lo, "kind": kind})
            if len(out) >= cap:
                break
    return jsonify(labels=out)

# congestion colouring: level from the current/free-flow time ratio (the _cong factor
# on each arc). free-flow ~ green, moderate ~ yellow, heavy ~ red (Google-style).
CONG_MID, CONG_HEAVY = 1.3, 2.5
def _cong_level(f):
    return "heavy" if f > CONG_HEAVY else "mid" if f > CONG_MID else "free"

def _route_colored(seq):
    """Split the route into consecutive same-congestion sub-paths so the UI can colour
    green/yellow/red. Returns None for distance profiles (no time metric to slow)."""
    if not ROUTE_BY_TIME or len(seq) < 2:
        return None
    out, cur = [], None
    for a, b in zip(seq, seq[1:]):
        lvl = _cong_level(_cong.get((a, b), 1.0))
        rl = road_line(a, b) or [list(coord[a]), list(coord[b])]   # [[lat,lon],...]
        if cur and cur["_lvl"] == lvl:
            cur["pts"].extend(rl[1:])                              # share the junction node
        else:
            cur = {"_lvl": lvl, "pts": list(rl)}; out.append(cur)
    return [{"level": s["_lvl"], "path": flip(s["pts"])} for s in out]

def _format_route(r):
    """Turn an internal route dict ({seq, dist_m, time_s, primary, algo, ms}) into the
    JSON the UI renders. Shared by /route and /alternatives so a card looks identical
    whichever endpoint produced it. `weight` is distance (m); `eta_min` is the real
    drive time when routing by time, else a synthetic distance/AVG_MPS estimate;
    `colored` is the per-segment congestion breakdown (time profiles only)."""
    seq  = r["seq"]
    dist = r["dist_m"] if r.get("dist_m") is not None else route_dist(seq)
    time_s = r.get("time_s")
    eta_min = (time_s / 60.0) if time_s is not None else (dist / AVG_MPS)
    return dict(
        primary=r["primary"],
        algo=r["algo"],
        compute_ms=round(r["ms"], 3) if r.get("ms") is not None else None,
        weight=float(dist),
        eta_min=round(eta_min, 1),
        time_s=round(time_s, 1) if time_s is not None else None,
        hops=len(seq) - 1,
        nodes=len(seq),
        path=flip(_seq_to_polyline(seq)),
        colored=_route_colored(seq),
        steps=build_steps(seq),
    )

@srv.get("/route")
def route():
    """The best route ONLY (CCH, or an A* fallback). Returns fast so the UI can draw
    it immediately; the slower A* alternatives are fetched separately from
    /alternatives. cch_ms is the DB's own CCH compute time (null on A* fallback)."""
    src = parse_id(request.args["src"]); dst = parse_id(request.args["dst"])
    pr, _seq, _pw = primary_route(src, dst)
    if pr is None:
        return jsonify(found=False)
    cch_ms = pr["ms"] if pr["algo"] == "CCH" and pr.get("ms") is not None else None
    return jsonify(found=True, route=_format_route(pr),
                   cch_ms=round(cch_ms, 3) if cch_ms is not None else None,
                   src=[coord[src][1], coord[src][0]] if src in coord else None,
                   dst=[coord[dst][1], coord[dst][0]] if dst in coord else None)

@srv.get("/alternatives")
def alternatives():
    """Up to K-1 diverse A* alternatives around the best route. Slower than /route by
    design (each alternative is a full-graph A* search), so the UI requests it after
    the CCH route is already on screen. Recomputes the cheap CCH route to get its path
    + cost to search around — stateless, no dependence on a prior /route call."""
    src = parse_id(request.args["src"]); dst = parse_id(request.args["dst"])
    K = max(1, min(3, int(request.args.get("k", 3))))
    pr, seq, pw = primary_route(src, dst)
    if pr is None:
        return jsonify(found=False, routes=[])
    opt = pw if pw else route_len(seq)
    alts, alt_ms = alt_routes(src, dst, seq, opt, K)
    # present alternatives fastest-first (by the CCH metric: drive time, else distance)
    alts.sort(key=lambda a: a["time_s"] if a.get("time_s") is not None else a["dist_m"])
    return jsonify(found=True, routes=[_format_route(a) for a in alts],
                   alt_ms=round(alt_ms, 3))

# ---------------------------------------------------------------------------
# congestion (time profiles only): raise/restore `time` on edges in a viewport
# box. Because it changes a weight (not topology), the CCH index *recustomizes*
# (Phase-2 only, auto at write-commit) instead of doing a full rebuild — and
# db.idx.cch.query then returns a new fastest route. This is the "C" in CCH.
# ---------------------------------------------------------------------------
_cong_lock = threading.Lock()
_SET_TIME_Q = (f"UNWIND $p AS p "
               f"MATCH (a:{NODE_LABEL} {{{ID_PROP}:p.a}})-[r:{ROAD_TYPE}]->(b:{NODE_LABEL} {{{ID_PROP}:p.b}}) "
               f"SET r.{CCH_WEIGHT} = p.t")
# CCH path index DDL (create/drop the index over the routing metric). The index
# maintains incrementally as edge weights change, so congestion SETs auto-reroute.
_CREATE_CCH_Q = f"CREATE CCH INDEX FOR ()-[e:{ROAD_TYPE}]->() ON (e.{CCH_WEIGHT})"
_DROP_CCH_Q   = f"DROP CCH INDEX FOR ()-[e:{ROAD_TYPE}]->() ON (e.{CCH_WEIGHT})"

def _pt_polyline_m(la, lo, pts):
    """approx planar distance (m) from point (la,lo) to a [[lat,lon],...] polyline."""
    coslat = math.cos(math.radians(la))
    px, py = lo * coslat * 111320.0, la * 111320.0
    best = INF
    for (ala, alo), (bla, blo) in zip(pts, pts[1:]):
        ax, ay = alo * coslat * 111320.0, ala * 111320.0
        bx, by = blo * coslat * 111320.0, bla * 111320.0
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        cx, cy = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(px - cx, py - cy))
    return best

def _nearest_edge(la, lo, k=8):
    """the ROAD edge (canonical u<v) whose DRAWN geometry is closest to (la,lo), or
    None. Snaps to the nearest geometry vertex across all polylines, so a click lands
    on the road actually under the cursor (contracted long arcs included)."""
    if _geo_lat is not None and _geo_lat.size:
        dlat = _geo_lat - la
        dlon = (_geo_lon - lo) * math.cos(math.radians(la))
        i = int(np.argmin(dlat * dlat + dlon * dlon))
        e = int(_geo_eidx[i])
        u, v = _edge_u[e], _edge_v[e]
        u, v = (u.item() if hasattr(u, "item") else u), (v.item() if hasattr(v, "item") else v)
        return (u, v) if u < v else (v, u)
    # fallback: arcs incident to the k nearest nodes, closest polyline wins
    dlat = lat_np - la
    dlon = (lon_np - lo) * math.cos(math.radians(la))
    d2 = dlat * dlat + dlon * dlon
    idx = np.argpartition(d2, min(k, len(d2) - 1))[:k]
    cand = set()
    for i in idx:
        u = id_arr[int(i)]
        for v in ADJ.get(u, {}):
            cand.add((u, v) if u < v else (v, u))
    best, best_d = None, INF
    for (u, v) in cand:
        pts = road_line(u, v) or [list(coord[u]), list(coord[v])]
        d = _pt_polyline_m(la, lo, pts)
        if d < best_d:
            best_d, best = d, (u, v)
    return best

@srv.get("/nearest_edge")
def nearest_edge():
    """nearest ROAD segment to a lat/lng."""
    la = float(request.args["lat"]); lo = float(request.args["lng"])
    e = _nearest_edge(la, lo)
    if e is None:
        return jsonify(found=False)
    u, v = e
    return jsonify(found=True, u=u, v=v, name=edge_name(u, v),
                   path=flip(road_line(u, v) or [list(coord[u]), list(coord[v])]))

def _road_run(u, v, cap_edges=140, cap_m=8000):
    """A connected run of edges sharing (u,v)'s street name, grown outward from the
    clicked segment (capped) — so selecting a road highlights a visible stretch, not a
    single tiny segment. Unnamed arcs return just the clicked segment."""
    nm = edge_name(u, v)
    chosen = {}                                # ekey -> (a,b) canonical
    def add(a, b):
        chosen[ekey(a, b)] = (a, b) if a < b else (b, a)
    add(u, v)
    if nm is None:
        return list(chosen.values())
    total = edge_dist(u, v) or 0.0
    frontier, seen = [u, v], {u, v}
    while frontier and len(chosen) < cap_edges and total < cap_m:
        x = frontier.pop()
        for y in ADJ.get(x, {}):
            if ekey(x, y) in chosen or edge_name(x, y) != nm:
                continue
            add(x, y); total += edge_dist(x, y) or 0.0
            if y not in seen:
                seen.add(y); frontier.append(y)
            if len(chosen) >= cap_edges or total >= cap_m:
                break
    return list(chosen.values())

def _nearest_route_edge(la, lo, s, t):
    """The edge of the CCH route s->t whose geometry is closest to (la,lo), and that
    distance in metres. Used to bias road-selection toward the shown route when the
    click is near it. (None, inf) if there's no route."""
    _pr, seq, _opt = primary_route(s, t)
    if not seq or len(seq) < 2:
        return None, INF
    best, bd = None, INF
    for a, b in zip(seq, seq[1:]):
        pts = road_line(a, b) or [list(coord[a]), list(coord[b])]
        d = _pt_polyline_m(la, lo, pts)
        if d < bd:
            bd, best = d, (a, b)
    if best is None:
        return None, INF
    u, v = best
    return ((u, v) if u < v else (v, u)), bd

@srv.get("/select_road")
def select_road():
    """The road (connected same-name stretch) nearest a lat/lng — for road selection.
    If src/dst + a `bias` (metres) are given and the click is within `bias` of the shown
    route, snap to the route's road (so slowing the route is easy); otherwise pick the
    nearest road (so off-route roads stay selectable). Returns the road's edges + per-edge
    polylines to highlight."""
    la = float(request.args["lat"]); lo = float(request.args["lng"])
    src = request.args.get("src"); dst = request.args.get("dst")
    bias = float(request.args.get("bias", 0) or 0)
    anchor, on_route = None, False
    if src and dst and bias > 0:
        re, rd = _nearest_route_edge(la, lo, parse_id(src), parse_id(dst))
        if re is not None and rd <= bias:
            anchor, on_route = re, True
    if anchor is None:
        anchor = _nearest_edge(la, lo)
    if anchor is None:
        return jsonify(found=False)
    u, v = anchor
    edges = _road_run(u, v)
    paths = [flip(road_line(a, b) or [list(coord[a]), list(coord[b])]) for (a, b) in edges]
    return jsonify(found=True, name=edge_name(u, v), anchor=[u, v], on_route=on_route,
                   edges=[[a, b] for (a, b) in edges], paths=paths)

def _dir_arcs(pairs):
    """expand undirected [u,v] selections to the existing directed arcs (both ways)."""
    out = []
    for pr in pairs:
        u, v = parse_id(pr[0]), parse_id(pr[1])
        for a, b in ((u, v), (v, u)):
            if ADJ.get(a, {}).get(b) is not None:
                out.append((a, b))
    return out

def _reroute(src, dst):
    """recompute the primary CCH route after a metric change; (route_json, cch_ms)."""
    src = parse_id(src); dst = parse_id(dst)
    pr, _seq, _opt = primary_route(src, dst)
    if pr is None:
        return None, None
    cch_ms = pr["ms"] if pr["algo"] == "CCH" and pr.get("ms") is not None else None
    return _format_route(pr), (round(cch_ms, 3) if cch_ms is not None else None)

def _set_times(rows):
    """SET r.<metric>=t for each {a,b,t}; returns DB run_time_ms (incl. recustomize)."""
    if not rows:
        return None
    qr = app.query(_SET_TIME_Q, {"p": rows}, timeout=DB_TIMEOUT)
    return float(getattr(qr, "run_time_ms", 0.0) or 0.0)

@srv.post("/congestion")
def congestion():
    """Slow the selected arcs (time = base*factor) so the CCH index recustomizes and
    reroutes. Body: {edges:[[u,v],...], factor?, src?, dst?} — or a viewport box
    {minlat,maxlat,minlng,maxlng} instead of edges."""
    if not FEAT.get("traveltime"):
        return jsonify(error="congestion needs a time-based profile"), 400
    d = request.get_json(force=True) or {}
    factor = float(d.get("factor", 5.0))
    capped = False
    if d.get("edges"):
        arcs = _dir_arcs(d["edges"])
    else:
        m = ((lat_np >= float(d["minlat"])) & (lat_np <= float(d["maxlat"])) &
             (lon_np >= float(d["minlng"])) & (lon_np <= float(d["maxlng"])))
        innodes = {id_arr[i] for i in np.nonzero(m)[0]}
        arcs = [(u, v) for u in innodes for v in ADJ.get(u, {}) if v in innodes]
    with _cong_lock:
        rows = []
        for (u, v) in arcs:
            base = ADJ.get(u, {}).get(v)
            if base is None:
                continue
            _cong[(u, v)] = factor; _user_arcs.add((u, v))   # user override on top of ambient
            rows.append({"a": u, "b": v, "t": base * factor})
        recustomize_ms = _set_times(rows)
    resp = dict(found=True, affected=len(rows), capped=capped,
                congested=len(_cong), factor=factor,
                recustomize_ms=round(recustomize_ms, 3) if recustomize_ms is not None else None)
    if d.get("src") is not None and d.get("dst") is not None:
        resp["route"], resp["cch_ms"] = _reroute(d["src"], d["dst"])
    return jsonify(resp)

@srv.post("/clear_congestion")
def clear_congestion():
    """Remove USER-added traffic, restoring arcs to their ambient baseline (the index
    recustomizes back). Body: {edges?, src?, dst?} — clears the given edges, or ALL
    user-added traffic when `edges` is omitted. Ambient traffic is never cleared here."""
    if not FEAT.get("traveltime"):
        return jsonify(error="congestion needs a time-based profile"), 400
    d = request.get_json(force=True) or {}
    with _cong_lock:
        targets = _dir_arcs(d["edges"]) if d.get("edges") else list(_user_arcs)
        rows = []
        for (u, v) in targets:
            base = ADJ.get(u, {}).get(v)
            if base is None:
                continue
            _user_arcs.discard((u, v))
            amb = _ambient.get((u, v), 1.0)            # restore to ambient (or free if none)
            if amb == 1.0:
                _cong.pop((u, v), None)
            else:
                _cong[(u, v)] = amb
            rows.append({"a": u, "b": v, "t": base * amb})
        recustomize_ms = _set_times(rows)
    resp = dict(found=True, cleared=len(rows), congested=len(_cong),
                recustomize_ms=round(recustomize_ms, 3) if recustomize_ms is not None else None)
    if d.get("src") is not None and d.get("dst") is not None:
        resp["route"], resp["cch_ms"] = _reroute(d["src"], d["dst"])
    return jsonify(resp)

def seed_ambient_traffic():
    """Seed a realistic congestion baseline into `_cong` (a fresh map then shows traffic
    and coloured routes). Deterministic per undirected edge; the same factor is applied
    both ways (a jam is a jam in both directions). For speed we drop the index, batch the
    time writes with no live maintenance, then rebuild once over the seeded metric."""
    if not (AMBIENT_TRAFFIC and ROUTE_BY_TIME and ADJ):
        return
    import random
    rng = random.Random(AMBIENT_SEED)
    heavy_cut = AMBIENT_HEAVY_FRAC
    slow_cut  = AMBIENT_HEAVY_FRAC + AMBIENT_SLOW_FRAC
    rows, n_slow, n_heavy = [], 0, 0
    for u in ADJ:
        for v in ADJ[u]:
            if u >= v:                      # decide once per undirected edge
                continue
            r = rng.random()
            f = (AMBIENT_HEAVY_FACTOR if r < heavy_cut else
                 AMBIENT_SLOW_FACTOR  if r < slow_cut  else 1.0)
            if f == 1.0:
                continue
            n_heavy += (f == AMBIENT_HEAVY_FACTOR); n_slow += (f == AMBIENT_SLOW_FACTOR)
            for a, b in ((u, v), (v, u)):
                base = ADJ.get(a, {}).get(b)
                if base is not None:
                    _cong[(a, b)] = f; _ambient[(a, b)] = f   # baseline, restored on clear
                    rows.append({"a": a, "b": b, "t": base * f})
    if not rows:
        return
    print(f"[boot] seeding ambient traffic: {n_slow} slow + {n_heavy} heavy roads "
          f"({len(rows)} arcs) ...", flush=True)
    try:                                    # drop the index so the batch writes below
        cy(_DROP_CCH_Q)                     # don't each pay incremental maintenance
    except Exception:
        pass
    for i in range(0, len(rows), 20000):
        app.query(_SET_TIME_Q, {"p": rows[i:i + 20000]}, timeout=DB_TIMEOUT)
    cy(_CREATE_CCH_Q)                        # rebuild once over the seeded metric
    print("[boot] ambient traffic seeded; CCH index rebuilt", flush=True)

def _seq_to_polyline(seq):
    """concatenate oriented ROAD geometry along a node-osmid sequence."""
    out = []
    for a, b in zip(seq, seq[1:]):
        rl = road_line(a, b)
        if rl is None:                          # non-adjacent (shouldn't happen) -> straight
            rl = [list(coord[a]), list(coord[b])]
        if out and out[-1] == rl[0]:
            out.extend(rl[1:])
        else:
            out.extend(rl)
    return out

def _hav_len(pts):
    R = 6371000.0; tot = 0.0
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        p1, p2 = math.radians(la1), math.radians(la2)
        dphi = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
        x = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        tot += 2 * R * math.asin(math.sqrt(x))
    return tot

# ---------------------------------------------------------------------------
# map-label index (built once): a representative on-road point + length for each
# named edge, so /labels can sprinkle a few street/route names per viewport.
# ---------------------------------------------------------------------------
LBL_NAME  = []                 # name text, aligned with the numpy arrays below
LBL_ROUTE = []                 # bool: numbered road ("Route N") vs a local street
_lbl_lat = _lbl_lon = _lbl_len = None

def build_label_index():
    global _lbl_lat, _lbl_lon, _lbl_len
    lats, lons, lens = [], [], []
    for key, nm in NAMES.items():
        pts = GEOM.get(key)
        if not pts:
            continue
        mla, mlo = pts[len(pts) // 2]           # midpoint vertex sits on the road
        LBL_NAME.append(nm)
        LBL_ROUTE.append(nm.startswith("Route "))
        lats.append(mla); lons.append(mlo)
        lens.append(_hav_len(pts))
    _lbl_lat = np.array(lats, dtype=np.float64)
    _lbl_lon = np.array(lons, dtype=np.float64)
    _lbl_len = np.array(lens, dtype=np.float64)
    print(f"[boot] label index: {len(LBL_NAME)} road/route labels "
          f"({sum(LBL_ROUTE)} numbered routes)", flush=True)

build_label_index()

# ---------------------------------------------------------------------------
# place-label index: real OSM place names (city/town/village/suburb/...), ranked
# by an importance score = population + a per-class floor, so a populous "town"
# (Netanya, Rishon LeZion) still outranks a tiny "city". These are the primary
# orientation labels; numbered routes/streets are secondary (see /labels).
# ---------------------------------------------------------------------------
PLACE_FLOOR = {"city": 100_000, "town": 50_000, "village": 5_000,
               "suburb": 3_000, "neighbourhood": 1_500, "hamlet": 500}
PL_NAME = []                   # aligned with the numpy arrays below
PL_KIND = []
_pl_lat = _pl_lon = _pl_score = None

def build_place_index():
    global _pl_lat, _pl_lon, _pl_score
    lats, lons, scores = [], [], []
    for p in PLACES:
        PL_NAME.append(p["name"])
        PL_KIND.append(p["place"])
        lats.append(p["lat"]); lons.append(p["lon"])
        scores.append(float(p.get("pop", 0) or 0) + PLACE_FLOOR.get(p["place"], 0))
    _pl_lat = np.array(lats, dtype=np.float64)
    _pl_lon = np.array(lons, dtype=np.float64)
    _pl_score = np.array(scores, dtype=np.float64)
    print(f"[boot] place index: {len(PL_NAME)} place labels", flush=True)

build_place_index()

# seed realistic ambient traffic once the graph + CCH index are in place
seed_ambient_traffic()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"[boot] serving on http://localhost:{port}", flush=True)
    srv.run(host="0.0.0.0", port=port, threaded=True)
