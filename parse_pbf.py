#!/usr/bin/env python3
"""
Extract the drivable road network from an OSM .pbf into FalkorDB bulk-loader CSVs.

Model:
  * nodes  = OSM nodes that survive graph simplification (intersections + dead-ends)
  * edges  = ROAD relationships, DIRECTED. Each surviving segment is emitted only
             in its legally drivable direction(s): two-way streets produce both
             u->v and v->u; one-way streets produce a single arc. The OSM `oneway`
             tag (plus implicit oneway for motorways and roundabouts) is respected.
             FalkorDB's CCH path index (db.idx.cch.*) honours these per-arc
             directional weights, so one-way streets are respected during routing.
  * weight = geodesic length in metres (haversine), summed across contracted chains.
  * names  = per-edge street name / road ref, kept in names.json for the UI's
             turn-by-turn driving directions.

Simplification: interior "continuation" nodes (a car merely passes through them)
are contracted away — the directed generalisation of degree-2 chain contraction,
collapsing long node-runs between junctions into single arcs while preserving each
arc's travel direction.
"""
import sys, os, csv, math, json
from collections import defaultdict
import osmium

# classic "drive" network highway classes (service/track/foot/cycle excluded)
DRIVE = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "road",
    "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link",
}

R = 6371000.0  # earth radius, metres
def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def oneway_mode(tags):
    """Which direction(s) a way may be driven: 'forward' (node order),
    'backward' (against node order, oneway=-1), or 'both'."""
    ow = tags.get("oneway")
    if ow in ("yes", "true", "1"):
        return "forward"
    if ow in ("-1", "reverse"):
        return "backward"
    if ow in ("no", "false", "0"):
        return "both"
    if ow in ("reversible", "alternating"):
        return "both"            # time-dependent; treat as two-way in a static graph
    # OSM implicit oneway: motorways and roundabouts are oneway in node order
    if tags.get("highway") == "motorway" or tags.get("junction") in ("roundabout", "circular"):
        return "forward"
    return "both"

_name_pool = {}                  # intern street-name strings to save memory
def way_name(tags):
    nm = tags.get("name") or tags.get("name:en")
    if not nm:
        ref = tags.get("ref")
        nm = f"Route {ref}" if ref else None
    if nm is None:
        return None
    return _name_pool.setdefault(nm, nm)


