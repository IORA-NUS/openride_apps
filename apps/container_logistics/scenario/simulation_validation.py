"""Phase 4: validate generated behaviors and completed simulation runs."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Dict, List, Optional, Sequence

from apps.container_logistics.duration_constants import MIN_HAUL_TRIP_SECONDS
from apps.container_logistics.haul_trip_duration import modeled_haul_duration_seconds
from apps.container_logistics.scenario.scenario_config import STEP_INTERVAL_SECONDS, order_settings

from .validation_config import (
    MAX_ORDERS_AT_STEP_ZERO_FRACTION,
    MIN_COMPLETED_HAUL_SAMPLE,
    MIN_COMPLETED_HAULS_PER_TRUCK,
    MIN_FRACTION_HAULS_ABOVE_MIN_DURATION,
    MIN_MODELED_HAUL_SECONDS,
    SMOKE_NUM_ORDERS,
    SMOKE_NUM_TRUCKS,
)

logger = logging.getLogger(__name__)


@dataclass
class ValidationCheck:
    name: str
    passed: bool
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    checks: List[ValidationCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, check: ValidationCheck) -> None:
        self.checks.append(check)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
        }

    def print_summary(self) -> None:
        for check in self.checks:
            status = "PASS" if check.passed else "FAIL"
            print(f"[{status}] {check.name}: {check.message}")
        print(f"\nOverall: {'PASSED' if self.passed else 'FAILED'}")


def _simulation_end_step(orsim_settings: Dict[str, Any]) -> int:
    steps = int(orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 1))
    return max(0, steps - 1)


def _expected_horizon_seconds(orsim_settings: Dict[str, Any]) -> float:
    steps = int(orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0))
    interval = int(orsim_settings.get("STEP_INTERVAL", STEP_INTERVAL_SECONDS))
    return steps * interval


def _order_in_business_hours(request_step: int, orsim_settings: Dict[str, Any]) -> bool:
    if order_settings.get("request_time_step_min") is not None:
        return True
    interval = int(orsim_settings.get("STEP_INTERVAL", STEP_INTERVAL_SECONDS))
    steps_per_day = (24 * 3600) // interval
    step_in_day = request_step % steps_per_day
    seconds_in_day = step_in_day * interval
    hour = seconds_in_day // 3600
    bh_start = int(order_settings.get("business_hour_start", 6))
    bh_end = int(order_settings.get("business_hour_end", 22))
    return bh_start <= hour < bh_end


def validate_simulation_horizon(orsim_settings: Dict[str, Any]) -> ValidationCheck:
    sim_days = orsim_settings.get("SIMULATION_DAYS")
    steps = int(orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0))
    interval = int(orsim_settings.get("STEP_INTERVAL", STEP_INTERVAL_SECONDS))
    expected_steps = None
    if sim_days is not None:
        expected_steps = (int(sim_days) * 24 * 3600) // interval
    ok = expected_steps is None or steps == expected_steps
    return ValidationCheck(
        name="simulation_horizon",
        passed=ok,
        message=(
            f"SIMULATION_LENGTH_IN_STEPS={steps} matches {sim_days} day(s) at {interval}s/step"
            if ok
            else f"expected {expected_steps} steps for {sim_days} day(s), got {steps}"
        ),
        details={"steps": steps, "simulation_days": sim_days, "step_interval": interval},
    )


def validate_truck_shifts(
    truck_collection: Dict[str, Any],
    orsim_settings: Dict[str, Any],
) -> ValidationCheck:
    end_step = _simulation_end_step(orsim_settings)
    violations = []
    for agent_id, behavior in truck_collection.items():
        start = int(behavior.get("shift_start_time", 0))
        end = int(behavior.get("shift_end_time", end_step))
        if start > end:
            violations.append(f"{agent_id}: shift_start {start} > shift_end {end}")
        if end > end_step:
            violations.append(f"{agent_id}: shift_end {end} > sim_end {end_step}")
    return ValidationCheck(
        name="truck_shifts",
        passed=not violations,
        message="All truck shifts within simulation horizon"
        if not violations
        else f"{len(violations)} shift bound violation(s)",
        details={"violations": violations[:20]},
    )


def validate_order_request_times(
    order_collection: Dict[str, Any],
    orsim_settings: Dict[str, Any],
) -> ValidationCheck:
    end_step = _simulation_end_step(orsim_settings)
    out_of_range = []
    outside_hours = []
    at_step_zero = 0
    for agent_id, behavior in order_collection.items():
        step = int(behavior.get("request_time_step", 0))
        if step == 0:
            at_step_zero += 1
        if step < 0 or step > end_step:
            out_of_range.append(agent_id)
        if not _order_in_business_hours(step, orsim_settings):
            outside_hours.append(agent_id)
    n = max(1, len(order_collection))
    zero_frac = at_step_zero / n
    ok = (
        not out_of_range
        and not outside_hours
        and zero_frac <= MAX_ORDERS_AT_STEP_ZERO_FRACTION
    )
    return ValidationCheck(
        name="order_request_times",
        passed=ok,
        message=(
            f"{len(order_collection)} orders spread in [0,{end_step}] "
            f"(step_zero={at_step_zero}, {zero_frac:.1%})"
            if ok
            else "Order request time distribution failed checks"
        ),
        details={
            "out_of_range": out_of_range[:10],
            "outside_business_hours": outside_hours[:10],
            "at_step_zero": at_step_zero,
            "max_zero_fraction": MAX_ORDERS_AT_STEP_ZERO_FRACTION,
        },
    )


def validate_truck_profile_trip_durations(
    truck_collection: Dict[str, Any],
) -> ValidationCheck:
    violations = []
    for agent_id, behavior in truck_collection.items():
        profile = behavior.get("profile") or {}
        trip = {
            "stats": {
                "estimated_time_to_pickup": profile.get("estimated_time_to_pickup"),
                "estimated_time_to_dropoff": profile.get("estimated_time_to_dropoff"),
                "pickup_service_time": 120,
                "dropoff_service_time": 120,
            }
        }
        modeled = modeled_haul_duration_seconds(trip, profile=profile)
        if modeled < MIN_MODELED_HAUL_SECONDS:
            violations.append({"agent_id": agent_id, "modeled_seconds": modeled})
    return ValidationCheck(
        name="truck_profile_trip_duration",
        passed=not violations,
        message=(
            f"All truck profiles model >= {MIN_MODELED_HAUL_SECONDS}s hauls"
            if not violations
            else f"{len(violations)} truck(s) below minimum modeled duration"
        ),
        details={"violations": violations[:20]},
    )


def validate_agent_counts(
    truck_collection: Dict[str, Any],
    order_collection: Dict[str, Any],
    *,
    expected_trucks: Optional[int] = None,
    expected_orders: Optional[int] = None,
) -> ValidationCheck:
    nt = len(truck_collection)
    no = len(order_collection)
    ok = True
    msgs = []
    if expected_trucks is not None and nt != expected_trucks:
        ok = False
        msgs.append(f"trucks expected {expected_trucks}, got {nt}")
    if expected_orders is not None and no != expected_orders:
        ok = False
        msgs.append(f"orders expected {expected_orders}, got {no}")
    return ValidationCheck(
        name="agent_counts",
        passed=ok,
        message="Agent counts match expected" if ok else "; ".join(msgs),
        details={"num_trucks": nt, "num_orders": no},
    )


def validate_behaviors(
    truck_collection: Dict[str, Any],
    order_collection: Dict[str, Any],
    orsim_settings: Dict[str, Any],
    *,
    expected_trucks: Optional[int] = None,
    expected_orders: Optional[int] = None,
) -> ValidationReport:
    report = ValidationReport()
    report.add(validate_simulation_horizon(orsim_settings))
    report.add(
        validate_agent_counts(
            truck_collection,
            order_collection,
            expected_trucks=expected_trucks,
            expected_orders=expected_orders,
        )
    )
    report.add(validate_truck_shifts(truck_collection, orsim_settings))
    report.add(validate_order_request_times(order_collection, orsim_settings))
    report.add(validate_truck_profile_trip_durations(truck_collection))
    return report


def validate_completed_haul_trips(
    haul_trips: Sequence[Dict[str, Any]],
    *,
    min_seconds: float = MIN_MODELED_HAUL_SECONDS,
    min_sample: int = MIN_COMPLETED_HAUL_SAMPLE,
    min_fraction_above_min: float = MIN_FRACTION_HAULS_ABOVE_MIN_DURATION,
) -> ValidationCheck:
    if len(haul_trips) < min_sample:
        return ValidationCheck(
            name="completed_haul_trip_duration",
            passed=False,
            message=f"Need at least {min_sample} completed haul(s), got {len(haul_trips)}",
            details={"count": len(haul_trips)},
        )
    durations = []
    below = []
    for trip in haul_trips:
        profile = (trip.get("meta") or {}).get("truck_profile") or {}
        seconds = modeled_haul_duration_seconds(trip, profile=profile)
        durations.append(seconds)
        if seconds < min_seconds:
            below.append({"trip_id": trip.get("_id"), "modeled_seconds": seconds})
    durations.sort()
    above_frac = (len(durations) - len(below)) / len(durations)
    p50 = median(durations)
    p95_idx = min(len(durations) - 1, int(0.95 * (len(durations) - 1)))
    p95 = durations[p95_idx]
    ok = above_frac >= min_fraction_above_min and p50 >= min_seconds
    return ValidationCheck(
        name="completed_haul_trip_duration",
        passed=ok,
        message=(
            f"{len(durations)} hauls: p50={p50:.0f}s p95={p95:.0f}s "
            f">={min_seconds}s: {above_frac:.1%}"
            if ok
            else f"Too many hauls below {min_seconds}s ({len(below)}/{len(durations)})",
        ),
        details={
            "p50_seconds": p50,
            "p95_seconds": p95,
            "below_minimum": below[:20],
            "min_seconds": min_seconds,
        },
    )


def validate_run_throughput(
    haul_trips: Sequence[Dict[str, Any]],
    truck_collection: Dict[str, Any],
    *,
    min_hauls_per_truck: float = MIN_COMPLETED_HAULS_PER_TRUCK,
) -> ValidationCheck:
    num_trucks = max(1, len(truck_collection))
    min_total = int(min_hauls_per_truck * num_trucks)
    total_ok = len(haul_trips) >= min_total
    distinct_trucks = len({t.get("truck") for t in haul_trips if t.get("truck") is not None})
    return ValidationCheck(
        name="run_throughput",
        passed=total_ok,
        message=(
            f"{len(haul_trips)} completed hauls across {distinct_trucks} truck(s) "
            f"(need >={min_total} for {num_trucks} trucks)"
            if total_ok
            else f"Only {len(haul_trips)} completed hauls (need >={min_total})"
        ),
        details={
            "completed_hauls": len(haul_trips),
            "distinct_trucks_with_hauls": distinct_trucks,
            "num_trucks": num_trucks,
        },
    )


def validate_reference_time_horizon(
    orsim_settings: Dict[str, Any],
    *,
    last_sim_clock: Optional[str] = None,
) -> ValidationCheck:
    ref = orsim_settings.get("REFERENCE_TIME")
    if not ref or not last_sim_clock:
        return ValidationCheck(
            name="reference_time_horizon",
            passed=True,
            message="Skipped (missing REFERENCE_TIME or last sim_clock)",
        )
    from apps.utils import str_to_time

    try:
        ref_dt = datetime.strptime(ref, "%Y-%m-%d %H:%M:%S")
        end_dt = str_to_time(last_sim_clock)
        expected_end = ref_dt + timedelta(seconds=_expected_horizon_seconds(orsim_settings))
        # Allow one step tolerance
        tolerance = timedelta(seconds=int(orsim_settings.get("STEP_INTERVAL", STEP_INTERVAL_SECONDS)))
        ok = end_dt >= expected_end - tolerance
        return ValidationCheck(
            name="reference_time_horizon",
            passed=ok,
            message=(
                f"Last sim clock {last_sim_clock} reaches ~{orsim_settings.get('SIMULATION_DAYS')} day horizon"
                if ok
                else f"Last sim clock {last_sim_clock} before expected {expected_end}"
            ),
            details={"reference_time": ref, "expected_end": expected_end.isoformat()},
        )
    except Exception as exc:
        return ValidationCheck(
            name="reference_time_horizon",
            passed=False,
            message=f"Could not parse times: {exc}",
        )


def fetch_completed_haul_trips(
    run_id: str,
    headers: Dict[str, str],
    *,
    simulation_domain: str,
) -> List[Dict[str, Any]]:
    import requests

    from apps.config import settings
    from apps.container_logistics.statemachine import HaulTripStateMachine

    url = f"{settings['OPENRIDE_SERVER_URL']}/{simulation_domain}/{run_id}/truck/trip"
    items = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=headers,
            params={
                "where": json.dumps(
                    {
                        "$and": [
                            {"run_id": run_id},
                            {"state": HaulTripStateMachine.completed.name},
                        ]
                    }
                ),
                "page": page,
                "max_results": 50,
            },
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 30),
        )
        resp.raise_for_status()
        batch = (resp.json() or {}).get("_items") or []
        if not batch:
            break
        items.extend(batch)
        page += 1
    return items


def validate_run_from_api(
    run_id: str,
    headers: Dict[str, str],
    truck_collection: Dict[str, Any],
    orsim_settings: Dict[str, Any],
    *,
    simulation_domain: str,
) -> ValidationReport:
    import requests

    from apps.config import settings
    from apps.container_logistics.statemachine import OrderStateMachine

    report = ValidationReport()
    try:
        haul_trips = fetch_completed_haul_trips(run_id, headers, simulation_domain=simulation_domain)
    except Exception as exc:
        report.add(
            ValidationCheck(
                name="fetch_completed_hauls",
                passed=False,
                message=f"Failed to fetch haul trips: {exc}",
            )
        )
        return report

    report.add(validate_completed_haul_trips(haul_trips))
    report.add(validate_run_throughput(haul_trips, truck_collection))
    last_clock = None
    if haul_trips:
        clocks = [t.get("sim_clock") for t in haul_trips if t.get("sim_clock")]
        if clocks:
            last_clock = max(clocks)
    report.add(validate_reference_time_horizon(orsim_settings, last_sim_clock=last_clock))

    orders_url = f"{settings['OPENRIDE_SERVER_URL']}/{simulation_domain}/{run_id}/order"
    try:
        resp = requests.get(
            orders_url,
            headers=headers,
            params={"where": json.dumps({"state": OrderStateMachine.completed.name}), "max_results": 500},
        )
        if resp.status_code == 200:
            embedded = (resp.json() or {}).get("_embedded") or {}
            completed_orders = embedded.get("order") or []
            report.add(
                ValidationCheck(
                    name="orders_completed",
                    passed=len(completed_orders) > 0,
                    message=f"{len(completed_orders)} order(s) completed",
                    details={"count": len(completed_orders)},
                )
            )
    except Exception as exc:
        logger.warning("orders_completed check skipped: %s", exc)
    return report


def validate_smoke_behaviors(
    truck_collection: Dict[str, Any],
    order_collection: Dict[str, Any],
    orsim_settings: Dict[str, Any],
) -> ValidationReport:
    return validate_behaviors(
        truck_collection,
        order_collection,
        orsim_settings,
        expected_trucks=SMOKE_NUM_TRUCKS,
        expected_orders=SMOKE_NUM_ORDERS,
    )
