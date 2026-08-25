# `datagen` — container-logistics data generation (internals)

This document describes **how the `datagen` package works inside** — its modules,
data structures, and the path a `GenerationSpec` takes to become agent behaviors.
For how the rest of the system *feeds* this package (ScenarioManager, the frontend
spec, config overrides), see `docs/data_generation_flow.md` instead.

For the **end-to-end scenario workflow** — the `spec.json` source, the compile step,
the `scenario_gen.py` override hooks, and how generation fits the
`source → compile → run` pipeline — see **`docs/scenario_workflow.md`**.

---

## 1. What this package is

A **pure, isolated, data-driven** generator. Its entire job:

> one immutable `GenerationSpec` in → the 5 agent behavior collections out
> (and, optionally, the 6 JSON files written to disk).

Three properties hold by construction:

- **Pure / isolated** — imports nothing from the scenario or runtime layers, reads
  no module globals, needs no Kafka/Celery/Mongo/engine. You can drive it with just
  a spec, which is why it is unit-testable standalone.
- **Real addresses only** — every coordinate is a genuine on-land postal address
  sampled from `locations_cleaned.csv` (optionally validated against the Singapore
  land mask). No polygon-interior sampling, no land-clamp hacks.
- **Data-driven codes** — there is **no hardcoded location-code list**. Codes are
  discovered from the address book; per-code presentation and facility share come
  from caller-supplied metadata with derived defaults. A brand-new code "just works."

The guiding slogan is **"sample, then generate."**

---

## 2. Module map

| Module | Role | Imports beyond stdlib |
|---|---|---|
| `spec.py` | `GenerationSpec` — the immutable input contract | — |
| `codes.py` | `CodeRegistry` / `LocationType` — what codes mean | — |
| `catalog.py` | `LocationCatalog` + `SingaporeMask` — the **sample** half | shapely (mask only) |
| `trip_matrix.py` | parse / restrict / sample the pickup→delivery matrix | — |
| `sampling.py` | weighted request-time-of-day sampling | — |
| `builders.py` | `*Builder` — the **generate** half (behavior dicts) | `haul_trip_duration`, `duration_constants` (pure leaves) |
| `generator.py` | `ScenarioGenerator` / `GenerationResult` — orchestrate + write | — |
| `__init__.py` | public surface | — |

The only app imports anywhere are the two **pure leaf** modules
`haul_trip_duration` / `duration_constants` (constants + a clamp function, no
globals, no back-edge) — reused rather than duplicated.

---

## 3. The pipeline inside

```mermaid
flowchart TD
    SPEC["GenerationSpec (frozen input)"] --> GEN["ScenarioGenerator"]
    GEN --> CAT["LocationCatalog\n(load CSV, apply mask, index by code)"]
    GEN --> BLD["builders"]

    subgraph SAMPLE["sample"]
      CAT --> REG["CodeRegistry\n(labels / prefixes / facility weights)"]
      REG --> SITES["catalog.facility_sites(n, registry)\n→ real facility coords + code + type"]
    end

    subgraph GENERATE["generate (per agent)"]
      BLD --> TB["TruckBuilder → truck dict"]
      BLD --> OB["OrderBuilder → order dict\n(matrix sample → facility of that type)"]
      BLD --> FB["FacilityBuilder → facility dict"]
      BLD --> AB["Assignment / Analytics → static dicts"]
    end

    SITES -.via facility_settings.profile.facilities.-> BLD
    TB & OB & FB & AB --> RES["GenerationResult\n5 collections"]
    RES --> WRITE["GenerationResult.write(dir)\n→ 6 JSON files"]
```

Note one subtlety: the **facility site list** is normally produced by the
scenario/config layer (which builds a `CodeRegistry` and calls
`catalog.facility_sites`) and handed in via `spec.facility_settings["profile"]["facilities"]`.
The builders read facilities from there. The catalog inside the generator is used
for **truck origin** sampling. (When you drive `datagen` standalone, you build the
registry + sites yourself — see §9.)

---

## 4. `spec.py` — the input contract

`GenerationSpec` is a `@dataclass(frozen=True)`. Frozen = no accidental mutation;
same spec in ⇒ same behaviors out. Field groups:

- **Counts**: `num_trucks`, `num_orders`, `num_facilities`.
- **Calendar**: `simulation_days`, `step_interval_seconds`,
  `simulation_length_in_steps`, `reference_time`, `behavior_revision`.
- **Role settings** (opaque pass-throughs mirroring `scenario_config.*_settings`):
  `truck_settings`, `order_settings`, `facility_settings`, `assignment_settings`,
  `analytics_settings`. Builders read knobs (`steps_per_action`, `profile`, …) from
  these — this is how behavior-dict field parity with the legacy generator is kept.
- **Hauliers**: `truck_hauliers`, `order_hauliers` — already **distributed per agent**
  (length `num_trucks` / `num_orders`) by the adapter.
