"""
Ground-plane publicness for a study box.

The citywide estimate counted venue points within 800 m. That reading broke down
on large sites: Atatürk Airport scored at the 0th percentile, the city's floor,
because its cells held plane and gate check-ins and a single "inaccessible"
label, while the ground is a patchwork of runway, military land, a fairground,
a hospital and a nation's garden.

This script reads the ground plane instead, after Dovey and Pafka (2020), who
map ground-floor area by access regime rather than counting places. Every square
metre of a cell is assigned to one regime from current OpenStreetMap geometry.
Place records — Foursquare and Overture — are used only as evidence of what a
building's ground floor is, never counted.

Rules come from rules.json, compiled from publicness_kurallar.xlsx, where every
regime, score and cleaning rule was decided and can be traced. Decisions applied
here (numbers as in the workbook's 'kararlar' sheet):

    D01  scores: P = A x [a + (1 - a) C] with a = 0.5; other schemes reported
    D02  car space out of the score, its share reported
    D03  residential ground between buildings a separate class, scored as invitation
    D04  a building of unknown use stays out of the score unless a place record
         lies inside it or within 10 m
    D05/06  mosques and cemevi open-public; other worship as the site rules say
    D07  fee tag: fee=yes conditions entry (ticketed); fee=no opens it, and the
         regime then follows who controls the site — public operator open-public,
         private operator quasi-public, unknown flagged for a decision
    D08  a building takes the most frequent regime among its place records,
         ties to the more public
    D09  Foursquare and Overture both count; a matched pair counts once; old
         Foursquare records with no Overture counterpart are dropped
    D12  barrier: major roads and surface rail, their length, how many pieces
         they cut the cell's ground into, and the pedestrian crossings over them
    D14  forest and open natural ground out of the score, their share reported;
         D18: forest under 50 ha (an urban grove, koru) is open-public
    D15  urban grass (landuse=grass) open-public; meadow, farmland and the rest
         of natural ground out of the score
    D16  a cell whose scored ground is under a tenth of its land is shown as
         'kanıt yetersiz' (insufficient evidence); its value stays in the table
    D17  beaches open-public; with fee=yes ticketed

Layers are painted in priority order; where two overlap the higher one wins:

    water, military, airfield, surface rail, buildings, platforms, pedestrian
    ways and squares, parks, named sites, closed land uses, modelled sidewalks,
    carriageway, residential yards, rest of the aerodrome, grass and natural
    ground, unmapped

Inputs   osm_<name>.json, rules.json, Foursquare v3 and Overture place and
         building geopackages, source_matches.csv, the pedestrian
         infrastructure geopackage, cells_estimate.geojson
Outputs  ground_<name>_cells.csv, ground_<name>_layers.geojson,
         ground_<name>_run.json, ground_<name>_kontrol_bilinmeyen.csv

Run:  py ground_plane.py ataturk
"""

import csv
import glob
import json
import math
import re
import sqlite3
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

import shapely
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import polygonize, transform, unary_union

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
MAP = HERE.parent
RULES = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(HERE))
from paths import P  # noqa: E402  — input locations (paths.py, paths_local.json)

FSQ_GPKG = P["foursquare_places"]
FSQ_TABLE = P["foursquare_table"]
OVT_PLACES = P["overture_places"]
OVT_BUILDINGS = P["overture_buildings"]
# the province strips beyond the first Overture export, same release (2026-09-23.1)
OVT_PLACES_ALL = [f for f in (OVT_PLACES, P["overture_places_ek"]) if f.exists()]
OVT_BUILDINGS_ALL = [f for f in (OVT_BUILDINGS, P["overture_buildings_ek"]) if f.exists()]
PEDESTRIAN = P["pedestrian"]
MATCHES = HERE / "source_matches.csv"
OSM_GPKG = P["osm_gpkg"]          # citywide, cut from the Geofabrik extract by extract_osm.py
LAND = P["land"]

AREAS = {
    "ataturk": (40.958, 28.785, 41.002, 28.848),      # south, west, north, east
    "gazhane": (40.9787, 29.0189, 41.0147, 29.0669),  # Müze Gazhane, Hasanpaşa, ±2 km
}
# close-reading boxes picked from the citywide divergence review (divergent_cells.csv),
# ±1 km around a cell; read from the citywide OSM GeoPackage, not Overpass
SAMPLE_CENTRES = {
    "hasdal": (41.12683, 28.94446),     # military edge: new 0.00, old top 3 %
    "gokturk": (41.18969, 28.90281),    # yards and forest: new 0.17, old top 15 %
    "levent": (41.07520, 29.01290),     # business district: new 0.70 (C), old bottom 20 %
    "sisli": (41.06173, 28.99206),      # 19 Mayıs, Şişli: new 0.81, old bottom 10 %
}
SAMPLES = {k: (la - 0.009, lo - 0.012, la + 0.009, lo + 0.012) for k, (la, lo) in SAMPLE_CENTRES.items()}

REGIMES = ["open_public", "quasi_public", "ticketed", "open_private", "invitation", "inaccessible"]
SCORE = RULES["scores"]
SENS = RULES["sensitivity"]
CLEAN = RULES["cleaning"]
OSM_RULE = RULES["osm"]
OV_DICT = RULES["overture"]
FSQ_DICT = RULES["foursquare"]

ATTACH_M = 10.0          # D04: a place record this close to a footprint belongs to it
MS_OVERLAP = 0.3         # a Microsoft footprint overlapping OSM more than this is a duplicate
MIN_PIECE_M2 = 2000.0    # D12: ground fragments smaller than this are slivers, not pieces
CROSSING_M = 15.0        # D12: a crossing this close to a barrier line crosses it

