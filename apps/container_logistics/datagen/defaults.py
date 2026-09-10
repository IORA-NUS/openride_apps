"""Container-logistics generation defaults — dissolved out of ``scenario_config``.

These are the *generation* half of the old ``scenario_config`` module, now owned by
datagen as **immutable data** (no mutable module globals, no override contextmanager).
The :class:`~.preprocess.Preprocessor` reads them to fill everything ``spec.json``
omits. A parity test (``tests/test_datagen_defaults_parity.py``) asserts these match
``scenario_config`` during the transition so nothing drifts.

Everything here is a *default*; the spec overrides what it names.
"""

from __future__ import annotations

from copy import deepcopy

from apps.container_logistics.duration_constants import (
    MAX_LEG_DROPOFF_SECONDS,
    MAX_LEG_PICKUP_SECONDS,
    MIN_HAUL_TRIP_SECONDS,
    MIN_LEG_DROPOFF_SECONDS,
    MIN_LEG_PICKUP_SECONDS,
)

# ---- calendar ----------------------------------------------------------------
SIMULATION_DAYS = 7
STEP_INTERVAL_SECONDS = 240
NUM_TRUCKS = 500
ORDERS_PER_TRUCK_PER_DAY = 10
NUM_FACILITIES = 15
FACILITY_GATE_COUNT = 1
# Gate service time per truck visit, seconds. 600 s (10 min) since 2026-08-27.
# At the previous 1800 s a single-gate facility could serve only 604800/1800 = 336
# visits over a 7-day run, while `consortium_collab_7d`'s busiest port needed 446 —
# 33% oversubscribed, so its queue never reached steady state (peak 203 trucks,
# 61 h average wait). 600 s lifts one gate to 1008 visits, clear of that demand.
# NOTE: this is WORLD PHYSICS — runs before and after are NOT comparable (CLAUDE.md
# §6.16), and it also shortens haul durations on the order side, not just gate
# occupancy (see tests/test_order_service_time_path.py).
FACILITY_SERVICE_TIME = 600
BEHAVIOR_REVISION = 10
EARLY_ORDER_COUNT = 64

# ---- seed --------------------------------------------------------------------
# Fixed default so generation is reproducible (plan §4.6 / Q6).
DEFAULT_SEED = 20260712

# ---- location / matrix defaults ---------------------------------------------
DEFAULT_TRIP_MATRIX = {
    "CT": {"CT": 0, "CU": 15, "MT": 4},
    "CU": {"CT": 11, "CU": 0, "MT": 14},
    "MT": {"CT": 3, "CU": 14, "MT": 0},
}

LOCATION_TYPE_METADATA = {
    "CT": {"label": "Port", "prefix": "port", "mask_file": "port_regions_cleaned.geojson"},
    "CU": {"label": "Warehouse", "prefix": "customer"},
    "MT": {"label": "Depot", "prefix": "depot", "mask_file": "depot_regions_cleaned.geojson"},
}

EXCLUDED_CODES = ("YD",)
TRUCK_ORIGIN_CODES = None

# ---- runtime tuning (orsim) --------------------------------------------------
# Derived-from-calendar orsim settings + host-tuning block (plan §4.2/Q7). The
# Preprocessor merges the calendar-derived fields on top of these.
ORSIM_RUNTIME_TUNING = {
    "AGENT_LAUNCH_TIMEOUT": 15,
    "STEP_TIMEOUT": 60,
    "STEP_TIMEOUT_TOLERANCE": 0.1,
    "HEARTBEAT_INTERVAL": 5,
}

# ---- per-role generation profiles (the old *_settings, minus mutable count) --
TRUCK_SETTINGS = {
    "num_trucks": NUM_TRUCKS,
    "steps_per_action": 6,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "profile": {
        "default_shift_start_seconds": 0,
        "default_shift_end_seconds": None,
        "idle_strategy": "stay_if_no_assignment",
        "cancel_probability_when_assigned": 0.0,
        "cancel_probability_in_queue": 0.0,
        "truck_size": "20ft",
        "restricted_areas": ["West Coast", "MBS"],
        "min_haul_trip_seconds": MIN_HAUL_TRIP_SECONDS,
        "min_estimated_time_to_pickup": MIN_LEG_PICKUP_SECONDS,
        "max_estimated_time_to_pickup": MAX_LEG_PICKUP_SECONDS,
        "min_estimated_time_to_dropoff": MIN_LEG_DROPOFF_SECONDS,
        "max_estimated_time_to_dropoff": MAX_LEG_DROPOFF_SECONDS,
        "use_osrm_at_assignment": False,
        "osrm_route_cache_max_entries": 2048,
    },
}

ORDER_SETTINGS = {
    "num_orders": NUM_TRUCKS * SIMULATION_DAYS * ORDERS_PER_TRUCK_PER_DAY,
    "early_order_count": EARLY_ORDER_COUNT,
    "order_demand_curve": None,
    "order_demand_weights": None,
    "steps_per_action": 11,
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

FACILITY_SETTINGS = {
    "num_facilities": NUM_FACILITIES,
    "steps_per_action": 5,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "profile": {
        "publish_facility_stream_kafka": True,
        "persist_facility_snapshots": False,
        "fifo_queue_policy": True,
        "gate_count": FACILITY_GATE_COUNT,
        "service_time": FACILITY_SERVICE_TIME,
        "max_queue_size": None,
        "facility_type": "Depo",
        "status": "Open",
        "operating_hours": "24/7",
        "operating_days": "7 days a week",
        # facilities list is filled per-scenario by the Preprocessor (sites policy).
    },
}

ASSIGNMENT_SETTINGS = {
    "steps_per_action": 28,
    "response_rate": 1.0,
    "step_only_on_events": False,
    "profile": {
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

ANALYTICS_SETTINGS = {
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


def role_settings() -> dict[str, dict]:
    """Fresh deep copies of the five role setting blocks (never share state)."""
    return {
        "truck": deepcopy(TRUCK_SETTINGS),
        "order": deepcopy(ORDER_SETTINGS),
        "facility": deepcopy(FACILITY_SETTINGS),
        "assignment": deepcopy(ASSIGNMENT_SETTINGS),
        "analytics": deepcopy(ANALYTICS_SETTINGS),
    }


def simulation_length_in_steps(days: int = SIMULATION_DAYS, step_interval: int = STEP_INTERVAL_SECONDS) -> int:
    return (int(days) * 24 * 3600) // int(step_interval)
