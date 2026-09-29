#!/usr/bin/env python3
"""
Extract named places (cities, towns, villages, suburbs, ...) from an OSM .pbf
into places.json, so the map UI can show real place names for orientation
instead of a handful of hard-coded cities.

Output: places.json = [{"name","lat","lon","place","pop"}], where
  * place  = OSM place class (city|town|village|suburb|neighbourhood|hamlet)
  * pop    = population (int, 0 if unknown) — used to rank same-class places
Names prefer name:en, then name (local Hebrew/Arabic), for a readable label.
"""
import sys, os, json
import osmium

# place classes worth labelling, most prominent first (index = rank)
PLACE_RANK = ["city", "town", "village", "suburb", "neighbourhood", "hamlet"]
PLACE_SET  = set(PLACE_RANK)

def _pop(tags):
    raw = tags.get("population")
    if not raw:
        return 0
    try:
        return int("".join(ch for ch in raw if ch.isdigit()) or "0")
    except ValueError:
        return 0

def main(pbf, outpath="places.json"):
    print(f"[places] reading {pbf} ...", flush=True)
    out = []
    seen = set()                       # (name, place) de-dup of exact repeats
    fp = osmium.FileProcessor(pbf).with_locations()
    for o in fp:
        if not o.is_node():
            continue
        pl = o.tags.get("place")
        if pl not in PLACE_SET:
            continue
        nm = o.tags.get("name:en") or o.tags.get("name")
        if not nm:
            continue
        loc = o.location
        if not loc.valid():
            continue
        key = (nm, pl)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": nm, "lat": round(loc.lat, 6), "lon": round(loc.lon, 6),
                    "place": pl, "pop": _pop(o.tags)})
    # stable, useful ordering: rank then population desc
    out.sort(key=lambda p: (PLACE_RANK.index(p["place"]), -p["pop"]))
    with open(outpath, "w") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    by_class = {}
    for p in out:
        by_class[p["place"]] = by_class.get(p["place"], 0) + 1
    print(f"[places] wrote {len(out)} places to {outpath}: {by_class}", flush=True)
    return len(out)

if __name__ == "__main__":
    pbf = sys.argv[1] if len(sys.argv) > 1 else "us-california-latest.osm.pbf"
    outpath = sys.argv[2] if len(sys.argv) > 2 else "places.json"
    main(pbf, outpath)