def main(pbf, outdir="."):
    coords = {}                      # nid -> (lat, lon)
    succ = defaultdict(dict)         # directed: u -> {v: weight}
    pred = defaultdict(dict)         # reverse index: v -> {u: weight}
    seg_name = {}                    # (min,max) raw node pair -> street name
    seg_class = {}                   # (min,max) raw node pair -> OSM highway class

    def add_arc(u, v, w):
        if u == v:
            return
        cur = succ[u].get(v)
        if cur is None or w < cur:
            succ[u][v] = w
            pred[v][u] = w

    print(f"[parse] reading {pbf} ...", flush=True)
    ways = 0
    oneway_ways = 0
    fp = osmium.FileProcessor(pbf).with_locations()
    for o in fp:
        if not o.is_way():
            continue
        hw = o.tags.get("highway")
        if hw not in DRIVE:
            continue
        ways += 1
        mode = oneway_mode(o.tags)
        if mode != "both":
            oneway_ways += 1
        nm = way_name(o.tags)
        prev_id = prev_lat = prev_lon = None
        for nd in o.nodes:
            loc = nd.location
            if not loc.valid():
                prev_id = None
                continue
            nid = nd.ref
            lat, lon = loc.lat, loc.lon
            coords[nid] = (lat, lon)
            if prev_id is not None:
                w = haversine(prev_lat, prev_lon, lat, lon)
                # emit arcs only in the legally drivable direction(s)
                if mode in ("forward", "both"):
                    add_arc(prev_id, nid, w)
                if mode in ("backward", "both"):
                    add_arc(nid, prev_id, w)
                key = (prev_id, nid) if prev_id < nid else (nid, prev_id)
                if nm is not None:
                    seg_name.setdefault(key, nm)
                seg_class.setdefault(key, hw)      # road class -> drive speed later
            prev_id, prev_lat, prev_lon = nid, lat, lon
        if ways % 20000 == 0:
            print(f"[parse]   {ways} drive ways, {len(coords)} nodes so far", flush=True)

    allnodes = set(succ) | set(pred)
    raw_nodes = len(allnodes)
    raw_arcs = sum(len(v) for v in succ.values())
    print(f"[parse] raw drive graph: {raw_nodes} nodes, {raw_arcs} directed arcs "
          f"(from {ways} ways, {oneway_ways} one-way)", flush=True)

    # -----------------------------------------------------------------------
    # simplify: contract interior "continuation" nodes into single directed arcs.
    # A node is an ENDPOINT (kept) unless a car merely passes straight through it.
    # Directed generalisation of degree-2 contraction (cf. OSMnx _is_endpoint):
    #   * self-loop                              -> endpoint
    #   * no in-edges or no out-edges            -> endpoint (source / sink / dead-end)
    #   * exactly 2 distinct neighbours AND total degree 2 (one-way through: a->n->b)
    #     or 4 (two-way through: a<->n<->b)      -> continuation (contract away)
    #   * anything else (junction, oneway<->twoway transition, parallel edge) -> endpoint
    # -----------------------------------------------------------------------
    def is_endpoint(n):
        s = succ.get(n)
        p = pred.get(n)
        outd = len(s) if s else 0
        ind = len(p) if p else 0
        if (s and n in s) or (p and n in p):        # self-loop
            return True
        if ind == 0 or outd == 0:                    # source / sink / dead-end
            return True
        nbrs = set(s) | set(p)
        if len(nbrs) == 2 and (ind + outd) in (2, 4):
            return False                             # pure pass-through
        return True

    endpoints = {n for n in allnodes if is_endpoint(n)}
    print(f"[simplify] {len(endpoints)} endpoint/junction nodes "
          f"({raw_nodes - len(endpoints)} continuation nodes to contract)", flush=True)

    dsucc = defaultdict(dict)         # simplified directed graph over endpoints
    dpred = defaultdict(dict)
    geom = {}                         # (min(u,v),max(u,v)) -> polyline [[lat,lon],...]
    geom_w = {}                       # same key -> weight of the arc that owns the geom
    name = {}                         # (min,max) -> street name for the contracted arc
    arc_class = {}                    # (min,max) -> dominant highway class of the arc

    def add_simp_arc(u, v, w, chain):
        if u == v:
            return
        cur = dsucc[u].get(v)
        if cur is None or w < cur:
            dsucc[u][v] = w
            dpred[v][u] = w
        key = (u, v) if u < v else (v, u)
        if key not in geom_w or w < geom_w[key]:
            pts = [coords[n] for n in chain]
            if key[0] != u:
                pts = pts[::-1]       # store canonically low-id -> high-id
            geom[key] = pts
            geom_w[key] = w
            # pick the name covering the most length along the contracted chain
            by_name = defaultdict(float)
            for a, b in zip(chain, chain[1:]):
                k = (a, b) if a < b else (b, a)
                nm = seg_name.get(k)
                if nm is not None:
                    by_name[nm] += succ[a].get(b, succ[b].get(a, 0.0))
            if by_name:
                name[key] = max(by_name, key=by_name.get)
            elif key in name:
                del name[key]
            # dominant road class along the chain (by covered length) -> drive speed
            by_class = defaultdict(float)
            for a, b in zip(chain, chain[1:]):
                k = (a, b) if a < b else (b, a)
                c = seg_class.get(k)
                if c:
                    by_class[c] += succ[a].get(b, succ[b].get(a, 0.0))
            if by_class:
                arc_class[key] = max(by_class, key=by_class.get)
            elif key in arc_class:
                del arc_class[key]

    # walk every directed chain starting at an endpoint, following edge direction,
    # until the next endpoint; contract it into one arc.
    for j in endpoints:
        for s in list(succ.get(j, ())):
            prev, cur = j, s
            total = succ[j][s]
            chain = [j, s]
            while not is_endpoint(cur):
                nxts = [x for x in succ.get(cur, ()) if x != prev]
                if not nxts:            # continuation node with nowhere new to go
                    break
                nn = nxts[0]
                total += succ[cur][nn]
                prev, cur = cur, nn
                chain.append(nn)
            add_simp_arc(j, cur, total, chain)

    # all-continuation isolated loops are dropped (no endpoint to anchor them); rare.
    simp_nodes = len(set(dsucc) | set(dpred))
    simp_arcs = sum(len(v) for v in dsucc.values())
    print(f"[simplify] simplified graph: {simp_nodes} nodes, {simp_arcs} directed arcs",
          flush=True)

    # -----------------------------------------------------------------------
    # keep only the largest STRONGLY-connected component: guarantees every kept
    # node can both reach and be reached from every other, so directed routing
    # between any pair always succeeds (no dead-end one-way traps).
    # -----------------------------------------------------------------------
    def largest_scc():
        # Kosaraju, fully iterative (the graph is far too large to recurse).
        visited = set()
        order = []
        for start in list(dsucc.keys()) + [n for n in dpred if n not in dsucc]:
            if start in visited:
                continue
            visited.add(start)
            stack = [(start, iter(dsucc.get(start, ())))]
            while stack:
                node, it = stack[-1]
                pushed = False
                for w in it:
                    if w not in visited:
                        visited.add(w)
                        stack.append((w, iter(dsucc.get(w, ()))))
                        pushed = True
                        break
                if not pushed:
                    order.append(node)
                    stack.pop()
        comp = {}
        cid = 0
        sizes = defaultdict(int)
        for start in reversed(order):
            if start in comp:
                continue
            cid += 1
            stack = [start]
            comp[start] = cid
            while stack:
                x = stack.pop()
                sizes[cid] += 1
                for y in dpred.get(x, ()):
                    if y not in comp:
                        comp[y] = cid
                        stack.append(y)
        best = max(sizes, key=sizes.get) if sizes else None
        return {n for n, c in comp.items() if c == best}, len(sizes)

    keep, n_comp = largest_scc()
    print(f"[component] {n_comp} strongly-connected components; "
          f"largest has {len(keep)} nodes ({len(keep)}/{simp_nodes})", flush=True)

    # -----------------------------------------------------------------------
    # write outputs
    # -----------------------------------------------------------------------
    with open(os.path.join(outdir, "nodes.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["osmid", "lat", "lon"])
        for n in keep:
            lat, lon = coords[n]
            w.writerow([n, f"{lat:.7f}", f"{lon:.7f}"])

    edge_count = 0
    with open(os.path.join(outdir, "roads.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["src", "dst", "weight", "class"])
        for u in keep:
            for v, wt in dsucc.get(u, {}).items():
                if v in keep:
                    key = (u, v) if u < v else (v, u)
                    cls = arc_class.get(key, "unclassified")
                    w.writerow([u, v, f"{wt:.3f}", cls])   # directional: only legal arcs
                    edge_count += 1
    print(f"[write] nodes.csv: {len(keep)} nodes; roads.csv: {edge_count} directed ROAD arcs",
          flush=True)

    # per-edge road geometry (real polylines), keyed "u-v" with u<v, rounded to ~1m
    geom_out = {}
    pts_total = 0
    for (a, b), pts in geom.items():
        if a in keep and b in keep:
            rp = [[round(la, 6), round(lo, 6)] for la, lo in pts]
            geom_out[f"{a}-{b}"] = rp
            pts_total += len(rp)
    with open(os.path.join(outdir, "geom.json"), "w") as f:
        json.dump(geom_out, f, separators=(",", ":"))
    print(f"[write] geom.json: {len(geom_out)} edge polylines, {pts_total} points", flush=True)

    # per-edge street name / road ref, keyed "u-v" with u<v (for turn-by-turn directions)
    name_out = {}
    for (a, b), nm in name.items():
        if a in keep and b in keep:
            name_out[f"{a}-{b}"] = nm
    with open(os.path.join(outdir, "names.json"), "w") as f:
        json.dump(name_out, f, separators=(",", ":"), ensure_ascii=False)
    print(f"[write] names.json: {len(name_out)} named edges "
          f"({100*len(name_out)/max(1,len(geom_out)):.0f}% of edges)", flush=True)
    print("[done]", flush=True)
    return dict(nodes=len(keep), edges=edge_count, geom=len(geom_out), names=len(name_out))


if __name__ == "__main__":
    pbf = sys.argv[1] if len(sys.argv) > 1 else "us-california-latest.osm.pbf"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    main(pbf, outdir)
