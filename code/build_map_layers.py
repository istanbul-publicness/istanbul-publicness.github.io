"""
The interactive map's two layers, from the citywide run.

    harita/data/cells.geojson   one 500 m cell per row: the score, where it sits in
    harita/data/cells.csv       the city (percentile), both uncertainty ranges, the
                                grade, whether it is shown (D16), its ground shares,
                                the barrier measures; the CSV also keeps the five
                                alternative-rule scores
    harita/tiles/ground.pmtiles      the painted ground plane as vector tiles, z14–15,
    harita/tiles/ground_far.pmtiles  and simplified for z12–13; class in `r`
    <tiles>/harita/zemin.gpkg   the same ground plane in full, for download

Every tile of the run painted its ground with a 150 m margin, so neighbouring
tiles overlap. Here each tile keeps only its own block of the lattice (8 x 8
cells), which joins the pieces edge to edge without double drawing. Water is
left out; the basemap draws it.

Percentile: among cells shown with a score, the share whose P is lower — 'more
public than X % of the city'. Cells marked 'kanıt yetersiz' keep their P in the
table but get no percentile.

Run:  py build_map_layers.py               (cells and ground, both layers)
      py build_map_layers.py hucre         (cells only)
      py build_map_layers.py zemin [il_ek] (ground of one or both layers)
      py build_map_layers.py karo          (vector tiles from existing GeoPackages)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import shapely
from shapely.geometry import box, mapping, shape

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ground_plane as gp  # noqa: E402
import run_city  # noqa: E402

# the map's two layers: the Foursquare extent, with both place sources, and the rest of
# the province, where only Overture's places exist
LAYERS = {
    "foursquare": {"kanit": "Foursquare + Overture", "cells": "cells.geojson", "gpkg": "zemin.gpkg",
                   "tiles": ("ground.pmtiles", "ground_far.pmtiles")},
    "il_ek": {"kanit": "yalnız Overture", "cells": "cells_il.geojson", "gpkg": "zemin_il.gpkg",
              "tiles": ("ground_il.pmtiles", "ground_il_far.pmtiles")},
}
EXTENT = "foursquare"            # the lattice is read from this grid
OUT = HERE / "harita"
GROUND_DIR = run_city.TILES / "harita"
QGIS = run_city.P["qgis"]
CODE = {"open_public": "op", "quasi_public": "qp", "ticketed": "ti", "open_private": "pr",
        "invitation": "in", "inaccessible": "na", "yard": "ya", "coast": "ks", "car": "ca", "natural": "do",
        "unknown_building": "ub", "unmapped": "um"}
KEEP = ["P_ground", "P_lo", "P_hi", "P_rule_lo", "P_rule_hi", "guven", "gosterim", "scored_share",
        "sh_open_public", "sh_quasi_public", "sh_ticketed", "sh_open_private", "sh_invitation",
        "sh_inaccessible", "sh_yard", "sh_coast", "sh_car", "sh_natural", "sh_unknown_building", "sh_unmapped",
        "bar_major_m", "bar_rail_m", "bar_pieces", "bar_level", "bar_passes", "kanit"]


def env():
    return dict(os.environ, GDAL_DATA=str(QGIS / "apps" / "gdal" / "share" / "gdal"),
                PROJ_LIB=str(QGIS / "share" / "proj"), PROJ_DATA=str(QGIS / "share" / "proj"),
                PATH=str(QGIS / "bin") + os.pathsep + os.environ.get("PATH", ""))


def cells():
    """One table for the whole province, the percentile ranked across all of it
    (one scale: the same P has the same percentile on either side of the
    Foursquare extent's edge); one GeoJSON per layer for the map."""
    import numpy as np
    parts = []
    for ext, L in LAYERS.items():
        f = HERE / f"ground_city_{ext}_cells.csv"
        if f.exists():
            parts.append(pd.read_csv(f).assign(kanit=L["kanit"], katman=ext))
    d = pd.concat(parts, ignore_index=True)
    shown = d[(d.gosterim == "puan") & d.P_ground.notna()].P_ground.sort_values().to_numpy()
    d["P_yuzdelik"] = [round(float(np.searchsorted(shown, p, side="left")) / len(shown) * 100, 1)
                       if g == "puan" and p == p else None for p, g in zip(d.P_ground, d.gosterim)]
    (OUT / "data").mkdir(parents=True, exist_ok=True)
    for ext, L in LAYERS.items():
        part = d[d.katman == ext]
        if part.empty:
            continue
        grid = {f["properties"]["id"]: f["geometry"] for f in
                json.loads((HERE / f"grid_{ext}.geojson").read_text(encoding="utf-8"))["features"]}
        feats = []
        for r in part.to_dict("records"):
            props = {"id": r["cell_id"]}
            props.update({k: (None if isinstance(r[k], float) and r[k] != r[k] else r[k])
                          for k in ["P_yuzdelik"] + KEEP})
            feats.append({"type": "Feature", "geometry": grid[r["cell_id"]], "properties": props})
        path = OUT / "data" / L["cells"]
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False), encoding="utf-8")
        s = part[part.gosterim == "puan"]
        print(f"{L['cells']}: {len(feats):,} hücre ({len(s):,} puanlı) · {path.stat().st_size / 1048576:.1f} MB · "
              f"P medyan {s.P_ground.median():.2f} -> yüzdelik medyan %{s.P_yuzdelik.median():.0f}")
    # the downloadable table: every computed column, the old point-count estimate left out
    d.drop(columns=["old_pct", "old_n", "katman"]).to_csv(OUT / "data" / "cells.csv", index=False, encoding="utf-8")


