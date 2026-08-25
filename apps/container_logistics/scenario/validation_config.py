"""Phase 4: smoke scenario sizing and validation thresholds."""

from copy import deepcopy

from apps.container_logistics.duration_constants import MIN_HAUL_TRIP_SECONDS

from .runtime_settings import apply_long_run_orsim_settings
from .scenario_config import STEP_INTERVAL_SECONDS, build_orsim_settings, order_settings, truck_settings

SMOKE_SIMULATION_DAYS = 1
SMOKE_NUM_TRUCKS = 5
SMOKE_NUM_ORDERS = 50

# Behavior checks
MAX_ORDERS_AT_STEP_ZERO_FRACTION = 0.05
MIN_MODELED_HAUL_SECONDS = MIN_HAUL_TRIP_SECONDS
MIN_COMPLETED_HAUL_SAMPLE = 1
MIN_FRACTION_HAULS_ABOVE_MIN_DURATION = 0.95

# Post-run throughput (completed hauls per truck when querying a finished run)
MIN_COMPLETED_HAULS_PER_TRUCK = 1


def smoke_scenario_name() -> str:
    return f"smoke_container_logistics_{SMOKE_SIMULATION_DAYS}d"


def smoke_simulation_length_in_steps() -> int:
    return (SMOKE_SIMULATION_DAYS * 24 * 3600) // STEP_INTERVAL_SECONDS


def build_smoke_orsim_settings(domain=None, reference_time="2020-01-01 08:00:00"):
    settings = build_orsim_settings(domain=domain, reference_time=reference_time)
    settings["SIMULATION_DAYS"] = SMOKE_SIMULATION_DAYS
    settings["SIMULATION_LENGTH_IN_STEPS"] = smoke_simulation_length_in_steps()
    return apply_long_run_orsim_settings(settings)


def smoke_truck_settings():
    cfg = deepcopy(truck_settings)
    cfg["num_trucks"] = SMOKE_NUM_TRUCKS
    return cfg


def smoke_order_settings():
    cfg = deepcopy(order_settings)
    cfg["num_orders"] = SMOKE_NUM_ORDERS
    return cfg


def smoke_scenario_config_bundle():
    """Values patched into scenario_config during smoke behavior generation."""
    return {
        "SIMULATION_DAYS": SMOKE_SIMULATION_DAYS,
        "truck_settings": smoke_truck_settings(),
        "order_settings": smoke_order_settings(),
    }
