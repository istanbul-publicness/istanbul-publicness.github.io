"""
Match Foursquare and Overture places that are the same place, and derive from
the matches which Foursquare category corresponds to which Overture category.

Two records are the same place when they stand within 50 m of each other and
their names agree once case, Turkish letters and punctuation are set aside
(similarity >= 0.8, or one name containing the other). Each Foursquare record
takes its best match only.

The matched pairs serve two decisions:
    D09  a place present in both sources counts once, as one piece of evidence
    the dictionaries: a reviewer's regime for an Overture category carries over
         to the Foursquare categories that the matches show to be the same kind
         of place, even where the names differ ('Government Building' and
         'government_office')

Inputs   Foursquare v3 geopackage (open records), Overture place_point (confidence >= 0.5)
Outputs  source_matches.csv, crosswalk_fsq_overture.csv

Run:  py match_sources.py
"""

import csv
import json
import math
import re
import sqlite3
import struct
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import P  # noqa: E402

FSQ = P["foursquare_places"]
OVT = P["overture_places"]

MAX_M = 50.0
MIN_SIM = 0.8
MIN_CONF = 0.5
CELL = 0.001            # degrees, ~110 m north-south — bins for the neighbour search
KX, KY = math.cos(math.radians(41.0)) * 111_320.0, 110_574.0

TR = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
                    "Ç": "c", "Ğ": "g", "İ": "i", "I": "i", "Ö": "o", "Ş": "s", "Ü": "u"})


def norm_name(s):
    if not s:
        return ""
    s = str(s).translate(TR).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def similar(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 5 and len(b) >= 5 and (a in b or b in a):
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def gpkg_point(blob):
    flags = blob[3]
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 0b111]
    w = bytes(blob[8 + env:])
    bo = "<" if w[0] == 1 else ">"
    x, y = struct.unpack(bo + "dd", w[5:21])
    return x, y


def load_overture():
    con = sqlite3.connect(f"file:{OVT}?mode=ro", uri=True)
    tbl, gcol = con.execute("SELECT table_name, column_name FROM gpkg_geometry_columns").fetchone()
    bins = defaultdict(list)
    rows = []
    for gid, blob, name, bc, tax, conf in con.execute(
            f'SELECT id, "{gcol}", names_pri, basic_cat, taxonomy, confidence FROM "{tbl}"'):
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < MIN_CONF or blob is None:
            continue
        x, y = gpkg_point(blob)
        if not bc:
            try:
                h = json.loads(tax).get("hierarchy") or []
            except Exception:
                h = []
            bc = f"(yok) {h[0] if h else '?'}"
        i = len(rows)
        rows.append((gid, x, y, norm_name(name), bc))
        bins[(int(x / CELL), int(y / CELL))].append(i)
    return rows, bins


def main():
    t0 = time.time()
    ov, bins = load_overture()
    print(f"Overture: {len(ov):,} mekân (güven ≥ {MIN_CONF}) · {time.time() - t0:.0f} sn")

    con = sqlite3.connect(f"file:{FSQ}?mode=ro", uri=True)
    q = ("SELECT place_id, longitude, latitude, name, cat_name, refreshed FROM poi_fsq_istanbul_dovey_v3 "
         "WHERE closed IS NULL")
    pairs, n_fsq = [], 0
    per_cat = Counter()
    used = set()
    for pid, lon, lat, name, cat, refreshed in con.execute(q):
        n_fsq += 1
        per_cat[cat] += 1
        if lon is None or lat is None:
            continue
        a = norm_name(name)
        if not a:
            continue
        bx, by = int(lon / CELL), int(lat / CELL)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in bins.get((bx + dx, by + dy), ()):
                    gid, x, y, b, bc = ov[i]
                    d = math.hypot((x - lon) * KX, (y - lat) * KY)
                    if d > MAX_M:
                        continue
                    s = similar(a, b)
                    if s >= MIN_SIM and (best is None or (s, -d) > (best[0], -best[1])):
                        best = (s, d, i)
        if best:
            s, d, i = best
            pairs.append((pid, ov[i][0], round(d, 1), round(s, 2), cat, ov[i][4], (refreshed or "")[:10]))
            used.add(i)
        if n_fsq % 200_000 == 0:
            print(f"   {n_fsq:,} Foursquare kaydı · {len(pairs):,} eşleşme · {time.time() - t0:.0f} sn")

    with (HERE / "source_matches.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fsq_place_id", "overture_id", "mesafe_m", "isim_benzerligi", "fsq_kategori",
                    "overture_kategori", "fsq_guncelleme"])
        w.writerows(pairs)

    # crosswalk: for each Foursquare category, the Overture categories its matches fall in
    cw = defaultdict(Counter)
    for _, _, _, _, cat, bc, _ in pairs:
        cw[cat][bc] += 1
    with (HERE / "crosswalk_fsq_overture.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fsq_kategori", "fsq_acik_kayit", "eslesen", "eslesme_orani",
                    "en_sik_overture", "pay", "ikinci_overture", "ikinci_pay"])
        for cat, n in per_cat.most_common():
            c = cw.get(cat, Counter())
            m = sum(c.values())
            top = c.most_common(2) + [("", 0), ("", 0)]
            w.writerow([cat, n, m, round(m / n, 3) if n else 0,
                        top[0][0], round(top[0][1] / m, 3) if m else 0,
                        top[1][0], round(top[1][1] / m, 3) if m else 0])

    print(f"\nFoursquare açık kayıt {n_fsq:,} · Overture'da karşılığı olan {len(pairs):,} "
          f"(%{len(pairs) / n_fsq * 100:.1f}) · kullanılan Overture kaydı {len(used):,} "
          f"(%{len(used) / len(ov) * 100:.1f}) · {time.time() - t0:.0f} sn")
    print("yazıldı: source_matches.csv, crosswalk_fsq_overture.csv")


if __name__ == "__main__":
    main()
