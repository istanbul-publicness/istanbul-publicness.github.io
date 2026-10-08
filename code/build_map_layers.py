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

Run:  py build_map_layers.py            (cells and ground)
      py build_map_layers.py hucre      (cells only)
      py build_map_layers.py karo       (vector tiles from an existing zemin.gpkg)
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

EXTENT = "foursquare"
OUT = HERE / "harita"
GROUND_DIR = run_city.TILES / "harita"
QGIS = run_city.P["qgis"]
CODE = {"open_public": "op", "quasi_public": "qp", "ticketed": "ti", "open_private": "pr",
        "invitation": "in", "inaccessible": "na", "yard": "ya", "car": "ca", "natural": "do",
        "unknown_building": "ub", "unmapped": "um"}
KEEP = ["P_ground", "P_lo", "P_hi", "P_rule_lo", "P_rule_hi", "guven", "gosterim", "scored_share",
        "sh_open_public", "sh_quasi_public", "sh_ticketed", "sh_open_private", "sh_invitation",
        "sh_inaccessible", "sh_yard", "sh_car", "sh_natural", "sh_unknown_building", "sh_unmapped",
        "bar_major_m", "bar_rail_m", "bar_pieces", "bar_level", "bar_passes"]


def cells():
    d = pd.read_csv(HERE / f"ground_city_{EXTENT}_cells.csv")
    shown = d[(d.gosterim == "puan") & d.P_ground.notna()].P_ground.sort_values().to_numpy()
    import numpy as np
    d["P_yuzdelik"] = [round(float(np.searchsorted(shown, p, side="left")) / len(shown) * 100, 1)
                       if g == "puan" and p == p else None for p, g in zip(d.P_ground, d.gosterim)]
    grid = {f["properties"]["id"]: f["geometry"] for f in
            json.loads((HERE / f"grid_{EXTENT}.geojson").read_text(encoding="utf-8"))["features"]}
    feats = []
    for r in d.to_dict("records"):
        props = {"id": r["cell_id"]}
        props.update({k: (None if isinstance(r[k], float) and r[k] != r[k] else r[k])
                      for k in ["P_yuzdelik"] + KEEP})
        feats.append({"type": "Feature", "geometry": grid[r["cell_id"]], "properties": props})
    (OUT / "data").mkdir(parents=True, exist_ok=True)
    path = OUT / "data" / "cells.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False), encoding="utf-8")
    # the downloadable table: every computed column, the old point-count estimate left out
    d.drop(columns=["old_pct", "old_n"]).to_csv(OUT / "data" / "cells.csv", index=False, encoding="utf-8")
    s = d[d.gosterim == "puan"]
    print(f"hücre katmanı: {len(feats):,} hücre ({len(s):,} puanlı) · {path.stat().st_size / 1048576:.1f} MB · "
          f"yüzdelik örnek: P {s.P_ground.quantile(.5):.2f} → %{s.P_yuzdelik.quantile(.5):.0f}")


def ground():
    x0, y0, dx, dy = lattice()
    T = run_city.TILE
    src = run_city.TILES / EXTENT
    GROUND_DIR.mkdir(parents=True, exist_ok=True)
    gpkg = GROUND_DIR / "zemin.gpkg"           # also the downloadable ground layer
    gpkg.unlink(missing_ok=True)
    env = dict(os.environ, GDAL_DATA=str(QGIS / "apps" / "gdal" / "share" / "gdal"),
               PROJ_LIB=str(QGIS / "share" / "proj"), PROJ_DATA=str(QGIS / "share" / "proj"),
               PATH=str(QGIS / "bin") + os.pathsep + os.environ.get("PATH", ""))
    ogr = str(QGIS / "bin" / "ogr2ogr.exe")
    tmp = GROUND_DIR / "_parca.geojson"
    t0, total = time.time(), 0
    files = sorted(src.glob("t*_layers.geojson"))
    # one tile at a time into the GeoPackage: the whole city at once would not fit in memory
    for k, f in enumerate(files):
        ti, tj = map(int, f.name[1:].split("_")[:2])
        own = box(x0 + ti * T * dx, y0 + tj * T * dy, x0 + (ti + 1) * T * dx, y0 + (tj + 1) * T * dy)
        feats = []
        for ft in json.loads(f.read_text(encoding="utf-8"))["features"]:
            cls = ft["properties"]["cls"]
            if cls not in CODE:
                continue
            g = gp.robust(shapely.intersection, shape(ft["geometry"]), own)
            for p in shapely.get_parts(g):
                if p.geom_type == "Polygon" and p.area > 1e-10:          # ~1 m², slivers out
                    feats.append({"type": "Feature", "geometry": mapping(p), "properties": {"r": CODE[cls]}})
        if not feats:
            continue
        tmp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")),
                       encoding="utf-8")
        cmd = [ogr, "-f", "GPKG", str(gpkg), str(tmp), "-nln", "zemin", "-nlt", "POLYGON", "-a_srs", "EPSG:4326"]
        if gpkg.exists():
            cmd[1:1] = ["-append"]
        subprocess.run(cmd, env=env, check=True, capture_output=True)
        total += len(feats)
        if (k + 1) % 20 == 0:
            print(f"   {k + 1}/{len(files)} parça · {total:,} poligon · {time.time() - t0:.0f} sn", flush=True)
    tmp.unlink(missing_ok=True)
    print(f"zemin.gpkg: {total:,} poligon · {gpkg.stat().st_size / 1048576:.0f} MB · {time.time() - t0:.0f} sn")

    tiles(gpkg)


def tiles(gpkg=GROUND_DIR / "zemin.gpkg"):
    """Two archives, each under GitHub Pages' 100 MB per file: z14–15 in full
    detail (~1 m; the map overzooms z15; adding z16 made 138 MB), and z12–13
    simplified, so the ground shows from the district scale."""
    env = dict(os.environ, GDAL_DATA=str(QGIS / "apps" / "gdal" / "share" / "gdal"),
               PROJ_LIB=str(QGIS / "share" / "proj"), PROJ_DATA=str(QGIS / "share" / "proj"),
               PATH=str(QGIS / "bin") + os.pathsep + os.environ.get("PATH", ""))
    ogr = str(QGIS / "bin" / "ogr2ogr.exe")
    (OUT / "tiles").mkdir(parents=True, exist_ok=True)
    for name, zmin, zmax, extra in (("ground.pmtiles", 14, 15, []),
                                    ("ground_far.pmtiles", 12, 13, ["-dsco", "SIMPLIFICATION=2"])):
        pm = OUT / "tiles" / name
        pm.unlink(missing_ok=True)
        t1 = time.time()
        r = subprocess.run([ogr, "-f", "PMTiles", str(pm), str(gpkg), "zemin", "-nln", "zemin",
                            "-dsco", f"MINZOOM={zmin}", "-dsco", f"MAXZOOM={zmax}",
                            "-dsco", "MAX_SIZE=1500000", *extra], env=env, capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-800:])
        print(f"{name} (z{zmin}–{zmax}) {pm.stat().st_size / 1048576:.0f} MB · {time.time() - t1:.0f} sn")


def lattice():
    """Origin and step of the grid, read back from the grid file itself."""
    f = json.loads((HERE / f"grid_{EXTENT}.geojson").read_text(encoding="utf-8"))["features"][0]
    x_min, y_min, x_max, y_max = shape(f["geometry"]).bounds
    dx, dy = x_max - x_min, y_max - y_min
    return x_min - f["properties"]["i"] * dx, y_min - f["properties"]["j"] * dy, dx, dy


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "karo":
        tiles()
    else:
        cells()
        if arg != "hucre":
            ground()
