# Simulated calendar horizon (Phase 1).
SIMULATION_DAYS = 7
# Coarser steps: 240s sim tick → 288 scheduler steps / sim day (~<10 min wall @ ~2s/step).
STEP_INTERVAL_SECONDS = 240

# Scalability test sizing (auto-regenerated at sim start if cached behaviors differ).
# Target: 500-truck fleet, 5000 orders / sim day (500 * 10 orders/truck/day).
NUM_TRUCKS = 500
ORDERS_PER_TRUCK_PER_DAY = 10
NUM_FACILITIES = 15
FACILITY_GATE_COUNT = 1

# Bump when order timing / agent counts change — triggers behavior regen on next sim start.
# Rev 10: hauliers — every truck/order now carries a haulier_id; matching is haulier-scoped.
BEHAVIOR_REVISION = 10

# Default warm-up cap for builtin scenarios (frontend uses recommended_early_order_count).
EARLY_ORDER_COUNT = 64

# Observed pickup_code -> delivery_code trip distribution ("Sum of count_pct" pivot
# over real haul trips), restricted to the active CT/CU/MT codes (YD excluded).
# This is the *config-layer* default supplied to the datagen module — datagen holds
# no baked-in probabilities; callers (frontend matrix, or this default) provide one.
# Weights are relative; the diagonal is forced to 0 and the grid is normalized at use.
DEFAULT_TRIP_MATRIX = {
    "CT": {"CT": 0, "CU": 15, "MT": 4},
    "CU": {"CT": 11, "CU": 0, "MT": 14},
    "MT": {"CT": 3, "CU": 14, "MT": 0},
}

# Per-code presentation supplied to the datagen module. datagen holds no code
# table; a code absent here still works (label/prefix are derived from the code
# itself). Add an entry only to override the auto-derived label/prefix, or to pin
# a code's facility-mix share via an explicit "weight" (otherwise the share is
# demand-proportional to the trip matrix). A brand-new code in the CSV + matrix
# needs no change here.
# Per-code presentation + (optional) real-footprint "mask" file in openroad_locations/.
# A code gets a footprint polygon only when it declares a `mask_file` that exists;
# otherwise it has no mask (masks are optional, never compulsory). CU/warehouse has
# no real footprint data yet, so it is intentionally mask-less.
LOCATION_TYPE_METADATA = {
    "CT": {"label": "Port", "prefix": "port", "mask_file": "port_regions_cleaned.geojson"},
    "CU": {"label": "Warehouse", "prefix": "customer"},
    "MT": {"label": "Depot", "prefix": "depot", "mask_file": "depot_regions_cleaned.geojson"},
}

# Location codes present in the address book but excluded from generation.
EXCLUDED_CODES = ("YD",)

# Codes trucks may start their shift at (None = any code with real addresses).
TRUCK_ORIGIN_CODES = None

import re as _re

# Hauliers: the companies that issue orders and own trucks. A truck may only be
# assigned orders issued by its own haulier (matched on the stable ``id``).
# ``fleet_share`` / ``order_share`` are **percentages** of the truck fleet and of
# the orders. They are enforced to sum to 100 across all hauliers (separately for
# fleet and for orders) — see ``normalize_hauliers``. The default single haulier
# owns 100% of both, reproducing pre-rev-10 behavior (one company owning
# everything), so existing scenarios are unchanged.
HAULIER_SHARE_TOTAL = 100.0
# Float slack so hand-entered integer percentages (and minor rounding) still pass.
_HAULIER_SHARE_TOLERANCE = 0.01

HAULIERS = [
    {"id": "haulier", "name": "Haulier", "fleet_share": 100.0, "order_share": 100.0},
]


def _validate_share_total(hauliers, key):
    """Raise ValueError unless ``key`` percentages across ``hauliers`` sum to 100."""
    total = sum(float(h[key]) for h in hauliers)
    if abs(total - HAULIER_SHARE_TOTAL) > _HAULIER_SHARE_TOLERANCE:
        breakdown = ", ".join(f"{h['id']}={h[key]:g}" for h in hauliers)
        raise ValueError(
            f"Haulier '{key}' percentages must sum to {HAULIER_SHARE_TOTAL:g} "
            f"(got {total:g} across {len(hauliers)} haulier(s): {breakdown})."
        )


