"""
The 500 m grid for the citywide ground-plane estimate.

Same lattice as the old estimate's cells, so the two can still be compared cell
by cell, but no longer limited to them: the old surface kept only cells with at
least 12 venues and some prior coverage, which the ground plane does not need.
Every cell at least a quarter on land is kept; cells on the coast are scored on
their land part (the computation divides by land area).

Extents
    foursquare   28.79–29.27 E, 40.84–41.29 N   first pass, both place sources
    il           the province's extent            later, lower-evidence layer

Output   grid_<extent>.geojson — id, lattice i/j, land share, old percentile

Run:  py make_grid.py foursquare
"""

import json
import math
import sys
from pathlib import Path

import shapely
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ground_plane as gp  # noqa: E402

EXTENTS = {"foursquare": (28.79, 40.84, 29.27, 41.29),     # west, south, east, north
           "il": (27.95, 40.78, 29.97, 41.60)}
MIN_LAND = 0.25


def lattice():
    """Origin and step of the old estimate's grid, read from its cells."""
    feats = json.loads((gp.MAP / "cells_estimate.geojson").read_text(encoding="utf-8"))["features"]
    b = [shape(f["geometry"]).bounds for f in feats]
    dx = sorted(x1 - x0 for x0, _, x1, _ in b)[len(b) // 2]
    dy = sorted(y1 - y0 for _, y0, _, y1 in b)[len(b) // 2]
    x0 = min(x for x, *_ in b)
    y0 = min(y for _, y, *_ in b)
    old = {}
    for f, (bx, by, _, _) in zip(feats, b):
        i, j = round((bx - x0) / dx), round((by - y0) / dy)
        old[(i, j)] = (round(float(f["properties"]["pub"]), 3), f["properties"].get("n"))
    return x0, y0, dx, dy, old


def main(extent):
    w, s, e, n = EXTENTS[extent]
    x0, y0, dx, dy, old = lattice()
    loc = gp.Local((s + n) / 2, (w + e) / 2)
    land = [gp.gpkg_geom(b) for (b,) in gp.gpkg_in_box(gp.LAND, (s, w, n, e), [])]
    land = unary_union([g.intersection(box(w - .01, s - .01, e + .01, n + .01)) for g in land])
    shapely.prepare(land)

    i0, i1 = math.floor((w - x0) / dx), math.ceil((e - x0) / dx)
    j0, j1 = math.floor((s - y0) / dy), math.ceil((n - y0) / dy)
    feats, kept_old = [], 0
    for i in range(i0, i1):
        for j in range(j0, j1):
            cell = box(x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy)
            if not land.intersects(cell):
                continue
            share = loc.geom(land.intersection(cell)).area / loc.geom(cell).area
            if share < MIN_LAND:
                continue
            o = old.get((i, j), (None, None))
            kept_old += o[0] is not None
            feats.append({"type": "Feature", "geometry": mapping(cell),
                          "properties": {"id": f"c{i}_{j}", "i": i, "j": j, "land": round(share, 3),
                                         "old_pct": o[0], "old_n": o[1]}})
    out = HERE / f"grid_{extent}.geojson"
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    print(f"ızgara {extent}: {len(feats):,} hücre (en az %{MIN_LAND * 100:.0f} kara) · "
          f"eski tahmini olan {kept_old:,} · adım {dx:.5f}° × {dy:.5f}° · yazıldı: {out.name}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "foursquare")
