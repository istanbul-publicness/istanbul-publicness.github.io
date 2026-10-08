"""
Citywide ground-plane estimate: the grid cut into tiles, computed in parallel,
resumable.

Each tile is 8 x 8 cells. It is painted with a 150 m margin, so a cell on a tile
edge still sees the sidewalk of the road just outside it, but only the tile's
own cells are scored, so every cell is computed exactly once. A finished tile
leaves <tiles>/<extent>/<tile>_run.json (paths.py); a rerun skips those and goes on where it
stopped. At the end the tiles are merged into ground_city_<extent>_cells.csv.

Memory, not CPU, limits the number of workers: a dense tile can take over a
gigabyte. Three is safe with other programs open.

Run:  py run_city.py foursquare 3
"""

import csv
import json
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import P  # noqa: E402

TILES = P["tiles"]          # ~15 MB of layers per tile, 1.6 GB in all: point it at a roomy drive
TILE = 8             # cells per tile side
MARGIN_DEG = (0.0018, 0.00135)   # ~150 m in longitude and latitude at 41° N


def tiles(extent):
    from shapely.geometry import shape
    feats = json.loads((HERE / f"grid_{extent}.geojson").read_text(encoding="utf-8"))["features"]
    groups = defaultdict(list)
    for f in feats:
        p = f["properties"]
        groups[(p["i"] // TILE, p["j"] // TILE)].append(
            {"id": p["id"], "geom": shape(f["geometry"]), "old_pct": p["old_pct"], "old_n": p["old_n"]})
    out = []
    for (ti, tj), cells in sorted(groups.items()):
        xs = [c["geom"].bounds for c in cells]
        w, s = min(b[0] for b in xs) - MARGIN_DEG[0], min(b[1] for b in xs) - MARGIN_DEG[1]
        e, n = max(b[2] for b in xs) + MARGIN_DEG[0], max(b[3] for b in xs) + MARGIN_DEG[1]
        out.append({"id": f"t{ti}_{tj}", "bbox": (s, w, n, e), "cells": cells})
    return out


def work(tile, outdir):
    """One tile, in a worker process."""
    sys.path.insert(0, str(HERE))
    import ground_plane as gp
    t0 = time.time()
    try:
        D = gp.prepare_box(tile["bbox"], gp.OSM_GPKG)
        rows = gp.compute_box(D, tile["cells"], outdir, tile["id"], tile["id"], verbose=False)
        return tile["id"], len(rows), time.time() - t0, None
    except Exception:
        return tile["id"], 0, time.time() - t0, traceback.format_exc()


def merge(extent, outdir):
    rows, grades, runs = [], Counter(), []
    for f in sorted(outdir.glob("*_cells.csv")):
        rows += list(csv.DictReader(f.open(encoding="utf-8")))
    for f in sorted(outdir.glob("*_run.json")):
        runs.append(json.loads(f.read_text(encoding="utf-8")))
    out = HERE / f"ground_city_{extent}_cells.csv"
    if rows:
        with out.open("w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
    shas = {r["rules_sha256"] for r in runs}
    summary = {"extent": extent, "tiles": len(runs), "cells": len(rows),
               "grades": dict(Counter(r["guven"] for r in rows)),
               "rules_sha256": sorted(shas), "rules_consistent": len(shas) == 1,
               "gosterim": dict(Counter(r.get("gosterim") for r in rows)),
               # summed over tiles: a record in a tile's 150 m margin is counted by both tiles
               "evidence_tile_sum": dict(sum((Counter(r["evidence"]) for r in runs), Counter())),
               "microsoft_added": sum(r["microsoft_added"] for r in runs)}
    (HERE / f"ground_city_{extent}_run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    print(f"\nbirleştirildi: {out.name} · {len(rows):,} hücre · {len(runs)} parça · güven {summary['grades']}")
    if len(shas) > 1:
        print("UYARI: parçalar farklı kural sürümleriyle hesaplanmış:", shas)


def main(extent, workers):
    outdir = TILES / extent
    outdir.mkdir(parents=True, exist_ok=True)
    todo = [t for t in tiles(extent) if not (outdir / f"{t['id']}_run.json").exists()]
    total = len(list(tiles(extent)))
    print(f"{extent}: {total} parça · kalan {len(todo)} · {workers} paralel işlem")
    t0, done, failed = time.time(), 0, []
    log = (outdir / "_log.txt").open("a", encoding="utf-8")
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, t, outdir): t["id"] for t in todo}
        for fut in as_completed(futs):
            tid, n, secs, err = fut.result()
            done += 1
            if err:
                failed.append(tid)
                log.write(f"HATA {tid}\n{err}\n")
            el = time.time() - t0
            eta = el / done * (len(todo) - done)
            line = (f"[{done}/{len(todo)}] {tid}: {n} hücre, {secs / 60:.1f} dk"
                    f"{' — HATA' if err else ''} · geçen {el / 60:.0f} dk · kalan ~{eta / 60:.0f} dk")
            print(line, flush=True)
            log.write(line + "\n")
            log.flush()
    log.close()
    if failed:
        print(f"\nHATALI PARÇALAR ({len(failed)}): {', '.join(failed)} — ayrıntı _log.txt'de; "
              "yeniden çalıştırınca yalnız bunlar denenir")
    merge(extent, outdir)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "foursquare",
         int(sys.argv[2]) if len(sys.argv) > 2 else 3)