def haulier_slug(name, *, fallback="haulier"):
    """Stable, filesystem/id-safe slug derived from a haulier display name."""
    slug = _re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower())
    slug = _re.sub(r"_+", "_", slug).strip("_")
    return slug[:64] or fallback


def normalize_hauliers(raw):
    """Coerce a raw haulier list into canonical ``{id, name, fleet_share, order_share}`` dicts.

    - Fills a slug ``id`` from ``name`` when missing; de-duplicates colliding ids.
    - ``fleet_share`` / ``order_share`` are **percentages**. When no haulier
      specifies a given share, it is split equally to 100. When some do, the
      remaining/missing entries are treated as 0.
    - Enforces that ``fleet_share`` and ``order_share`` each sum to 100 across all
      hauliers, raising ``ValueError`` otherwise (negatives are also rejected).
    - Always returns at least one haulier (falls back to the module default).
    """
    items = raw if isinstance(raw, (list, tuple)) else None
    if not items:
        return [dict(h) for h in HAULIERS]

    out = []
    seen_ids = set()
    for idx, entry in enumerate(items):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("id") or f"Haulier {idx + 1}").strip()
        hid = haulier_slug(entry.get("id") or name, fallback=f"haulier_{idx}")
        if hid in seen_ids:
            suffix = 1
            base = hid
            while f"{base}_{suffix}" in seen_ids:
                suffix += 1
            hid = f"{base}_{suffix}"
        seen_ids.add(hid)

        def _share(key):
            raw_val = entry.get(key)
            if raw_val is None:
                return None
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                return None
            if val < 0:
                raise ValueError(
                    f"Haulier '{hid}' has a negative {key} ({val:g}); percentages must be >= 0."
                )
            return val

        out.append(
            {
                "id": hid,
                "name": name or hid,
                "fleet_share": _share("fleet_share"),
                "order_share": _share("order_share"),
            }
        )

    if not out:
        return [dict(h) for h in HAULIERS]

    # Per share key: if nobody specified it, split equally to 100; otherwise the
    # unspecified entries are 0. Then enforce the percentages sum to 100.
    for key in ("fleet_share", "order_share"):
        if all(h[key] is None for h in out):
            equal = HAULIER_SHARE_TOTAL / len(out)
            for h in out:
                h[key] = equal
        else:
            for h in out:
                if h[key] is None:
                    h[key] = 0.0
        _validate_share_total(out, key)
    return out


