#!/usr/bin/env python3
"""Verify the loaded graph, build the CCH path index, and sanity-check a query.

CCH is a graph-level path *index*: its shortcut arcs and node ranks live inside the
index — no SHORTCUT edges / rank properties on the graph. It's created/dropped with
DDL (CREATE/DROP CCH INDEX) and maintained incrementally as edges change; routing is
answered by the db.idx.cch.query procedure.

  CREATE CCH INDEX FOR ()-[e:ROAD]->() ON (e.time)
  DROP   CCH INDEX FOR ()-[e:ROAD]->() ON (e.time)
  CALL db.idx.cch.query({sourceNode, targetNode, relTypes:['ROAD'], weightProp:'time'})

Target graph/port come from env (GRAPH, FALKOR_PORT), defaulting to the US (California)
demo, so the same script builds the CCH for any loaded profile."""
import os, time
from falkordb import FalkorDB

HERE  = os.path.dirname(os.path.abspath(__file__))
GRAPH = os.environ.get("GRAPH", "us_roads")
PORT  = int(os.environ.get("FALKOR_PORT", "6379"))
RELTYPES   = ["ROAD"]
# metric the CCH minimizes: distance ("weight") by default, or drive time ("time")
# when the profile routes by travel time (CCH_WEIGHT_PROP=time; see add_traveltime.py).
WEIGHTPROP = os.environ.get("CCH_WEIGHT_PROP", "weight")
REL_PATTERN = "|".join(RELTYPES)               # DDL edge pattern, e.g. ROAD  (or A|B)
# generous budget for the heavy build/query on big graphs (e.g. US ~11s build);
# stays well under the container's TIMEOUT_MAX where one is configured.
CCH_TIMEOUT = int(os.environ.get("CCH_TIMEOUT_MS", "1200000"))
db = FalkorDB(host="localhost", port=PORT)
g = db.select_graph(GRAPH)
print(f"[cch] target graph={GRAPH!r} port={PORT}")

def q(cypher, **params):
    return g.query(cypher, params, timeout=CCH_TIMEOUT) if params \
        else g.query(cypher, timeout=CCH_TIMEOUT)

# ---- graph size ----
n = q("MATCH (n:Intersection) RETURN count(n)").result_set[0][0]
e = q("MATCH ()-[r:ROAD]->() RETURN count(r)").result_set[0][0]
print(f"[graph] {n} Intersection nodes, {e} ROAD edges")

# ---- build the CCH index (DDL) ----
# drop any pre-existing index so the build is idempotent (re-runnable)
try:
    q(f"DROP CCH INDEX FOR ()-[e:{REL_PATTERN}]->() ON (e.{WEIGHTPROP})")
    print("[cch] dropped existing CCH index")
except Exception:
    pass   # none existed — fine

print(f"[cch] building CCH index: CREATE CCH INDEX FOR ()-[e:{REL_PATTERN}]->() ON (e.{WEIGHTPROP}) ...",
      flush=True)
t0 = time.time()
q(f"CREATE CCH INDEX FOR ()-[e:{REL_PATTERN}]->() ON (e.{WEIGHTPROP})")
dt = time.time() - t0
print(f"[cch] done in {dt:.1f}s")

# confirm the index is registered
idx = q("CALL db.indexes() YIELD label, types RETURN label, types").result_set
print(f"[cch] indexes: {idx}")

# ---- sample point-to-point query: southern-most -> northern-most node ----
south = q("MATCH (n:Intersection) RETURN n.osmid ORDER BY n.lat ASC  LIMIT 1").result_set[0][0]
north = q("MATCH (n:Intersection) RETURN n.osmid ORDER BY n.lat DESC LIMIT 1").result_set[0][0]
print(f"[query] routing osmid {south} (south) -> {north} (north)")
t0 = time.time()
qr = q("""MATCH (s:Intersection {osmid:$s}), (t:Intersection {osmid:$t})
          CALL db.idx.cch.query({sourceNode:s, targetNode:t,
                                 relTypes:$rels, weightProp:$w})
          YIELD pathWeight, path
          RETURN pathWeight, length(path)""", s=south, t=north,
       rels=RELTYPES, w=WEIGHTPROP)
dt = time.time() - t0
if qr.result_set and qr.result_set[0][0] is not None:
    w, plen = qr.result_set[0]
    unit = f"{w/60:.1f} min" if WEIGHTPROP == "time" else f"{w/1000:.1f} km"
    print(f"[query] path {WEIGHTPROP}={w:.1f} ({unit}), {plen} hops, {dt:.3f}s")
else:
    print(f"[query] no path found ({dt:.3f}s)")
print("[done]")
