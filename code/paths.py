"""
Where the inputs and the heavy intermediate files live.

The defaults point into the code/inputs/ folder, so a fresh copy runs
once the inputs listed in the README are placed there. A local
paths_local.json (not published) can point any entry elsewhere — a second
drive, a GIS install — without touching the code.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULTS = {
    "foursquare_places": "inputs/foursquare_places.gpkg",      # Foursquare Open Source Places, Istanbul
    "foursquare_table": "poi_fsq_istanbul_dovey_v3",
    "overture_places": "inputs/overture/place_point.gpkg",
    "overture_buildings": "inputs/overture/building_polygon.gpkg",
    "pedestrian": "inputs/pedestrian_infrastructure.gpkg",     # crossings, footbridges, underpasses
    "land": "inputs/land.gpkg",                                # land polygon; the sea is what lies outside it
    "osm_pbf": "inputs/osm/turkey-latest.osm.pbf",
    "osm_gpkg": "inputs/osm/istanbul_osm.gpkg",
    "tiles": "work/ground_city",                              # per-tile outputs of the citywide run
    "qgis": "C:/Program Files/QGIS 3.44.12",                  # ogr2ogr and a Python with openpyxl
}


def _load():
    p = dict(DEFAULTS)
    local = ROOT / "paths_local.json"
    if local.exists():
        p.update(json.loads(local.read_text(encoding="utf-8")))
    return {k: (v if k == "foursquare_table" else (Path(v) if Path(v).is_absolute() else ROOT / v))
            for k, v in p.items()}


P = _load()