# defaults; any of these can be overridden from the workbook's osm_kurallari sheet
SITE_REGIME = {
    ("amenity", "parking"): "ticketed", ("amenity", "exhibition_centre"): "ticketed",
    ("amenity", "conference_centre"): "ticketed", ("amenity", "events_venue"): "invitation",
    ("amenity", "hospital"): "open_private", ("amenity", "clinic"): "open_private",
    ("amenity", "school"): "invitation", ("amenity", "kindergarten"): "invitation",
    ("amenity", "university"): "quasi_public", ("amenity", "college"): "invitation",
    ("amenity", "library"): "open_public", ("amenity", "place_of_worship"): "open_private",
    ("amenity", "marketplace"): "open_public", ("amenity", "bus_station"): "open_public",
    ("amenity", "community_centre"): "open_public", ("amenity", "social_centre"): "open_public",
    ("amenity", "courthouse"): "invitation", ("amenity", "townhall"): "invitation",
    ("amenity", "police"): "inaccessible", ("amenity", "fire_station"): "inaccessible",
    ("amenity", "waste_transfer_station"): "inaccessible", ("amenity", "fuel"): "open_private",
    ("amenity", "restaurant"): "open_private", ("amenity", "cafe"): "open_private",
    ("amenity", "fast_food"): "open_private", ("amenity", "bank"): "open_private",
    ("amenity", "ferry_terminal"): "ticketed", ("amenity", "theatre"): "ticketed",
    ("amenity", "cinema"): "ticketed", ("amenity", "arts_centre"): "quasi_public",
    ("landuse", "cemetery"): "open_public", ("landuse", "religious"): "open_private",
    ("landuse", "commercial"): "open_private", ("landuse", "retail"): "open_private",
    ("landuse", "education"): "invitation",
    ("leisure", "pitch"): "ticketed", ("leisure", "sports_centre"): "ticketed",
    ("leisure", "stadium"): "ticketed", ("leisure", "swimming_pool"): "ticketed",
    ("leisure", "track"): "open_public", ("leisure", "fitness_station"): "open_public",
    ("leisure", "dog_park"): "open_public", ("leisure", "outdoor_seating"): "open_private",
    ("tourism", "museum"): "ticketed", ("tourism", "gallery"): "open_private",
    ("tourism", "attraction"): "quasi_public", ("tourism", "zoo"): "ticketed",
    ("tourism", "theme_park"): "ticketed", ("tourism", "viewpoint"): "open_public",
    ("tourism", "picnic_site"): "open_public", ("tourism", "hotel"): "open_private",
    ("tourism", "hostel"): "open_private",
    # infrastructure, missing from the first queries; Overture's infrastructure
    # layer showed it was absent
    ("man_made", "wastewater_plant"): "inaccessible", ("man_made", "water_works"): "inaccessible",
    ("man_made", "storage_tank"): "inaccessible", ("man_made", "silo"): "inaccessible",
    ("man_made", "reservoir_covered"): "inaccessible", ("man_made", "works"): "invitation",
    ("man_made", "pier"): "open_public", ("man_made", "quay"): "open_public",
    ("man_made", "breakwater"): "open_public",
    ("power", "substation"): "inaccessible", ("power", "plant"): "inaccessible",
    ("power", "generator"): "inaccessible",
    ("amenity", "toilets"): "open_public", ("amenity", "fountain"): "open_public",
}
SITE_KEYS = ("amenity", "tourism", "man_made", "power", "landuse", "leisure")
PUBLIC_OPEN = {("leisure", "park"), ("leisure", "garden"), ("leisure", "playground"),
               ("leisure", "nature_reserve"), ("place", "square"), ("highway", "pedestrian"),
               ("landuse", "village_green"), ("landuse", "recreation_ground"),
               ("natural", "beach")}       # D17: beaches open-public; fee=yes makes them ticketed
INVITATION_LAND = {("landuse", "industrial"), ("aeroway", "hangar"),
                   ("landuse", "construction"), ("landuse", "brownfield")}
# D14/D15: forest, open natural ground and farmland are out of the score, their
# share reported (like car space); urban grass is open-public. Painted last, so
# anything else mapped on the same ground wins.
NATURAL = {**{("landuse", v): "natural" for v in ("forest", "meadow", "farmland", "farmyard", "orchard",
                                                  "vineyard", "plant_nursery", "quarry")},
           **{("natural", v): "natural" for v in ("wood", "grassland", "scrub", "heath", "wetland",
                                                  "bare_rock")},
           ("man_made", "clearcut"): "natural", ("landuse", "grass"): "open_public"}
MIN_SCORED = 0.10        # D16: below this scored share of the ground a cell is shown as 'kanıt yetersiz'
URBAN_FOREST_M2 = 500_000  # D18: forest under 50 ha is an urban grove, open-public; larger stays out
COAST_M = 30.0             # D21: the coastal strip painted open-public where OSM leaves it unmapped
# D21: the Bosphorus and its shores — north of the Sarayburnu–Harem line, south of the
# Rumeli Feneri–Anadolu Feneri line, east of the Golden Horn's mouth (lon, lat)
BOSPHORUS = box(28.990, 41.012, 29.150, 41.232)
AIRFIELD = {"runway", "taxiway", "apron", "helipad", "airstrip", "stopway"}
AIR_W = {"runway": 45.0, "taxiway": 23.0, "stopway": 45.0, "airstrip": 30.0}
CAR_W = {"motorway": 14, "trunk": 14, "primary": 12, "secondary": 10, "tertiary": 8,
         "unclassified": 6, "residential": 6, "service": 4, "motorway_link": 6, "trunk_link": 6,
         "primary_link": 6, "secondary_link": 6, "tertiary_link": 6, "track": 3, "road": 6}
WALK_W = {"primary": 3.0, "secondary": 3.0, "tertiary": 2.5, "unclassified": 2.0, "residential": 2.0,
          "primary_link": 2.0, "secondary_link": 2.0, "tertiary_link": 2.0}
PED_W = {"footway": 2.5, "path": 2.0, "steps": 3.0, "pedestrian": 6.0, "cycleway": 2.0,
         "living_street": 5.0, "bridleway": 2.0}
MAJOR_ROADS = {"motorway", "trunk", "primary", "motorway_link", "trunk_link", "primary_link"}
RAIL = ("rail", "light_rail", "tram", "narrow_gauge")
BUILDING_TAG = {
    "apartments": "invitation", "residential": "invitation", "house": "invitation",
    "detached": "invitation", "dormitory": "invitation", "terrace": "invitation",
    "school": "invitation", "kindergarten": "invitation", "office": "invitation",
    "industrial": "invitation", "warehouse": "invitation", "storage_tank": "inaccessible",
    "hangar": "invitation", "shed": "invitation", "garage": "invitation",
    "greenhouse": "invitation", "construction": "inaccessible",
    "mosque": "open_public", "church": "open_private", "religious": "open_private",
    "hospital": "open_private", "hotel": "open_private", "commercial": "open_private",
    "retail": "open_private", "train_station": "ticketed", "transportation": "ticketed",
    "civic": "quasi_public", "public": "quasi_public", "university": "quasi_public",
}

# D07: who controls a site, read from what OSM records about its operator
PUBLIC_CONTROL = re.compile(
    r"(büyükşehir|buyuksehir|belediye|municipal|\bibb\b|i̇bb|bakanl|ministry|\bt\.\s?c\.|valili|"
    r"kaymakaml|vakıflar|vakiflar|milli saraylar|kültür a\.?ş|kultur a\.?s|diyanet|devlet|"
    r"gov\.tr|bel\.tr)", re.I)


def rule(k, v, default):
    """A tag's regime: the workbook's decision if it has one, else the default."""
    r = OSM_RULE.get(f"{k}={v}")
    return r if r in REGIMES else default


# ---------------------------------------------------------------- geometry