def distribute_by_share(n, hauliers, share_key):
    """Deterministically allocate ``n`` agents to hauliers proportional to ``share_key``.

    Uses the largest-remainder method so the per-haulier counts sum exactly to ``n``
    and the assignment is stable for a given (n, hauliers) input. Returns a list of
    length ``n`` of haulier dicts, ordered so consecutive agents spread across
    hauliers rather than clustering (round-robin interleave of the quota blocks).
    """
    hauliers = normalize_hauliers(hauliers)
    n = max(0, int(n))
    if n == 0:
        return []
    if len(hauliers) == 1:
        return [hauliers[0]] * n

    weights = [max(0.0, float(h.get(share_key) or 0.0)) for h in hauliers]
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(hauliers)
        total = float(len(hauliers))

    raw = [w / total * n for w in weights]
    counts = [int(x) for x in raw]
    remainder = n - sum(counts)
    # Hand out the leftover slots to the largest fractional remainders.
    order = sorted(range(len(hauliers)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in range(remainder):
        counts[order[i % len(order)]] += 1

    # Interleave so early agents (e.g. warm-up orders) still span all hauliers.
    pools = [[hauliers[i]] * counts[i] for i in range(len(hauliers))]
    result = []
    while len(result) < n:
        for pool in pools:
            if pool:
                result.append(pool.pop())
                if len(result) == n:
                    break
    return result

from apps.container_logistics.duration_constants import (
    MAX_LEG_DROPOFF_SECONDS,
    MAX_LEG_PICKUP_SECONDS,
    MIN_HAUL_TRIP_SECONDS,
    MIN_LEG_DROPOFF_SECONDS,
    MIN_LEG_PICKUP_SECONDS,
)


def simulation_length_in_steps():
    """Total scheduler steps for the configured simulated calendar."""
    return (SIMULATION_DAYS * 24 * 3600) // STEP_INTERVAL_SECONDS


def seconds_to_simulation_steps(seconds):
    """Convert simulated seconds to scheduler step indices."""
    return int(seconds // STEP_INTERVAL_SECONDS)


def default_scenario_name():
    """Scenario folder name; bump SIMULATION_DAYS to get a fresh behavior dataset."""
    return f"default_container_logistics_{SIMULATION_DAYS}d"


def build_orsim_settings(domain=None, reference_time="2020-01-01 00:00:00"):
    """ORSim settings written to per-scenario orsim_settings.json."""
    from .runtime_settings import apply_long_run_orsim_settings

    settings = {
        "DOMAIN": domain or "UPDATE_DOMAIN_WHEN_GENERATING_BEHAVIOR",
        "SIMULATION_LENGTH_IN_STEPS": simulation_length_in_steps(),
        "STEP_INTERVAL": STEP_INTERVAL_SECONDS,
        "SIMULATION_DAYS": SIMULATION_DAYS,
        "AGENT_LAUNCH_TIMEOUT": 15,
        "STEP_TIMEOUT": 60,
        "STEP_TIMEOUT_TOLERANCE": 0.1,
        "HEARTBEAT_INTERVAL": 5,
        "REFERENCE_TIME": reference_time,
    }
    return apply_long_run_orsim_settings(settings)


truck_settings = {
    "num_trucks": NUM_TRUCKS,
    # Cadence de-synchronization (perf): we deliberately pick pairwise co-prime
    # steps_per_action across roles (truck=6, order=11, facility=5,
    # assignment=7, analytics=16). With identical cadences before, every
    # multiple of 6 hit the wall as trucks + facilities + assignment all
    # fired at once and step time spiked to ~9 s. Co-prime cadences smear
    # active work uniformly across all steps so the p95 wall comes down
    # without changing any per-role logic.
    "steps_per_action": 6,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "profile": {
        # Workflow lifecycle consumed by TruckAgent/TruckApp (seconds → steps in generate_behavior).
        "default_shift_start_seconds": 0,
        "default_shift_end_seconds": None,
        # HaulTrip StateMachine steering.
        "idle_strategy": "stay_if_no_assignment",
        "cancel_probability_when_assigned": 0.0,
        "cancel_probability_in_queue": 0.0,
        "truck_size": "20ft",  # 20ft, 40ft, 2x20ft
        # Per-truck haulier is assigned from the HAULIERS pool at generation time
        # (see GenerateBehavior); this fallback only applies if the pool is empty.
        "restricted_areas": ["West Coast", "MBS"],
        # Bounds for seeding profile estimated times (actual values live on each truck profile).
        "min_haul_trip_seconds": MIN_HAUL_TRIP_SECONDS,
        "min_estimated_time_to_pickup": MIN_LEG_PICKUP_SECONDS,
        "max_estimated_time_to_pickup": MAX_LEG_PICKUP_SECONDS,
        "min_estimated_time_to_dropoff": MIN_LEG_DROPOFF_SECONDS,
        "max_estimated_time_to_dropoff": MAX_LEG_DROPOFF_SECONDS,
        "use_osrm_at_assignment": False,
        "osrm_route_cache_max_entries": 2048,
    },
}

order_settings = {
    "num_orders": truck_settings["num_trucks"] * SIMULATION_DAYS * ORDERS_PER_TRUCK_PER_DAY,
    "early_order_count": EARLY_ORDER_COUNT,
    "order_demand_curve": None,
    "order_demand_weights": None,
    # Active orders observe haul-trip workflow events — they react via MQTT
    # rather than polling, so a coarser cadence is fine and keeps order
    # ticks off the truck/facility convergence schedule.
    "steps_per_action": 11,
    # Before launch (and after terminal states before shutdown), orders are
    # passive MQTT subscribers. A coarse cadence keeps their no-op ticks off
    # the hot path without changing when they enter the market.
    "dormant_steps_per_action": 48,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "business_hour_start": 0,
    "business_hour_end": 24,
    "profile": {
        "require_planned_routes": False,
        "cancel_probability_before_assignment": 0.0,
    },
}

# Keep in step with `datagen/defaults.py::FACILITY_SERVICE_TIME` — this is the LEGACY
# path, and it is the one that runs when a scenario's behaviours are regenerated at
# launch, so a divergence here silently ships a different world than a fresh compile.
_FACILITY_SERVICE_TIME = 600

_LOCATION_CATALOG = None


def _location_catalog():
    """Lazily-built real-address catalog (the isolated datagen sampler).

    Codes are discovered from the address book; EXCLUDED_CODES is the only filter,
    so a brand-new code in the CSV is picked up automatically.
    """
    global _LOCATION_CATALOG
    if _LOCATION_CATALOG is None:
        from apps.container_logistics.datagen.catalog import (
            LocationCatalog,
            default_locations_csv,
            default_sg_mask_path,
        )

        _LOCATION_CATALOG = LocationCatalog(
            default_locations_csv(), mask_path=default_sg_mask_path(), excluded=EXCLUDED_CODES
        )
    return _LOCATION_CATALOG


def _code_registry(trip_matrix=None):
    """CodeRegistry over the catalog's real codes, weighted by the given matrix's
    demand marginals (falls back to the observed DEFAULT_TRIP_MATRIX)."""
    from apps.container_logistics.datagen import (
        CodeRegistry,
        parse_trip_matrix,
        restrict_trip_matrix,
    )

    cat = _location_catalog()
    raw = trip_matrix or DEFAULT_TRIP_MATRIX
    try:
        matrix = restrict_trip_matrix(parse_trip_matrix(raw), cat.codes())
    except ValueError:
        matrix = restrict_trip_matrix(parse_trip_matrix(DEFAULT_TRIP_MATRIX), cat.codes())
    return CodeRegistry.build(cat.codes(), matrix, LOCATION_TYPE_METADATA)


def facilities_for_count(n: int, trip_matrix=None) -> list[dict]:
    """Return *n* facility site dicts spanning the real location codes, drawn from
    real on-land addresses. The per-type mix is demand-proportional (each code's
    share follows its marginal in ``trip_matrix``); customers are first-class and
    matrix codes are never starved. Selection is stable across regenerations.
    """
    return _location_catalog().facility_sites(
        max(1, int(n)),
        _code_registry(trip_matrix),
        gate_count=FACILITY_GATE_COUNT,
        service_time=_FACILITY_SERVICE_TIME,
    )


def _scalability_facilities():
    """Default-scenario facility set: real addresses across all active types."""
    return facilities_for_count(NUM_FACILITIES)


facility_settings = {
    "num_facilities": NUM_FACILITIES,
    # See truck_settings comment — co-prime with truck/assignment/analytics.
    "steps_per_action": 5,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "profile": {
        "publish_facility_stream_kafka": True,
        # REST persist every snapshot blocks Celery; Kafka stream is enough for the live map.
        "persist_facility_snapshots": False,
        "fifo_queue_policy": True,
        "gate_count": FACILITY_GATE_COUNT,
        "service_time": _FACILITY_SERVICE_TIME,
        "max_queue_size": None,
        "facility_type": "Depo",
        "status": "Open",
        "operating_hours": "24/7",
        "operating_days": "7 days a week",
        "facilities": _scalability_facilities(),
    },
}

assignment_settings = {
    # See truck_settings comment — co-prime with truck/facility/analytics.
    "steps_per_action": 28,
    "response_rate": 1.0,
    "step_only_on_events": False,
    "profile": {
        # Pluggable solver selection. ``strategy`` must match a key in
        # apps.container_logistics.assignment.solver.SOLVER_REGISTRY
        # ("RandomAssignment" | "GreedyNearest"). ``solver_params`` tunes the
        # chosen solver (greedy dual-cycle knobs below; ignored by random).
        "strategy": "RandomAssignment",
        "solver_params": {
            "dual_cycle_bonus_km": 5.0,
            "dual_cycle_radius_km": 0.5,
        },
        "max_travel_time_pickup": 7200,
        "online_metric_scale_strategy": "time",
        "respect_truck_online_state": True,
        "reject_if_active_haul_trip": True,
        "max_orders_per_tick": 500,
        "assignment_page_size": 500,
    },
}

analytics_settings = {
    # 48 steps @ 240s = ~3.2 sim hours per KPI tick (assignment co-prime: 28).
    "steps_per_action": 48,
    "response_rate": 1.0,
    "step_only_on_events": False,
    "profile": {
        "publish_realtime_data": False,
        "publish_trip_geo_kafka": True,
        "trip_geo_steps_per_action": 48,
        "write_ws_output_to_file": True,
        "publish_paths_history": False,
        "write_ph_output_to_file": False,
        "paths_history_time_window": 1800,
    },
}
