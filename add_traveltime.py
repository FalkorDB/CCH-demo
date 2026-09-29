#!/usr/bin/env python3
"""Give each ROAD arc a `time` attribute (drive-time seconds) for time-based CCH routing.

`time = length_metres / free_flow_speed(class)`, where the speed comes from the arc's
OSM highway class (parse_pbf.py emits a `class` column). Free-flow speeds are realistic
per class (motorway ~110 km/h ... residential ~35), so the fastest route by time
genuinely differs from the shortest by distance.

Idempotent and non-destructive — the output keeps the `class` column, so re-running
recomputes `time` from it every time (edit SPEED_KMH, re-run, done). Cases:
  * roads.csv has `class`  -> compute time from class; write src,dst,weight,class,time
  * roads.csv has `time` but no `class` (already processed by an older build, class
    unavailable) -> keep the existing time as-is (never clobber good times with a proxy)
  * roads.csv has only weight (no class, no time) -> fall back to a name-based proxy
    (numbered routes = highway) using names.json; write src,dst,weight,time

Usage:
    ./venv/bin/python add_traveltime.py [roads.csv] [names.json]
"""
import sys, os, csv, json

# free-flow drive speed (km/h) by OSM highway class. MAX_KMH (the fastest of these) is
# what app.py's A* heuristic uses to stay an admissible time lower-bound -- keep them
# in sync (app.py.MAX_KMH).
SPEED_KMH = {
    "motorway": 110, "motorway_link": 60,
    "trunk": 95,     "trunk_link": 55,
    "primary": 80,   "primary_link": 50,
    "secondary": 65, "secondary_link": 45,
    "tertiary": 55,  "tertiary_link": 40,
    "unclassified": 50, "residential": 35, "living_street": 15, "road": 40,
}
DEFAULT_KMH = 45.0
MAX_KMH = max(SPEED_KMH.values())          # 110 -> A* heuristic scalar (see app.py)

def speed_mps(cls):
    return SPEED_KMH.get(cls, DEFAULT_KMH) / 3.6

# --- fallback (no class, no time): infer a coarse class from the street name ---
def ekey(a, b):
    return f"{a}-{b}" if a < b else f"{b}-{a}"

def _proxy_speed_mps(name):
    if name is None:              return 40.0 / 3.6     # minor
    if name.startswith("Route "): return 90.0 / 3.6    # numbered route == highway
    return 50.0 / 3.6                                    # named arterial/street

def main(roads_csv="roads.csv", names_json="names.json"):
    if not os.path.exists(roads_csv):
        raise SystemExit(f"missing {roads_csv}")
    with open(roads_csv) as f:
        header = next(csv.reader(f))
    has_class = "class" in header
    has_time  = "time" in header

    # already processed and class is gone -> keep existing times (don't clobber)
    if has_time and not has_class:
        print(f"[traveltime] {roads_csv}: already has `time` (no `class` to recompute "
              f"from) — kept as-is", flush=True)
        return

    names = {}
    if not has_class and os.path.exists(names_json):
        with open(names_json) as f:
            names = json.load(f)

    out_class = has_class
    rows, tally = [], {}
    with open(roads_csv) as f:
        r = csv.DictReader(f)
        for row in r:
            src, dst, weight = row["src"], row["dst"], float(row["weight"])
            if has_class:
                cls = row.get("class") or "unclassified"
                sp = speed_mps(cls)
            else:
                cls = "(proxy)"
                sp = _proxy_speed_mps(names.get(ekey(src, dst)))
            tally[cls] = tally.get(cls, 0) + 1
            t = f"{weight / sp:.3f}"
            rows.append((src, dst, f"{weight:.3f}", cls, t) if out_class
                        else (src, dst, f"{weight:.3f}", t))

    tmp = roads_csv + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["src", "dst", "weight", "class", "time"] if out_class
                   else ["src", "dst", "weight", "time"])
        w.writerows(rows)
    os.replace(tmp, roads_csv)
    src_desc = "class" if has_class else "name-proxy (no class column)"
    top = ", ".join(f"{k}={v}" for k, v in sorted(tally.items(), key=lambda x: -x[1])[:6])
    print(f"[traveltime] {roads_csv}: {len(rows)} arcs from {src_desc}; "
          f"max {MAX_KMH:.0f} km/h; classes: {top}", flush=True)

if __name__ == "__main__":
    roads = sys.argv[1] if len(sys.argv) > 1 else "roads.csv"
    names = sys.argv[2] if len(sys.argv) > 2 else "names.json"
    main(roads, names)