class Local:
    """Equirectangular metres around the box centre — ample for a few km."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = math.cos(math.radians(lat0)) * 111_320.0
        self.ky = 110_574.0

    def fwd(self, lon, lat):
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def inv(self, x, y):
        return x / self.kx + self.lon0, y / self.ky + self.lat0

    def geom(self, g):
        return transform(lambda x, y, z=None: self.fwd(x, y), g)

    def back(self, g):
        return transform(lambda x, y, z=None: self.inv(x, y), g)


def coords(geometry, loc):
    return [loc.fwd(p["lon"], p["lat"]) for p in geometry if p]


def way_shape(el, loc):
    pts = coords(el.get("geometry") or [], loc)
    if len(pts) < 2:
        return None
    if len(pts) >= 4 and pts[0] == pts[-1] and is_area(el.get("tags", {})):
        poly = Polygon(pts)
        return poly if poly.is_valid else poly.buffer(0)
    return LineString(pts)


def relation_shape(el, loc):
    outer, inner = [], []
    for m in el.get("members", []):
        if m.get("type") != "way" or not m.get("geometry"):
            continue
        pts = coords(m["geometry"], loc)
        if len(pts) >= 2:
            (inner if m.get("role") == "inner" else outer).append(LineString(pts))
    if not outer:
        return None
    polys = list(polygonize(unary_union(outer)))
    if not polys:
        return None
    shape = unary_union(polys)
    if inner:
        holes = list(polygonize(unary_union(inner)))
        if holes:
            shape = shape.difference(unary_union(holes))
    return shape if shape.is_valid else shape.buffer(0)


AREA_KEYS = ("building", "landuse", "leisure", "amenity", "aeroway", "tourism", "man_made", "power",
             "natural", "place", "area:highway", "railway", "highway")


def is_area(t):
    if t.get("area") == "no":
        return False
    if "highway" in t and t.get("area") != "yes" and t.get("highway") != "pedestrian":
        return False
    # a wall or fence on its own is a line; but campuses, hospitals and gated
    # estates are often drawn as one closed way carrying both the site's tags and
    # barrier=wall — the boundary is the wall. Reading those as lines lost
    # Marmara University, Göztepe and GATA hospitals and dozens of schools.
    if t.get("barrier") in ("fence", "wall", "retaining_wall", "kerb", "ditch", "hedge", "city_wall") \
            and not any(k in t for k in AREA_KEYS if k not in ("highway",)):
        return False
    return any(k in t for k in AREA_KEYS)


def landform(t):
    """Peninsulas, straits and administrative boundaries come back as relations
    spanning a region — the Balkans among them. No rule uses them."""
    return "boundary" in t or t.get("natural") in ("peninsula", "strait", "bay", "cape", "isthmus")


def underground(t):
    try:
        layer = int(str(t.get("layer", "0")).split(";")[0])
    except ValueError:
        layer = 0
    return t.get("tunnel") in ("yes", "building_passage", "culvert") or layer < 0 \
        or t.get("location") == "underground"


def width(t, default):
    try:
        return float(str(t.get("width") or t.get("est_width")).replace("m", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


def gpkg_geom(blob):
    """GeoPackage geometry blob -> shapely: skip the GP header and its envelope."""
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(blob[3] >> 1) & 0b111]
    return shapely.from_wkb(bytes(blob[8 + env:]))


def gpkg_point(blob):
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(blob[3] >> 1) & 0b111]
    w = bytes(blob[8 + env:])
    return struct.unpack(("<" if w[0] == 1 else ">") + "dd", w[5:21])


def gpkg_in_box(path, bbox, cols, table=None):
    """Rows of a geopackage layer whose envelope meets the box, via its R-tree."""
    s, w, n, e = bbox
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    if table is None:
        table, gcol = con.execute("SELECT table_name, column_name FROM gpkg_geometry_columns").fetchone()
    else:
        gcol = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?",
                           (table,)).fetchone()[0]
    sel = ", ".join(f't."{c}"' for c in cols)
    q = (f'SELECT t."{gcol}"{", " + sel if cols else ""} FROM "{table}" t '
         f'JOIN "rtree_{table}_{gcol}" r ON t.fid = r.id '
         f"WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ?")
    rows = con.execute(q, (e, w, n, s)).fetchall()
    con.close()
    return rows


def load_sea(bbox, loc, frame):
    """OSM keeps the sea as coastline lines, not polygons: read it as whatever
    in the frame lies outside the land polygon."""
    if not LAND.exists():
        return None
    s, w, n, e = bbox
    land = [loc.geom(gpkg_geom(b).intersection(box(w - .01, s - .01, e + .01, n + .01)))
            for (b,) in gpkg_in_box(LAND, bbox, [])]
    return frame.difference(unary_union(land)) if land else None


# ---------------------------------------------------------------- rules

def is_open_worship(t):
    """D05/D06: mosques and cemevi, by any of the ways OSM records them."""
    name = str(t.get("name", "")).casefold()
    return (t.get("building") == "mosque" or
            (t.get("religion") == "muslim" and (t.get("amenity") == "place_of_worship"
                                                or t.get("landuse") == "religious")) or
            "cemevi" in name or t.get("denomination") in ("alevi", "alevism"))


def control(t):
    """'public', 'private' or None (unknown) — who runs a site: the workbook's
    'kontrol' sheet first, then the site's own tags."""
    decided = RULES.get("control", {}).get(str(t.get("name", "")).strip())
    if decided:
        return decided
    if t.get("ownership") in ("public", "municipal", "national", "state", "government") or \
            t.get("operator:type") in ("public", "government"):
        return "public"
    text = " ".join(str(t.get(k, "")) for k in ("operator", "owner", "website", "contact:website"))
    if PUBLIC_CONTROL.search(text):
        return "public"
    if t.get("operator") or t.get("owner"):
        return "private"
    return None


def apply_fee(t, regime):
    """D07. Returns (regime, note, control unknown?)."""
    if t.get("fee") == "yes" and regime in ("open_public", "quasi_public", "open_private"):
        return "ticketed", ", fee=yes", False
    if t.get("fee") == "no" and regime == "ticketed":
        # D13: for Dovey and Pafka a price of admission is a condition that may
        # or may not apply, and parking is their example — free parking is open
        if t.get("amenity") == "parking":
            return "open_public", ", fee=no, ücretsiz otopark", False
        c = control(t)
        if c == "public":
            return "open_public", ", fee=no, kamu kontrolü", False
        if c == "private":
            return "quasi_public", ", fee=no, özel kontrol", False
        return "quasi_public", ", fee=no, kontrol bilinmiyor", True
    return regime, "", False


