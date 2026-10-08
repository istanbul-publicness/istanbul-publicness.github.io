# Ground publicness of Istanbul

An open map and dataset of how public the ground of Istanbul is, cell by cell,
read from the ground plane rather than from counts of venues.

**Map:** https://istanbul-publicness.github.io/
**Data:** `data/cells.csv`, `data/cells.geojson`; ground plane as vector tiles in
`tiles/`; the full ground plane as a GeoPackage in the archive linked below.
**Rules:** `code/publicness_kurallar.xlsx`, where every decision was taken.

> Author information is withheld while the associated paper is under
> double-blind review.

---

## What it measures

Each 500 m cell's ground is partitioned so that every square metre is assigned
one access regime, after Dovey & Pafka's (2020) access/control typology:
open-public, quasi-public, ticketed, open-private, invitation, inaccessible.
The shapes come from OpenStreetMap: roads (carriageway and modelled sidewalks),
pedestrian ways and squares, parks, named sites (hospitals, campuses, stations,
mosques …), closed land uses, military land, rail, water and buildings.

Place records — Foursquare Open Source Places and Overture Maps places — are
**not counted**. They decide only what a building's ground floor is: a building
takes the most frequent regime among the records inside it or within 10 m.
A building with twelve cafés therefore weighs as much as its footprint, not
twelve times as much.

The cell score is the area-weighted mean of the regime scores:

| regime | score |
|---|---|
| open-public | 1.000 |
| open-private | 0.500 |
| quasi-public | 0.469 |
| ticketed | 0.375 |
| invitation | 0.125 |
| inaccessible | 0.000 |

The scores come from P = A × [α + (1 − α) C] with α = 0.5, where A is access and
C is control. Open-private sits above quasi-public because quasi-public space
(malls, privately run plazas) usually adds security screening at the door.

### Decisions (workbook sheet `kararlar`)

| no | decision |
|---|---|
| D01 | scores as above (balanced A × C, α = 0.5) |
| D02 | car space out of the score; its share reported |
| D03 | residential ground between buildings is its own class (yard), scored as invitation |
| D04 | a building of unknown use is out of the score unless a place record lies inside it or within 10 m |
| D05–06 | mosques and cemevi open-public; churches and synagogues open-private |
| D07 | `fee=yes` makes a site ticketed; `fee=no` opens it, and the regime then follows who runs it (public operator → open-public, private → quasi-public) |
| D08 | a building takes its most frequent place regime; ties go to the more public |
| D09 | Foursquare and Overture both count; a matched pair (≤ 50 m, name similarity ≥ 0.8) counts once; Foursquare records not refreshed since 2019 and absent from Overture are dropped, as are closed venues and Overture records below 0.5 confidence |
| D10 | the map shows each cell's own ground |
| D11 | a walking-distance score (10-minute network isochrone) is computed only for sample places, not citywide |
| D12 | barriers measured separately: major roads and surface rail — their length, how many pieces they cut the ground into, and the pedestrian crossings over them |
| D13 | free parking open-public; parking with no fee tag stays ticketed |
| D14 | larger forests and open natural ground out of the score; share reported |
| D15 | grass open-public; meadow, farmland, scrub, heath and quarries out of the score |
| D16 | a cell whose scorable ground is under a tenth of its land is shown as *insufficient evidence* |
| D17 | beaches open-public; with `fee=yes` ticketed |
| D18 | forest under 50 ha (an urban grove, *koru*) open-public |

Every OSM tag rule, the 248 Overture and 979 Foursquare category assignments,
the cleaning rules and the reviewers' overrides are in the workbook. `rules.json`
is the workbook compiled for the code; its `sha256` field identifies the
workbook version each run used, and every run writes it into its outputs.

### Uncertainty

Two ranges are reported for every cell:

- **missing-data range** (`P_lo`–`P_hi`): unmapped ground and buildings of
  unknown use counted as fully private or fully public;
- **rule range** (`P_rule_lo`–`P_rule_hi`): the lowest and highest score across
  five alternative rule sets — sevenths scale, α = 0.7, α = 0.3, car space as
  inaccessible, unknown buildings as residential.

