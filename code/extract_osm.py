"""
Cut Istanbul out of the Geofabrik Turkey extract into one GeoPackage.

One file, one date, for the whole city: the test boxes fetched OSM through
Overpass box by box, which does not scale to a hundred tiles and puts a heavy
load on a public server. The province's extent is cut once now, so that the
first pass (the Foursquare extent) and the later province-wide layer read the
same data.

Layers written: points, lines, multipolygons (relations only — see
osmconf_publicness.ini). Every tag is kept in all_tags.

Run:  py extract_osm.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import P  # noqa: E402

QGIS = P["qgis"]
PBF = P["osm_pbf"]
OUT = P["osm_gpkg"]
CONF = HERE / "osmconf_publicness.ini"
PROVINCE = (27.95, 40.78, 29.97, 41.60)      # west, south, east, north


def main():
    if OUT.exists():
        print(f"zaten var: {OUT}")
        return
    env = dict(os.environ,
               GDAL_DATA=str(QGIS / "apps" / "gdal" / "share" / "gdal"),
               PROJ_LIB=str(QGIS / "share" / "proj"), PROJ_DATA=str(QGIS / "share" / "proj"),
               PATH=str(QGIS / "bin") + os.pathsep + os.environ.get("PATH", ""),
               OSM_MAX_TMPFILE_SIZE="2000", OGR_INTERLEAVED_READING="YES")
    cmd = [str(QGIS / "bin" / "ogr2ogr.exe"), "-f", "GPKG", str(OUT), str(PBF),
           "-oo", f"CONFIG_FILE={CONF}", "-oo", "USE_CUSTOM_INDEXING=YES",
           "-spat", *map(str, PROVINCE), "-gt", "65536", "-progress",
           "points", "lines", "multipolygons"]
    t0 = time.time()
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(r.stdout[-400:], r.stderr[-1200:])
    if r.returncode or not OUT.exists():
        print("BAŞARISIZ"); sys.exit(1)
    import sqlite3
    con = sqlite3.connect(f"file:{OUT}?mode=ro", uri=True)
    for (t,) in con.execute("SELECT table_name FROM gpkg_contents"):
        print(f"   {t:<16}{con.execute(f'SELECT COUNT(*) FROM \"{t}\"').fetchone()[0]:>12,}")
    print(f"yazıldı: {OUT} ({OUT.stat().st_size / 1048576:.0f} MB) · {time.time() - t0:.0f} sn")


if __name__ == "__main__":
    main()