def classify(t, g):
    """Which layer an OSM shape paints into: a list of (layer key, geometry, rule).

    The key's prefix sets the painting priority. Buildings come back as
    'building'; their regime depends on the evidence inside and is set later.
    A road yields its carriageway and its modelled sidewalks."""
    area = g.geom_type in ("Polygon", "MultiPolygon")
    if underground(t):
        return []
    if area and (t.get("natural") == "water" or t.get("water") or t.get("landuse") == "basin"):
        return [("p01_water", g, "su")]
    if area and t.get("landuse") == "military":
        return [(f"p02_{rule('landuse', 'military', 'inaccessible')}", g, "askeri alan")]
    if t.get("aeroway") in AIRFIELD:
        geom = g if area else g.buffer(width(t, AIR_W.get(t["aeroway"], 23.0)) / 2, cap_style="flat")
        return [(f"p03_{rule('aeroway', t['aeroway'], 'inaccessible')}", geom, f"havalimanı {t['aeroway']}")]
    if t.get("railway") in RAIL and not area:
        return [("p04_inaccessible", g.buffer(4.0, cap_style="flat"), "demiryolu, 8 m")]
    if area and "building" in t and t.get("building") != "no":
        return [("building", g, "bina")]
    if t.get("railway") == "platform" or t.get("public_transport") == "platform":
        return [("p06_ticketed", g if area else g.buffer(2.0, cap_style="flat"), "peron")]
    if "area:highway" in t and area:
        return [("p07_open_public", g, "yol alanı poligonu")]
    if t.get("highway") in PED_W and t.get("access") not in ("private", "no"):
        if area:
            return [("p07_open_public", g, f"yaya alanı {t['highway']}")]
        w = width(t, PED_W[t["highway"]])
        return [("p07_open_public", g.buffer(w / 2, cap_style="flat"), f"yaya yolu {t['highway']}, {w:g} m")]
    if area and any((k, t.get(k)) in PUBLIC_OPEN for k in ("leisure", "place", "highway", "landuse", "natural")) \
            and t.get("access") not in ("private", "no", "customers"):
        k = next(k for k in ("leisure", "place", "highway", "landuse", "natural") if (k, t.get(k)) in PUBLIC_OPEN)
        regime, note, _ = apply_fee(t, rule(k, t[k], "open_public"))
        return [(f"p08_{regime}", g, f"açık alan {k}={t[k]}{note}")]
    if area and any((k, t.get(k)) in SITE_REGIME for k in SITE_KEYS):
        k = next(k for k in SITE_KEYS if (k, t.get(k)) in SITE_REGIME)
        regime = rule(k, t[k], SITE_REGIME[(k, t[k])])
        note = ""
        if is_open_worship(t):
            regime, note = "open_public", ", cami/cemevi"
        regime, fee_note, unknown = apply_fee(t, regime)
        if t.get("barrier") in ("wall", "fence", "city_wall", "hedge"):
            note += f", {t['barrier']} ile çevrili"
        out = [(f"p09_{regime}", g, f"alan türü {k}={t[k]}{note}{fee_note}")]
        if unknown:
            CONTROL_UNKNOWN.append({k_: t.get(k_) for k_ in ("name", "tourism", "amenity", "leisure",
                                                              "fee", "operator", "website")})
        return out
    if area and any((k, t.get(k)) in INVITATION_LAND for k in ("landuse", "aeroway")):
        k = next(k for k in ("landuse", "aeroway") if (k, t.get(k)) in INVITATION_LAND)
        default = "inaccessible" if t.get("landuse") == "construction" else "invitation"
        return [(f"p10_{rule(k, t[k], default)}", g, f"kapalı kullanım {k}={t[k]}")]
    if t.get("highway") in CAR_W and not area and t.get("access") not in ("private", "no"):
        hw = t["highway"]
        cw = width(t, CAR_W[hw])
        out = [("p12_car", g.buffer(cw / 2, cap_style="flat"), f"taşıt yolu {hw}, {cw:g} m")]
        sw = t.get("sidewalk", t.get("sidewalk:both", ""))
        side_w = WALK_W.get(hw, 0.0)
        if side_w and sw not in ("no", "none", "separate"):
            strip = g.buffer(cw / 2 + side_w, cap_style="flat").difference(g.buffer(cw / 2, cap_style="flat"))
            if sw in ("left", "right"):
                strip = strip.intersection(
                    g.buffer(cw / 2 + side_w, cap_style="flat", single_sided=True) if sw == "left" else
                    g.buffer(-(cw / 2 + side_w), cap_style="flat", single_sided=True))
            sides = "tek yan" if sw in ("left", "right") else "iki yan"
            out.append(("p11_open_public", strip,
                        f"kaldırım, {'etiketli' if sw else 'modellenmiş'}, {sides}, {side_w:g} m"))
        return out
    if area and t.get("landuse") == "residential":
        return [("p13_yard", g, "konut alanı, bina arası zemin")]                    # D03
    if area and t.get("aeroway") == "aerodrome":
        return [("p14_inaccessible", g, "havalimanı geri kalanı")]
    if area and any((k, t.get(k)) in NATURAL for k in ("landuse", "natural", "man_made")):   # D14, D15
        k = next(k for k in ("landuse", "natural", "man_made") if (k, t.get(k)) in NATURAL)
        r = OSM_RULE.get(f"{k}={t[k]}", NATURAL[(k, t[k])])
        if (k, t[k]) in (("landuse", "forest"), ("natural", "wood")) and g.area < URBAN_FOREST_M2:
            r = "open_public"                                        # D18: an urban grove (koru)
        cls = r if r in REGIMES else "natural"
        what = ("doğal / tarım alanı" if cls == "natural" else
                "kent korusu (50 ha altı)" if k in ("landuse", "natural") and t[k] in ("forest", "wood") else
                "çim alan")
        return [(f"p15_{cls}", g, f"{what} {k}={t[k]}")]
    return []


CONTROL_UNKNOWN = []


def site_index(classified):
    """Named sites with their regime, for buildings that stand inside them."""
    sites = [(g, key.split("_", 1)[1]) for key, g, _ in classified if key.startswith(("p01k_", "p02_", "p09_"))]
    return shapely.STRtree([g for g, _ in sites]), sites


# ---------------------------------------------------------------- D19

D19_NEAR_M = 25.0
D19_SIM = 0.85
D19_M2 = (500.0, 1_000_000.0)          # a site, not a building corner or a district
# a café, an ATM or a kiosk inside a site does not speak for the whole site
D19_PART = re.compile(r"atm|bank|cafe|caf.|coffee|restaurant|food|bar\b|pub|bakery|kiosk|store|shop|market|"
                      r"boutique|storage|media|home_service|office|b2b|manufactur|dealer|repair|automotive|"
                      r"salon|barber|pharmacy|dentist|doctor|clinic|gas|fuel|laundry|hotel", re.I)
_TR = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def name_key(s):
    s = str(s or "").replace("İ", "i").replace("I", "ı").lower().translate(_TR)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def site_names(shapes, evidence, names):
    """D19. A named OSM area that no rule reads takes the regime of the place
    records inside it (or within 25 m) that carry the same name — a converted
    site such as Çubuklu Silolar, whose OSM tags still say oil tanks. Votes as
    D08: most frequent regime, ties to the more public. The workbook's
    d19_adaylar sheet can set another regime for a site or turn it off."""
    from difflib import SequenceMatcher
    cand = [(el, t, g) for el, t, g, rules in shapes
            if not rules and t.get("name") and g.geom_type in ("Polygon", "MultiPolygon")
            and D19_M2[0] <= g.area <= D19_M2[1]]
    if not cand:
        return []
    tree = shapely.STRtree([g for *_, g in cand])
    votes = defaultdict(list)
    for (pt, reg, src, cat), nm in zip(evidence, names):
        if not reg or reg == "exclude" or not nm or D19_PART.search(cat or ""):
            continue
        a = name_key(nm)
        if len(a) < 6:
            continue
        for i in tree.query(pt, predicate="dwithin", distance=D19_NEAR_M):
            b = name_key(cand[i][1]["name"])
            if len(b) >= 6 and SequenceMatcher(None, a, b).ratio() >= D19_SIM:
                votes[i].append((reg, f"{nm} ({src}, {cat})"))
    rank = {r: k for k, r in enumerate(REGIMES)}
    out = []
    for i, v in votes.items():
        el, t, g = cand[i]
        c = Counter(r for r, _ in v)
        top = max(c.values())
        reg = min((r for r, n in c.items() if n == top), key=rank.get)
        review = RULES.get("d19", {}).get(t["name"])
        if review == "off":
            continue
        if review in REGIMES:
            reg = review
        out.append((f"p09_{reg}", g, f"D19 ad eşleşmesi: {t['name']} ← {v[0][1]}"
                                     + (" · gözden geçirildi" if review else "")))
    return out