Grade **A**: missing-data range < 0.2 and rule range < 0.1; **B**: < 0.4 and
< 0.2; **C** otherwise (hatched on the map).

---

## Cell table (`data/cells.csv`)

| column | meaning |
|---|---|
| `cell_id`, `lon`, `lat` | grid cell (500 m) and its centre |
| `P_ground` | cell score, 0–1 |
| `P_yuzdelik` | percentile among cells shown with a score ("more public than X % of the city") |
| `P_lo`, `P_hi` | missing-data range |
| `P_rule_lo`, `P_rule_hi` | rule range |
| `guven` | grade A / B / C |
| `gosterim` | `puan` (shown with a score) or `kanıt yetersiz` (insufficient evidence, D16) |
| `P_yedide_bir`, `P_a07`, `P_a03`, `P_car_inacc`, `P_ground_bld` | score under each alternative rule set |
| `sh_open_public` … `sh_inaccessible` | share of the cell's land in each regime |
| `sh_yard`, `sh_car`, `sh_natural`, `sh_unknown_building`, `sh_unmapped` | residential yards, car space, larger forest / farmland, buildings of unknown use, unmapped ground |
| `scored_share` | share of the land that enters the score |
| `bar_major_m`, `bar_rail_m` | length of major roads and surface rail in the cell (m) |
| `bar_pieces` | pieces of ≥ 2,000 m² the barriers cut the land into |
| `bar_level`, `bar_passes`, `bar_crossings` | level crossings, footbridges/underpasses, and their sum |
| `bar_cross_per_km` | crossings per km of barrier |

Ground classes in the vector tiles (`r`): `op` open-public, `qp` quasi-public,
`ti` ticketed, `pr` open-private, `in` invitation, `na` inaccessible, `ya` yard,
`ca` car space, `do` forest/farmland, `ub` building of unknown use, `um` unmapped.

---

## Coverage and limits

- 28.79–29.27° E, 40.84–41.29° N — the extent of the Foursquare data; 5,499
  cells at least a quarter on land.
- Only the ground floor is read. Upper floors are not.
- Where OpenStreetMap has not mapped a use, the ground stays *unmapped* and
  widens the missing-data range rather than being guessed.
- Sidewalks are mostly modelled from road class, because few are mapped.

---

## Reproducing it

Requirements: Python 3.12 with `shapely>=2.1`, `pandas`, `numpy`; GDAL ≥ 3.8
command-line tools (`ogr2ogr`, with the PMTiles driver for the tiles); `openpyxl`
to compile the workbook. Input locations are set in `code/paths.py`; a local
`code/paths_local.json` can point them elsewhere.

Inputs (place them under `code/inputs/`, see `paths.py`):
OpenStreetMap Turkey extract (Geofabrik); Overture Maps places and buildings
for Istanbul; Foursquare Open Source Places for Istanbul; a land polygon;
OSM pedestrian crossings, footbridges and underpasses.

```
cd code
python compile_rules.py            # workbook -> rules.json
python extract_osm.py              # Turkey PBF -> Istanbul GeoPackage
python match_sources.py            # Foursquare <-> Overture matches (source_matches.csv)
python run_city.py foursquare 3    # citywide run, 115 tiles, resumable
python build_map_layers.py         # cells table, ground GeoPackage, vector tiles
```

`grid_foursquare.geojson` (the 500 m grid) and `source_matches.csv` are
included, so the last two steps run without rebuilding them.

---

## Sources and licences

- OpenStreetMap © OpenStreetMap contributors, ODbL — Geofabrik extract of
  6 October 2026.
- Overture Maps Foundation places and buildings, October 2026 release
  (places CDLA-Permissive-2.0; buildings ODbL).
- Foursquare Open Source Places (Apache 2.0).
- Base map © CARTO; map built with MapLibre GL and PMTiles.

The data in `data/` and `tiles/` derive from OpenStreetMap and are released
under the **Open Database License (ODbL 1.0)** — see `DATA_LICENSE.md`. The
code is released under the **MIT licence** — see `LICENSE`.

## Citation

To follow once the paper is published.
