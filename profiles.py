"""
Map profile for the road-network explorer (California / US).

The app itself is map-agnostic: everything specific to the road network — which
FalkorDB graph/port to hit, the graph's schema (labels, rel types, property names),
the node-id type, the CCH weight's unit, which precomputed artifact files exist,
which features make sense, and the initial UI framing — lives here and is selected
with the PROFILE env var (default "us").

CCH is a graph-level path index (db.idx.cch.*): the hierarchy's shortcut arcs and
node ranks live inside the index, not as SHORTCUT edges / rank properties, so the
schema only needs the road relationship type and its weight property.

The index is built over `cch_weight_prop` ("time"), so routing minimizes drive TIME:
each ROAD arc carries a `time` attribute (add_traveltime.py), `weight` (metres) stays
for the distance readout, and changing `time` (congestion) is absorbed by the index's
incremental maintenance — the next query reroutes with no manual rebuild.
"""

# US (California region) city label anchors — spread N→S so the map has
# orientation across the whole state; OSM places.json supplies the rest.
_US_CITIES = [
    {"name": "Los Angeles",   "lng": -118.2437, "lat": 34.0522},
    {"name": "San Diego",     "lng": -117.1611, "lat": 32.7157},
    {"name": "San Jose",      "lng": -121.8863, "lat": 37.3382},
    {"name": "San Francisco", "lng": -122.4194, "lat": 37.7749},
    {"name": "Fresno",        "lng": -119.7871, "lat": 36.7378},
    {"name": "Sacramento",    "lng": -121.4944, "lat": 38.5816},
    {"name": "Oakland",       "lng": -122.2712, "lat": 37.8044},
    {"name": "Bakersfield",   "lng": -119.0187, "lat": 35.3733},
    {"name": "Long Beach",    "lng": -118.1937, "lat": 33.7701},
    {"name": "Riverside",     "lng": -117.3755, "lat": 33.9806},
    {"name": "Stockton",      "lng": -121.2908, "lat": 37.9577},
    {"name": "Santa Barbara", "lng": -119.6982, "lat": 34.4208},
    {"name": "Palm Springs",  "lng": -116.5453, "lat": 33.8303},
    {"name": "Modesto",       "lng": -120.9969, "lat": 37.6391},
    {"name": "South Lake Tahoe", "lng": -119.9772, "lat": 38.9399},
    {"name": "Redding",       "lng": -122.3917, "lat": 40.5865},
    {"name": "Eureka",        "lng": -124.1637, "lat": 40.8021},
    {"name": "Bishop",        "lng": -118.3951, "lat": 37.3634},
]

PROFILES = {
    "us": {
        # OSM California through the parse pipeline. The full 50-state US graph
        # (~20M nodes, ~100M+ CCH shortcuts) can't fit this Mac's RAM (the CCH build
        # peak alone needs tens of GB), so the delivered graph is the largest region
        # that fits: California by default (~1.25M nodes).
        # setup_us.sh's STATES var grows the region on a bigger VM. Real curved
        # geometry, street + place names.
        "graph": "us_roads",
        "port": 6500,
        "schema": {
            "node_label": "Intersection", "id_prop": "osmid", "id_type": "int",
            "road_type": "ROAD", "weight_prop": "weight",
            "cch_weight_prop": "time",       # route by drive time + congestion
        },
        "unit": "m",
        "straight_geometry": False,
        "files": {
            "nodes": "data/us/nodes.csv", "geom": "data/us/geom.json",
            "names": "data/us/names.json", "places": "data/us/places.json",
            "roads": "data/us/roads.csv",
        },
        "features": {
            "alternatives": True, "traveltime": True,
            "street_labels": True, "place_labels": True,
        },
        "ui": {
            "title": "🇺🇸 US Road Network",
            "subtitle": "California region · FalkorDB · Customizable Contraction Hierarchies",
            "view": {"longitude": -119.6, "latitude": 37.3, "zoom": 5.15},
            "geocode_bbox": [-124.55, 32.45, -114.05, 42.05],
            "geocode_bias": {"lat": 34.05, "lon": -118.24},
            "cities": _US_CITIES,
        },
    },
}


def get_profile(name):
    if name not in PROFILES:
        raise SystemExit(f"unknown PROFILE {name!r}; choose from {sorted(PROFILES)}")
    return PROFILES[name]