# ---------------------------------------------------------------- evidence

_MATCHES = None


def matches():
    global _MATCHES
    if _MATCHES is None:
        _MATCHES = {r["fsq_place_id"]: r["overture_id"]
                    for r in csv.DictReader(MATCHES.open(encoding="utf-8"))}
    return _MATCHES


def load_evidence(bbox, loc):
    """D09. Place records that say what a ground floor is, cleaned and with
    each place counted once. Returns (evidence, tally, dropped, names):
        evidence  [(Point, regime or None, source, category)]
        dropped   [(lon, lat, category, source, reason)]
        names     the record names, in the order of evidence (for D19)"""
    tally, dropped = Counter(), []
    s, w, n, e = bbox
    min_conf = CLEAN["overture_min_conf"]
    ng_ov = set(CLEAN["non_ground_overture"])
    ov = {}
    for blob, gid, bc, tax, conf, oname in (row for f in OVT_PLACES_ALL for row in gpkg_in_box(
            f, bbox, ["id", "basic_cat", "taxonomy", "confidence", "names_pri"])):
        x, y = gpkg_point(blob)
        if not (w <= x <= e and s <= y <= n):
            continue
        if not bc:
            try:
                h = json.loads(tax).get("hierarchy") or []
            except Exception:
                h = []
            bc = f"(yok) {h[0] if h else '?'}"
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        reg = OV_DICT.get(bc)
        if conf < min_conf:
            reason = "Overture: düşük güven"
        elif bc in ng_ov or reg == "exclude":
            reason = "Overture: zemin dışı"
        else:
            reason = None
        if reason:
            tally[reason] += 1
            dropped.append((x, y, bc, "overture", reason))
            continue
        ov[gid] = [Point(loc.fwd(x, y)), reg, "overture", bc, oname]
        tally["Overture: kullanıldı"] += 1

    if not USE_FOURSQUARE:          # the province layer beyond Foursquare's extent, and its test
        recs = list(ov.values())
        return [tuple(v[:4]) for v in recs], tally, dropped, [v[4] for v in recs]
    m = matches()
    ng_fsq = set(CLEAN["non_ground_fsq"])
    stale = CLEAN.get("stale_before")
    con = sqlite3.connect(f"file:{FSQ_GPKG}?mode=ro", uri=True)
    fsq = []
    for pid, lon, lat, cat, closed, refreshed, fname in con.execute(
            f'SELECT place_id, longitude, latitude, cat_name, closed, refreshed, name FROM "{FSQ_TABLE}" '
            "WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?", (s, n, w, e)):
        reg = FSQ_DICT.get(cat)
        oid = m.get(pid)
        if closed and CLEAN["drop_closed"]:
            reason = "Foursquare: kapanmış"
        elif cat in ng_fsq or reg == "exclude":
            reason = "Foursquare: zemin dışı"
        elif oid in ov:
            # the same place as a kept Overture record: count it once, keeping
            # Overture's regime, or this one where Overture had none
            if ov[oid][1] is None and reg:
                ov[oid][1] = reg
            ov[oid][2] = "ikisi"
            reason = "Foursquare: Overture ile eşleşti, tek sayıldı"
        elif stale and (refreshed or "") < stale and not oid:
            reason = "Foursquare: eski, Overture'da karşılığı yok"
        else:
            reason = None
        if reason:
            tally[reason] += 1
            if not reason.endswith("tek sayıldı"):
                dropped.append((lon, lat, cat, "foursquare", reason))
            continue
        fsq.append((Point(loc.fwd(lon, lat)), reg, "foursquare", cat, fname))
        tally["Foursquare: kullanıldı"] += 1
    con.close()
    recs = list(ov.values()) + fsq
    return [tuple(v[:4]) for v in recs], tally, dropped, [v[4] for v in recs]


def microsoft_buildings(bbox, loc, osm_geoms):
    """Overture footprints from Microsoft's model, where OSM has no building."""
    tree = shapely.STRtree(osm_geoms) if osm_geoms else None
    out = []
    for blob, src in (row for f in OVT_BUILDINGS_ALL for row in gpkg_in_box(f, bbox, ["sources"])):
        if "OpenStreetMap" in str(src):
            continue
        g = loc.geom(gpkg_geom(blob))
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty or g.area < 10:
            continue
        if tree is not None:
            hits = tree.query(g, predicate="intersects")
            if len(hits) and sum(g.intersection(osm_geoms[i]).area for i in hits) > MS_OVERLAP * g.area:
                continue
        out.append(g)
    return out


def assign_evidence(building_geoms, evidence):
    """D04: each place record goes to the building it stands in, or to the
    nearest one within 10 m — records are often placed on the pavement outside."""
    tree = shapely.STRtree(building_geoms)
    votes = defaultdict(list)
    for pt, reg, src, cat in evidence:
        hits = tree.query(pt, predicate="within")
        if len(hits):
            j = min(hits, key=lambda i: building_geoms[i].area)
        else:
            near = tree.query_nearest(pt, max_distance=ATTACH_M)
            j = int(near[0]) if len(near) else None
        if j is not None:
            votes[int(j)].append((reg, src))
    return votes


def building_regime(t, g, votes, sites):
    """D05/D06/D08. A building's ground-floor regime and where it came from:
    worship rule; else the most frequent regime among its place records; else
    the named site it stands in; else its building tag; else unknown."""
    if is_open_worship(t):
        return "open_public", "ibadet yeri", 0
    regs = [r for r, _ in votes if r in REGIMES]
    if regs:
        c = Counter(regs)
        top = max(c.items(), key=lambda kv: (kv[1], SCORE[kv[0]]))[0]
        return top, "POI", len(regs)
    stree, slist = sites
    inside = [slist[i] for i in stree.query(g.representative_point(), predicate="within")]
    if inside:
        return min(inside, key=lambda s: s[0].area)[1], "alan", 0
    tag = t.get("building")
    if tag in BUILDING_TAG:
        return rule("building", tag, BUILDING_TAG[tag]), "etiket", 0
    return "unknown_building", "bilinmiyor", 0


# ---------------------------------------------------------------- OSM sources

