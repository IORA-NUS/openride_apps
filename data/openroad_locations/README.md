# openroad_locations — real Singapore location data

Input data for scenario generation. Committed rather than fetched: it is 2.2 MB,
static, and required at **module import time** by
`apps/container_logistics/datagen/catalog.py`, so a fetch step would make the
package unimportable until the network had been consulted.

| File | Used by |
|---|---|
| `locations_cleaned.csv` | `LocationCatalog` — the real address book; every facility coordinate comes from here |
| `SingaporeMaskNoSea.geojson` | land mask used to validate sampled coordinates |
| `port_regions_cleaned.geojson` | port (CT) facility masks |
| `depot_regions_cleaned.geojson` | depot (MT/YD) facility masks |

Two files from the original drop are **deliberately not here**:
`industrial_zones.geojson` (8.4 MB) and `heavy_vehicle_parking.json` (5.5 MB).
Both have zero code references. Industrial zones fed the polygon-interior sampling
that the 2026-06-17 datagen rewrite removed; parking was never used (no trailers).
They remain in the original out-of-repo drop if ever needed.

Override the directory with `OPENRIDE_LOCATIONS_DIR`.