- **Demand**: `hourly_weights` (24 normalized), `business_hour_start/end`.
- **Trips**: `trip_matrix` (normalized, restricted to codes with real addresses).
- **Codes / origins**: `excluded_codes` (e.g. `("YD",)`), `truck_origin_codes`
  (`None` = any code).
- **Data sources**: `locations_csv`, `sg_mask_path`.
- **Optional**: `orsim_settings`, `generation_spec_meta` (for the written
  `orsim_settings.json`).

Helpers: `simulation_end_step` (= `simulation_length_in_steps - 1`) and
`facilities()` (reads `facility_settings["profile"]["facilities"]`).

---

## 5. `codes.py` — what codes mean (the data-driven core)

No code table lives here; everything is derived or supplied.

- **`LocationType(code, label, name_prefix, facility_weight)`** — frozen per-code record.
- **`derive_label(code, metadata)`** — `metadata[code]["label"]` else `code.title()`.
- **`trip_matrix_marginals(trip_matrix, codes)`** — each code's total incidence as a
  pickup or delivery; this is the **demand signal** used to size the facility mix.
- **`CodeRegistry`** — the single source of truth. `.codes`, `.label(c)`,
  `.facility_type(c)` (= label), `.prefix(c)`, `.weight(c)`.
  - **`CodeRegistry.build(address_codes, trip_matrix, metadata)`** resolves, per code:
    - `label`  ← metadata, else `code.title()`
    - `prefix` ← metadata, else label/code slugified
    - `weight` ← explicit `metadata[code]["weight"]`, else its **trip-matrix marginal**
      (demand-proportional). If every weight ends up 0, fall back to an equal split.

So adding `RL` to the CSV + matrix yields a working `LocationType("RL","Rl","rl",<marginal>)`
with no edits here.

---

## 6. `catalog.py` — the "sample" half

### `LocationCatalog(locations_csv, mask_path=None, excluded=())`
- `_load` reads the CSV and indexes `{code: [(lon, lat), …]}`, **discovering every
  code** except those in `excluded`; if a mask is active each point must pass
  `mask.contains` (drops sea/out-of-bounds points).
- `codes()` — sorted tuple of codes that have ≥1 real address.
- `available()` — `{code: count}`.
- `sample(code, rng)` — a real `(lon, lat)`; falls back to the densest pool if a code
  is empty.
- `sample_origin(codes, rng)` — a real start coordinate from `codes` (default: any).
- `facility_sites(n, registry, gate_count, service_time)` — see below.

### `SingaporeMask(geojson_path=None)`
- No-op until a file is supplied. `_load` unions the polygons and wraps them in a
  **prepared geometry** (`shapely.prepared.prep`) so 1000s of `contains` checks cost
  ~0.02s instead of ~4.4s. `active` / `contains(lon, lat)`.
- Resolved by `default_sg_mask_path()` from `_SG_MASK_CANDIDATES`
  (currently `SingaporeMaskNoSea.geojson`).

### `_allocate_facility_counts(n, available, registry)` — the facility mix
1. Take codes with addresses **and** positive registry weight (fallback: any code
   with addresses).
2. Largest-remainder split of `n` by weight.
3. Cap each code at its available address count; spill overflow to the
   highest-weight codes with room.
4. **Guarantee ≥1 facility for every weighted code** (steal from the largest holder,
   never zeroing it) — so any code that participates in trips can host orders.

### `facility_sites(...)`
Allocates counts, then for each code draws that many **distinct** real addresses with
a fixed-seed RNG (`_FACILITY_SAMPLE_SEED`) so facility positions are **stable across
regenerations**. Each emitted site dict:

```python
{"name": f"{prefix}_{i:03d}", "lat": …, "lon": …,
 "gate_count": …, "service_time": …,
 "code": "CU", "facility_type": "Warehouse"}   # registry-derived
```

---

## 7. `trip_matrix.py` — the pickup→delivery distribution

Codes are taken from the matrix itself; **no fixed code set**.

- `parse_trip_matrix(raw)` — discovers codes from `raw`, builds the grid, clamps
  negatives, **forces the diagonal to 0**, normalizes to sum 1. **Raises** if there
  is no positive off-diagonal weight (the matrix is a required input — `datagen`
  invents no default).
- `restrict_trip_matrix(matrix, allowed_codes)` — drops codes not in `allowed_codes`
  (excluded, or with no addresses, e.g. `YD`) and renormalizes.
- `sample_pickup_delivery_codes(rng, *, matrix)` — weighted choice over the matrix's
  own `(pickup, delivery)` cells; `matrix` is **required**.
- `location_type_for_code(code, metadata=None)` — label fallback used only when a
  facility has no `facility_type` (normally the facility carries it).

---

## 8. `builders.py` — the "generate" half