def ground(ext="foursquare"):
    """The painted ground of one layer, each tile clipped to its own block of the
    lattice. Beyond the Foursquare extent a block can be shared with a
    Foursquare tile; there the province tile keeps only its own cells' ground."""
    L = LAYERS[ext]
    x0, y0, dx, dy = lattice()
    T = run_city.TILE
    src = run_city.TILES / ext
    taken = None
    if ext != "foursquare":
        fs = [shape(f["geometry"]) for f in
              json.loads((HERE / "grid_foursquare.geojson").read_text(encoding="utf-8"))["features"]]
        taken = shapely.STRtree(fs), fs
    GROUND_DIR.mkdir(parents=True, exist_ok=True)
    gpkg = GROUND_DIR / L["gpkg"]           # also the downloadable ground layer
    gpkg.unlink(missing_ok=True)
    ogr = str(QGIS / "bin" / "ogr2ogr.exe")
    tmp = GROUND_DIR / "_parca.geojson"
    t0, total = time.time(), 0
    files = sorted(src.glob("t*_layers.geojson"))
    # one tile at a time into the GeoPackage: the whole city at once would not fit in memory
    for k, f in enumerate(files):
        ti, tj = map(int, f.name[1:].split("_")[:2])
        own = box(x0 + ti * T * dx, y0 + tj * T * dy, x0 + (ti + 1) * T * dx, y0 + (tj + 1) * T * dy)
        if taken is not None:
            hit = [taken[1][i] for i in taken[0].query(own, predicate="intersects")]
            if hit:
                own = gp.robust(shapely.difference, own, shapely.union_all(hit))
        feats = []
        for ft in json.loads(f.read_text(encoding="utf-8"))["features"]:
            cls = ft["properties"]["cls"]
            if cls not in CODE:
                continue
            g = gp.robust(shapely.intersection, shape(ft["geometry"]), own)
            for p in shapely.get_parts(g):
                if p.geom_type == "Polygon" and p.area > 1e-10:          # ~1 m2, slivers out
                    feats.append({"type": "Feature", "geometry": mapping(p), "properties": {"r": CODE[cls]}})
        if not feats:
            continue
        tmp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")),
                       encoding="utf-8")
        cmd = [ogr, "-f", "GPKG", str(gpkg), str(tmp), "-nln", "zemin", "-nlt", "POLYGON", "-a_srs", "EPSG:4326"]
        if gpkg.exists():
            cmd[1:1] = ["-append"]
        subprocess.run(cmd, env=env(), check=True, capture_output=True)
        total += len(feats)
        if (k + 1) % 40 == 0:
            print(f"   {k + 1}/{len(files)} parça · {total:,} poligon · {time.time() - t0:.0f} sn", flush=True)
    tmp.unlink(missing_ok=True)
    print(f"{L['gpkg']}: {total:,} poligon · {gpkg.stat().st_size / 1048576:.0f} MB · {time.time() - t0:.0f} sn")
    tiles(ext)


def tiles(ext="foursquare"):
    """Two archives per layer, each under GitHub Pages' 100 MB per file: z14-15
    in full detail (~1 m; the map overzooms z15; adding z16 made the city's
    138 MB), and z12-13 simplified, so the ground shows from the district scale."""
    L = LAYERS[ext]
    gpkg = GROUND_DIR / L["gpkg"]
    ogr = str(QGIS / "bin" / "ogr2ogr.exe")
    (OUT / "tiles").mkdir(parents=True, exist_ok=True)
    for name, zmin, zmax, extra in ((L["tiles"][0], 14, 15, []),
                                    (L["tiles"][1], 12, 13, ["-dsco", "SIMPLIFICATION=2"])):
        pm = OUT / "tiles" / name
        pm.unlink(missing_ok=True)
        t1 = time.time()
        r = subprocess.run([ogr, "-f", "PMTiles", str(pm), str(gpkg), "zemin", "-nln", "zemin",
                            "-dsco", f"MINZOOM={zmin}", "-dsco", f"MAXZOOM={zmax}",
                            "-dsco", "MAX_SIZE=1500000", *extra], env=env(), capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-800:])
        print(f"{name} (z{zmin}-{zmax}) {pm.stat().st_size / 1048576:.0f} MB · {time.time() - t1:.0f} sn")


def lattice():
    """Origin and step of the grid, read back from the grid file itself."""
    f = json.loads((HERE / f"grid_{EXTENT}.geojson").read_text(encoding="utf-8"))["features"][0]
    x_min, y_min, x_max, y_max = shape(f["geometry"]).bounds
    dx, dy = x_max - x_min, y_max - y_min
    return x_min - f["properties"]["i"] * dx, y_min - f["properties"]["j"] * dy, dx, dy


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    exts = [e for e in LAYERS if (run_city.TILES / e).exists()]
    if arg == "karo":
        for e in exts:
            tiles(e)
    elif arg == "zemin":
        for e in (sys.argv[2:] or exts):
            ground(e)
    else:
        cells()
        if arg != "hucre":
            for e in exts:
                ground(e)
