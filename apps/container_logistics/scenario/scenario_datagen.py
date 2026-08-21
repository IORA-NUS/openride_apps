"""Adapter: resolve a :class:`GenerationSpec` from ``scenario_config``.

This is the *only* bridge between the (possibly frontend/smoke-overridden)
scenario configuration and the isolated ``datagen`` package. All reads of module
globals happen here, once, and are frozen into the immutable spec — ``datagen``
itself stays pure.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

from apps.container_logistics.datagen import (
    GenerationSpec,
    default_locations_csv,
    default_sg_mask_path,
    parse_trip_matrix,
    restrict_trip_matrix,
)
from apps.container_logistics.datagen.builders import resolve_facilities

from . import scenario_config
from .order_demand import hourly_weights_from_spec


def _hourly_weights(order_settings: dict) -> Optional[list]:
    weights = order_settings.get("order_demand_weights")
    if weights:
        return list(weights)
    curve = order_settings.get("order_demand_curve")
    if curve:
        return hourly_weights_from_spec(curve)
    return None


def _trip_matrix(order_settings: dict) -> dict:
    """Resolve + normalize the trip matrix: the scenario's own (frontend-supplied)
    one, else the config-layer observed default — restricted to codes that have
    real addresses (drops excluded/unknown codes like YD). datagen holds no
    default; the matrix is always provided here."""
    codes = scenario_config._location_catalog().codes()
    raw = order_settings.get("trip_matrix") or scenario_config.DEFAULT_TRIP_MATRIX
    try:
        return restrict_trip_matrix(parse_trip_matrix(raw), codes)
    except ValueError:
        return restrict_trip_matrix(
            parse_trip_matrix(scenario_config.DEFAULT_TRIP_MATRIX), codes
        )


def _truck_origin_codes() -> Optional[tuple]:
    codes = getattr(scenario_config, "TRUCK_ORIGIN_CODES", None)
    return tuple(codes) if codes else None


def _refuse_facility_rules() -> None:
    """Raise when the active scenario_config carries a non-empty rule list.

    Reached only when ``frontend_scenario_config_override`` stamped one on, i.e. when
    a rules-carrying scenario is regenerating on the legacy path. A rules-less
    scenario is untouched, which matters: 13 of the 15 shipped scenarios carry none.
    """
    rules = getattr(scenario_config, "FACILITY_RULES", None)
    if not rules:
        return
    raise RuntimeError(
        f"This scenario carries {len(rules)} facilityRules, and the legacy "
        f"GenerateBehavior path cannot honour them.\n"
        f"Compiling here would produce a complete, internally consistent, RULES-FREE "
        f"world — every per-facility gate_count/service_time/rebate silently reverted "
        f"to the scenario-wide blanket layer, with nothing in the bundle saying so.\n"
        f"Use the Preprocessor path (`openride scenario compile <slug>`), which is the "
        f"single validation surface and the only implementation of rule precedence.\n"
        f"Rules: {[r.get('match') for r in rules if isinstance(r, dict)]}"
    )


def build_generation_spec(
    domain: str,
    *,
    orsim_settings: Optional[dict] = None,
    counts: Optional[dict] = None,
    generation_spec_meta: Optional[dict] = None,
    reference_time: Optional[str] = None,
) -> GenerationSpec:
    """Read the current scenario_config (post-override) into a frozen spec.

    **Refuses a rules-carrying scenario (R2-2 / F3).** This is the LEGACY generation
    path; it has no facilityRules implementation and must not acquire one — two
    implementations of a precedence rule is how they diverge. It raises rather than
    warns because the alternative outcome is the most expensive one available: a
    complete, internally consistent, RULES-FREE world, silently reverting every
    per-facility value to the blanket layer, for a feature that exists precisely
    because those values were unexpressible. Eight call sites reach GenerateBehavior
    and every one of them is better off failing loudly.
    """
    _refuse_facility_rules()
    truck_settings = deepcopy(scenario_config.truck_settings)
    order_settings = deepcopy(scenario_config.order_settings)
    facility_settings = deepcopy(scenario_config.facility_settings)
    assignment_settings = deepcopy(scenario_config.assignment_settings)
    analytics_settings = deepcopy(scenario_config.analytics_settings)

    facilities = resolve_facilities(facility_settings)
    if counts:
        truck_n = int(counts.get("trucks", truck_settings.get("num_trucks", 1)))
        order_n = int(counts.get("orders", order_settings.get("num_orders", 1)))
        facility_n = int(counts.get("facilities", len(facilities)))
    else:
        truck_n = int(truck_settings.get("num_trucks", 1))
        order_n = int(order_settings.get("num_orders", 1))
        facility_n = len(facilities)

    hauliers = scenario_config.normalize_hauliers(getattr(scenario_config, "HAULIERS", None))
    truck_hauliers = tuple(
        scenario_config.distribute_by_share(max(1, truck_n), hauliers, "fleet_share")
    )
    order_hauliers = tuple(
        scenario_config.distribute_by_share(max(1, order_n), hauliers, "order_share")
    )

    ref_time = (
        reference_time
        or (orsim_settings or {}).get("REFERENCE_TIME")
        or "2020-01-01 08:00:00"
    )

    return GenerationSpec(
        domain=domain,
        num_trucks=truck_n,
        num_orders=order_n,
        num_facilities=facility_n,
        simulation_days=int(scenario_config.SIMULATION_DAYS),
        step_interval_seconds=int(scenario_config.STEP_INTERVAL_SECONDS),
        simulation_length_in_steps=int(scenario_config.simulation_length_in_steps()),
        reference_time=ref_time,
        behavior_revision=int(scenario_config.BEHAVIOR_REVISION),
        truck_settings=truck_settings,
        order_settings=order_settings,
        facility_settings=facility_settings,
        assignment_settings=assignment_settings,
        analytics_settings=analytics_settings,
        truck_hauliers=truck_hauliers,
        order_hauliers=order_hauliers,
        hourly_weights=_hourly_weights(order_settings),
        business_hour_start=int(order_settings.get("business_hour_start", 0)),
        business_hour_end=int(order_settings.get("business_hour_end", 24)),
        trip_matrix=_trip_matrix(order_settings),
        excluded_codes=tuple(getattr(scenario_config, "EXCLUDED_CODES", ())),
        truck_origin_codes=_truck_origin_codes(),
        locations_csv=default_locations_csv(),
        sg_mask_path=default_sg_mask_path(),
        orsim_settings=orsim_settings,
        generation_spec_meta=generation_spec_meta,
    )