HSTORE = re.compile(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"')


def parse_hstore(s):
    """GDAL's all_tags field: "k"=>"v","k2"=>"v2" with backslash escapes."""
    if not s:
        return {}
    un = lambda x: x.replace('\\"', '"').replace("\\\\", "\\")
    return {un(k): un(v) for k, v in HSTORE.findall(s)}


def osm_from_overpass(path, loc):
    """(element, tags, local geometry) from an Overpass 'out geom' JSON."""
    out = []
    for el in json.loads(Path(path).read_text(encoding="utf-8"))["elements"]:
        t = el.get("tags", {})
        if not t or el["type"] == "node" or landform(t):
            continue
        g = way_shape(el, loc) if el["type"] == "way" else relation_shape(el, loc)
        if g is not None and not g.is_empty:
            out.append((el, t, g))
    return out


def osm_from_gpkg(path, bbox, loc):
    """The same, from the GeoPackage cut out of the Geofabrik extract. Ways come
    as lines, closed or not; is_area() decides which are areas, exactly as for
    Overpass data. Relations come as multipolygons."""
    out = []
    for layer, kind in (("lines", "way"), ("multipolygons", "relation")):
        for blob, oid, name, tags in gpkg_in_box(path, bbox, ["osm_id", "name", "all_tags"], table=layer):
            t = parse_hstore(tags)
            if name and "name" not in t:
                t["name"] = name
            if not t or landform(t):
                continue
            g = loc.geom(gpkg_geom(blob))
            if kind == "way":
                cs = list(g.coords)
                if len(cs) >= 4 and cs[0] == cs[-1] and is_area(t):
                    g = Polygon(cs)
            if not g.is_valid:
                g = g.buffer(0) if g.geom_type in ("Polygon", "MultiPolygon") else g
            if not g.is_empty:
                out.append(({"type": kind, "id": int(oid) if oid else 0}, t, g))
    return out


# ---------------------------------------------------------------- preparation

USE_FOURSQUARE = True       # False: Overture alone, as beyond Foursquare's extent
CORRECTIONS = HERE / "duzeltme_alanlari.gpkg"
_LABEL = {"açık-kamusal": "open_public", "yarı-kamusal": "quasi_public", "biletli": "ticketed",
          "açık-özel": "open_private", "davetli": "invitation", "erişimsiz": "inaccessible"}


def drawn_corrections(bbox, loc):
    """Polygons drawn by hand with their real regime, a source and a date."""
    if not CORRECTIONS.exists():
        return []
    out = []
    for blob, reg, src, date in gpkg_in_box(CORRECTIONS, bbox, ["rejim", "kaynak", "tarih"], table="duzeltme"):
        reg = _LABEL.get(str(reg or "").strip(), reg)
        if reg in REGIMES:
            g = loc.geom(gpkg_geom(blob))
            out.append((f"p01k_{reg}", g if g.is_valid else g.buffer(0), f"çizilmiş düzeltme: {src or '?'}, {date or '?'}"))
    return out


def prepare(name):
    """A named box: a test box from its Overpass download, or a close-reading
    sample from the citywide GeoPackage."""
    D = (prepare_box(AREAS[name], HERE / f"osm_{name}.json") if name in AREAS else
         prepare_box(SAMPLES[name], OSM_GPKG))
    D["name"] = name
    return D


def prepare_box(bbox, source):
    """Everything the painting and the QGIS export share: shapes and the rules
    applied to them, buildings with their regimes, the evidence and what was
    dropped from it, and the lines that make barriers. `source` is an Overpass
    JSON or the citywide OSM GeoPackage."""
    CONTROL_UNKNOWN.clear()
    s, w, n, e = bbox
    loc = Local((s + n) / 2, (w + e) / 2)
    frame = box(*loc.fwd(w, s), *loc.fwd(e, n))

    raw = (osm_from_gpkg(source, bbox, loc) if str(source).endswith(".gpkg")
           else osm_from_overpass(source, loc))
    shapes = [(el, t, g, classify(t, g)) for el, t, g in raw]

    classified = [r for *_, rules in shapes for r in rules if r[0] != "building"]
    # hand corrections (workbook sheet 'duzeltmeler'), painted above everything but water
    fixes = RULES.get("duzeltme", {})
    if fixes:
        for el, t, g, _ in shapes:
            key = next((k for k in (f"{el['type'][0]}{el['id']}", t.get("name")) if k and k in fixes), None)
            if key and g.geom_type in ("Polygon", "MultiPolygon"):
                classified.append((f"p01k_{fixes[key]}", g, f"elle düzeltme: {key}"))
    # hand-drawn corrections (duzeltme_alanlari.gpkg): ground the rules cannot know
    classified += drawn_corrections(bbox, loc)
    sites = site_index(classified)
    buildings = [(el, t, r[1]) for el, t, _, rules in shapes for r in rules if r[0] == "building"]
    ms = microsoft_buildings(bbox, loc, [g for *_, g in buildings])
    buildings += [(None, {"building": "yes", "source": "Microsoft ML"}, g) for g in ms]

    evidence, tally, dropped, names = load_evidence(bbox, loc)
    d19 = site_names(shapes, evidence, names)                                       # D19
    if d19:
        classified += d19
        sites = site_index(classified)
    votes = assign_evidence([g for *_, g in buildings], evidence)
    b_regimes = [building_regime(t, g, votes.get(i, []), sites) for i, (_, t, g) in enumerate(buildings)]

    barrier = {"major": [], "rail": [], "passes": []}
    for el, t, g, _ in shapes:
        if g.geom_type != "LineString":
            continue
        # footbridges and underpasses: pedestrian ways off the ground level
        if t.get("highway") in PED_W and (t.get("bridge") not in (None, "no") or
                                          t.get("tunnel") not in (None, "no")):
            barrier["passes"].append(g)
            continue
        if underground(t) or t.get("bridge") not in (None, "no"):
            continue                    # a viaduct can be walked under; it is not a barrier
        if t.get("highway") in MAJOR_ROADS:
            barrier["major"].append((g, width(t, CAR_W[t["highway"]])))
        elif t.get("railway") in RAIL:
            barrier["rail"].append((g, 8.0))

    return {"name": None, "bbox": bbox, "loc": loc, "frame": frame, "shapes": shapes,
            "classified": classified, "buildings": buildings, "b_regimes": b_regimes,
            "n_microsoft": len(ms), "evidence": evidence, "tally": tally, "dropped": dropped,
            "votes": votes, "barrier": barrier, "control_unknown": list(CONTROL_UNKNOWN), "d19": d19}


def crossings(bbox, loc):
    """Pedestrian crossing points and the midpoints of crossing ways."""
    out = []
    for layer in ("crossings_points", "crossings_lines"):
        try:
            for (blob,) in gpkg_in_box(PEDESTRIAN, bbox, [], table=layer):
                g = loc.geom(gpkg_geom(blob))
                out.append(g if g.geom_type == "Point" else g.interpolate(0.5, normalized=True))
        except sqlite3.Error:
            pass
    return out


# D12: crossing records closer than this are one crossing. At 6 m half of central
# Kadıköy's records merge — a crossing drawn both as a node and as a way — and
# beyond it there is no break: larger distances start merging the separate legs
# of a junction (20 m turned 39 records into 6). Crossings on the two halves of a
# dual carriageway can still count twice; telling them apart needs lane geometry.
MERGE_M = 6.0


def merge_points(points):
    """One point per crossing: records within MERGE_M of each other merge."""
    if not points:
        return []
    blobs = unary_union([p.buffer(MERGE_M / 2) for p in points])
    return [part.centroid for part in shapely.get_parts(blobs)]


# ---------------------------------------------------------------- scoring

def score(sh, sc):
    tot = sum(sh.get(r, 0.0) for r in REGIMES)
    return sum(sc[r] * sh.get(r, 0.0) for r in REGIMES) / tot if tot else float("nan")


def cell_estimate(share, ground):
    """Point estimate with its two uncertainty ranges and a confidence grade.
    `share` is regime -> fraction of the cell; yards fold into invitation and
    the modelled coastal strip (D21) into open-public."""
    known = {r: share.get(r, 0.0) for r in REGIMES}
    known["invitation"] += share.get("yard", 0.0)
    known["open_public"] += share.get("coast", 0.0)
    K = sum(known.values())
    u = share.get("unknown_building", 0.0) + share.get("unmapped", 0.0)
    S = sum(SCORE[r] * known[r] for r in REGIMES)
    P = S / K if K else float("nan")
    lo, hi = (S / (K + u), (S + u) / (K + u)) if K + u else (float("nan"),) * 2
    variants = {k: score(known, sc) for k, sc in SENS.items()}
    with_car = dict(known); with_car["inaccessible"] += share.get("car", 0.0)
    variants["car_inacc"] = score(with_car, SCORE)
    with_bld = dict(known); with_bld["invitation"] += share.get("unknown_building", 0.0)
    variants["bld_konut"] = score(with_bld, SCORE)
    vals = [P] + [v for v in variants.values() if v == v]
    rlo, rhi = min(vals), max(vals)
    grade = ("A" if hi - lo < .2 and rhi - rlo < .1 else
             "B" if hi - lo < .4 and rhi - rlo < .2 else "C")
    return P, lo, hi, rlo, rhi, grade, variants, K


def paint(D):
    """Paint the box ground in priority order: each layer takes only what is
    still free. Returns (class -> geometry, building-source tally)."""
    loc, frame = D["loc"], D["frame"]
    layers = defaultdict(list)
    sea = load_sea(D["bbox"], loc, frame)
    if sea is not None and not sea.is_empty:
        layers["p00_water"].append(sea)
    for key, g, _ in D["classified"]:
        layers[key].append(g)
    b_source = Counter()
    for (_, t, g), (regime, source, _) in zip(D["buildings"], D["b_regimes"]):
        layers[f"p05_{regime}"].append(g)
        b_source[source + (" (Microsoft)" if t.get("source") == "Microsoft ML" else "")] += 1

    # D20: OSM draws military land loosely; a park, mosque, cemetery or square
    # mapped inside it is a public enclave and is painted before the military
    # land around it (Hz. Yuşa Tepesi inside the Bosphorus Command)
    mil = [g for k in layers if k.startswith("p02_") for g in layers[k]]
    if mil:
        mil_tree = shapely.STRtree(mil)
        for key in ("p05_open_public", "p08_open_public", "p09_open_public"):
            layers["p01m_open_public"] += [g for g in layers.get(key, [])
                                           if len(mil_tree.query(g, predicate="intersects"))]

    # D21: the first 30 m of land from the sea shore is public by the Coastal Law
    # (Kıyı Kanunu 3621); where OSM leaves it unmapped it is painted open-public,
    # as its own class so the map shows it is modelled. Painted last, it takes
    # only ground no other rule reads. The Bosphorus is left out: unmapped ground
    # there is as likely a yalı garden as a promenade.
    if sea is not None and not sea.is_empty:
        strip = robust(shapely.difference, sea.buffer(COAST_M), sea)
        bos = loc.geom(BOSPHORUS)
        strip = robust(shapely.difference, strip, bos) if strip.intersects(bos) else strip
        layers["p16_coast"].append(strip)

    painted, taken = {}, Polygon()
    for key in sorted(layers):
        geom = robust(unary_union, [g for g in layers[key] if g is not None and not g.is_empty])
        geom = robust(shapely.difference, robust(shapely.intersection, geom, frame), taken)
        geom = robust(unary_union, [p for p in shapely.get_parts(geom) if p.geom_type in ("Polygon", "MultiPolygon")])
        if geom.is_empty:
            continue
        cls = key.split("_", 1)[1]
        painted[cls] = robust(unary_union, [painted[cls], geom]) if cls in painted else geom
        taken = robust(unary_union, [taken, geom])
    painted["unmapped"] = robust(shapely.difference, frame, taken)
    return painted, b_source


def robust(op, *args):
    """A shapely overlay that survives GEOS topology errors: on failure the
    inputs are repaired (make_valid, polygons only), then if need be snapped to
    a 1 cm grid. Only a failing operation is touched, so every box that worked
    before gives the same result."""
    def fix(a, grid=None):
        if isinstance(a, list):
            return [fix(g, grid) for g in a]
        g = shapely.make_valid(a)
        g = unary_union([p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon")])
        return shapely.set_precision(g, grid) if grid else g
    try:
        return op(*args)
    except shapely.errors.GEOSException:
        pass
    try:
        return op(*fix(list(args)))
    except shapely.errors.GEOSException:
        return op(*fix(list(args), 0.01))


def barrier_index(D):
    """D12 geometry: barrier footprint, deduplicated crossings and passes."""
    blines = [g for g, _ in D["barrier"]["major"]] + [g for g, _ in D["barrier"]["rail"]]
    bpoly = unary_union([g.buffer(wd / 2, cap_style="flat")
                         for g, wd in D["barrier"]["major"] + D["barrier"]["rail"]]) if blines else Polygon()
    btree = shapely.STRtree(blines) if blines else None
    xings = merge_points([p for p in crossings(D["bbox"], D["loc"]) if btree is not None and
                          len(btree.query(p, predicate="dwithin", distance=CROSSING_M))])
    # a footbridge or underpass counts once, however many carriageways it spans
    bline_union = unary_union(blines) if blines else None
    passes = merge_points([g.intersection(bline_union).centroid for g in D["barrier"]["passes"]
                           if bline_union is not None and g.intersects(bline_union)])
    return {"bpoly": bpoly, "xtree": shapely.STRtree(xings) if xings else None,
            "ptree": shapely.STRtree(passes) if passes else None}


def cell_row(D, painted, B, cell):
    """The output row for one cell, or None if less than a quarter of it is land."""
    loc = D["loc"]
    cg = loc.geom(cell["geom"])
    ca = cg.area
    share = {cls: painted[cls].intersection(cg).area / ca for cls in painted}
    ground = 1.0 - share.pop("water", 0.0)
    if ground < 0.25:
        return None
    sh = {k: v / ground for k, v in share.items()}
    P, lo, hi, rlo, rhi, grade, var, K = cell_estimate(sh, ground)

    major_m = sum(g.intersection(cg).length for g, _ in D["barrier"]["major"])
    rail_m = sum(g.intersection(cg).length for g, _ in D["barrier"]["rail"])
    land = cg.difference(painted.get("water", Polygon()))
    rest = land.difference(B["bpoly"]) if not B["bpoly"].is_empty else land
    pieces = sum(1 for p in shapely.get_parts(rest) if p.area >= MIN_PIECE_M2)
    n_level = len(B["xtree"].query(cg, predicate="contains")) if B["xtree"] is not None else 0
    n_pass = len(B["ptree"].query(cg, predicate="contains")) if B["ptree"] is not None else 0
    nx = n_level + n_pass
    blen_km = (major_m + rail_m) / 1000
    lon, lat = loc.inv(cg.centroid.x, cg.centroid.y)
    rnd = lambda v: round(v, 3) if v == v else None
    return {
        "cell_id": cell["id"], "lon": round(lon, 5), "lat": round(lat, 5),
        "old_pct": cell.get("old_pct"), "old_n": cell.get("old_n"),
        "P_ground": rnd(P), "P_lo": rnd(lo), "P_hi": rnd(hi),
        "P_rule_lo": rnd(rlo), "P_rule_hi": rnd(rhi), "guven": grade,
        "P_yedide_bir": rnd(var["yedide_bir"]), "P_a07": rnd(var["alpha_0.7"]),
        "P_a03": rnd(var["alpha_0.3"]), "P_car_inacc": rnd(var["car_inacc"]),
        "P_ground_bld": rnd(var["bld_konut"]),
        **{f"sh_{r}": round(sh.get(r, 0.0), 3) for r in REGIMES},
        "sh_yard": round(sh.get("yard", 0.0), 3),
        "sh_coast": round(sh.get("coast", 0.0), 3),
        "sh_car": round(sh.get("car", 0.0), 3),
        "sh_natural": round(sh.get("natural", 0.0), 3),
        "sh_unknown_building": round(sh.get("unknown_building", 0.0), 3),
        "sh_unmapped": round(sh.get("unmapped", 0.0), 3),
        "scored_share": round(K, 3),
        "gosterim": "puan" if K >= MIN_SCORED else "kanıt yetersiz",
        "bar_major_m": round(major_m), "bar_rail_m": round(rail_m), "bar_pieces": pieces,
        "bar_crossings": nx, "bar_level": n_level, "bar_passes": n_pass,
        "bar_cross_per_km": round(nx / blen_km, 1) if blen_km > 0.05 else None,
    }


def compute_box(D, cells, outdir, prefix, label, verbose=True, write_layers=True):
    """Paint a prepared box and score the given cells. Writes
    <prefix>_cells.csv, _layers.geojson, _kontrol_bilinmeyen.csv, _run.json."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    painted, b_source = paint(D)
    B = barrier_index(D)
    out = [row for row in (cell_row(D, painted, B, c) for c in cells) if row]

    path = outdir / f"{prefix}_cells.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        if out:
            wr = csv.DictWriter(fh, fieldnames=list(out[0]))
            wr.writeheader()
            wr.writerows(out)
    if write_layers:
        loc = D["loc"]
        feats = [{"type": "Feature", "properties": {"cls": cls}, "geometry": mapping(loc.back(geom))}
                 for cls, geom in painted.items() if not geom.is_empty]
        (outdir / f"{prefix}_layers.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    with (outdir / f"{prefix}_kontrol_bilinmeyen.csv").open("w", newline="", encoding="utf-8") as fh:
        cols = ["name", "tourism", "amenity", "leisure", "fee", "operator", "website", "KARAR (kamu/özel)"]
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        for row in D["control_unknown"]:
            wr.writerow({**row, "KARAR (kamu/özel)": ""})

    tally = D["tally"]
    run = {"box": label, "bbox": D["bbox"], "rules_source": RULES["source"], "rules_sha256": RULES["sha256"],
           "rules_compiled": RULES["compiled"], "scores": SCORE,
           "corrections_sha256": (__import__("hashlib").sha256(CORRECTIONS.read_bytes()).hexdigest()
                                  if CORRECTIONS.exists() else None),
           "d19_sites": [n for *_, n in D.get("d19", [])],
           "place_sources": "Foursquare + Overture" if USE_FOURSQUARE else "Overture only",
           "evidence": dict(tally), "buildings": dict(b_source), "microsoft_added": D["n_microsoft"],
           "control_unknown_sites": len(D["control_unknown"]), "cells": len(out),
           "grades": dict(Counter(r["guven"] for r in out))}
    (outdir / f"{prefix}_run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")

    if verbose:
        print(f"kurallar: {RULES['source']} · sha256 {RULES['sha256'][:12]} · {RULES['compiled']}")
        print("kanıt: " + ", ".join(f"{k} {v:,}" for k, v in sorted(tally.items())))
        print(f"bina {len(D['buildings']):,} (Microsoft eklenen {D['n_microsoft']:,}) · " +
              ", ".join(f"{k} {v:,}" for k, v in b_source.most_common()))
        print(f"kontrolü bilinmeyen ücretsiz alan: {len(D['control_unknown'])}")
        tot = D["frame"].area
        print("\nkutunun zemin bileşimi:")
        for cls, geom in sorted(painted.items(), key=lambda kv: -kv[1].area):
            print(f"   {cls:<18}{geom.area / 1e6:>7.2f} km²  %{geom.area / tot * 100:5.1f}")
        print(f"\n{len(out)} hücre · güven {dict(Counter(r['guven'] for r in out))} · yazıldı: {path.name}")
    return out


def main(name):
    """A box: the cells that fall wholly inside it — the old estimate's cells
    for the test boxes, the citywide grid's for the samples."""
    D = prepare(name)
    frame, loc = D["frame"], D["loc"]
    cells = []
    inside = lambda g: (lambda cg: cg.intersects(frame) and cg.intersection(frame).area >= 0.99 * cg.area)(loc.geom(g))
    if name in AREAS:
        for f in json.loads((MAP / "cells_estimate.geojson").read_text(encoding="utf-8"))["features"]:
            g = shapely.geometry.shape(f["geometry"])
            if inside(g):
                c = g.centroid
                cells.append({"id": f"{c.x:.5f}_{c.y:.5f}", "geom": g,
                              "old_pct": round(float(f["properties"]["pub"]), 3),
                              "old_n": f["properties"].get("n")})
    else:
        for f in json.loads((HERE / "grid_foursquare.geojson").read_text(encoding="utf-8"))["features"]:
            g = shapely.geometry.shape(f["geometry"])
            if inside(g):
                p = f["properties"]
                cells.append({"id": p["id"], "geom": g, "old_pct": p["old_pct"], "old_n": p["old_n"]})
    compute_box(D, cells, HERE, f"ground_{name}", name)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ataturk")