Shared helpers: `geojson_point(lon, lat)` (JSON-identical to shapely
`mapping(Point)`) and `resolve_facilities(facility_settings)`. The latter is
**strict**: facilities must be data-generated (via `LocationCatalog.facility_sites`)
and handed in through `facility_settings["profile"]["facilities"]`; a missing/empty
list — or any facility lacking a required field in `_REQUIRED_FACILITY_FIELDS`
(`name`, `lat`, `lon`, `code`, `facility_type`, `gate_count`, `service_time`) —
raises `ValueError` and stops generation. **There are no default facility centers
and no fallbacks.**

`_BaseBuilder` holds `spec`, `catalog`, `rng`, the resolved `facilities`, and a
`_default_haulier`. Each builder emits a dict **field-compatible** with the legacy
schema (so agents/sim are unaffected):

- **`TruckBuilder`** — shift bounds in step indices; `init_loc` via
  `catalog.sample_origin(spec.truck_origin_codes)` (a real address, no hardcoded
  origin); ETAs randomized within profile bounds then passed through
  `apply_haul_trip_duration_floors`.
- **`OrderBuilder`** — groups facilities by code; samples `(pickup_code, dropoff_code)`
  from `spec.trip_matrix`; picks a facility of each type. **The realized code/type
  then follow the chosen facility**, so `code ↔ location_type ↔ facility ↔ coordinate`
  are coherent *even under tiny-`n` fallback*. Request time via `sampling`.
- **`FacilityBuilder`** — one agent per site; `facility_type` from the site.
- **`AssignmentBuilder` / `AnalyticsBuilder`** — pass the static profiles through.

---

## 9. `generator.py` — orchestrate + persist

- **`ScenarioGenerator(spec, catalog=None, rng=random)`** — builds a catalog from the
  spec (`locations_csv`, `sg_mask_path`, `excluded_codes`) if none is passed.
  `.generate()` instantiates the builders and produces ids
  `truck_NNNNNN` / `order_NNNNNN` / `facility_NNN` / `assignment_main` /
  `analytics_000`, returning a `GenerationResult`.
- **`GenerationResult`** — the 5 collections + optional `orsim_settings`; `.write(dir)`
  is the **only** filesystem I/O (dumps the 6 JSON files, `indent=4, sort_keys=True`).
  When `spec.orsim_settings` is set it is written too, stamped with
  `BEHAVIOR_REVISION` and `GENERATION_SPEC`.

### Standalone usage
```python
from apps.container_logistics.datagen import (
    LocationCatalog, CodeRegistry, GenerationSpec, ScenarioGenerator,
    parse_trip_matrix, restrict_trip_matrix, default_locations_csv, default_sg_mask_path,
)

cat = LocationCatalog(default_locations_csv(), default_sg_mask_path(), excluded=("YD",))
matrix = restrict_trip_matrix(parse_trip_matrix(RAW_MATRIX), cat.codes())
reg = CodeRegistry.build(cat.codes(), matrix, metadata=LOCATION_TYPE_METADATA)
sites = cat.facility_sites(15, reg, gate_count=1, service_time=1800)

spec = GenerationSpec(
    domain="container-logistics-sim", num_trucks=10, num_orders=200,
    num_facilities=len(sites), simulation_days=1, step_interval_seconds=240,
    simulation_length_in_steps=360, trip_matrix=matrix, excluded_codes=("YD",),
    facility_settings={"profile": {"facilities": sites}},
)
ScenarioGenerator(spec, cat).write("/tmp/scenario")   # no Kafka/Celery/Mongo needed
```

---

## 10. Invariants worth preserving

- **Real on-land coordinates only** — every coord is a CSV address and (with the mask
  active) passes `SingaporeMask.contains`.
- **Order coherence** — `pickup_code == pickup_facility["code"]`,
  `pickup_location_type == facility_type == registry.label(code)` (and likewise for
  dropoff). The realized code follows the facility, never the other way around.
- **No hardcoded codes** — `grep` the package for `"CT"/"CU"/"MT"` literals: only a
  docstring example remains. New codes flow purely from data + metadata.
- **Deterministic facilities** — `facility_sites` is seeded; positions don't jump
  between regenerations of the same scenario.
- **Behavior-dict schema parity** — the emitted dicts match the legacy generator
  field-for-field; only *where* things are placed changed.

## 11. Extension points

- **New location code** — add rows to `locations_cleaned.csv` + a cell in the trip
  matrix. Optionally add a `LOCATION_TYPE_METADATA` entry for a nicer label. No
  `datagen` change. (Covered by `tests/test_datagen_new_code.py`.)
- **New agent role** — add a `*Builder(_BaseBuilder)` and wire it into
  `ScenarioGenerator.generate()` / `GenerationResult`.
- **Different land mask** — drop a GeoJSON into `openroad_locations/` named per
  `_SG_MASK_CANDIDATES` (or extend that list).
