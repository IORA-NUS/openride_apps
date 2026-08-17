#!/usr/bin/env python3
"""
Offline + API stress checks for container logistics (Phases 1–4).

  PYTHONPATH=. venv/bin/python apps/simulation/stress_container_logistics.py
  PYTHONPATH=. venv/bin/python apps/simulation/stress_container_logistics.py --quick-sim-steps 120
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from apps.common.user_registry import UserRegistry
from apps.config import settings, simulation_domains
from apps.container_logistics.scenario.scenario_config import default_scenario_name
from apps.container_logistics.scenario.scenario_manager import ScenarioManager
from apps.container_logistics.scenario.simulation_validation import (
    ValidationCheck,
    ValidationReport,
    validate_behaviors,
    validate_completed_haul_trips,
    validate_run_from_api,
    validate_smoke_behaviors,
)
from apps.container_logistics.scenario.validation_config import smoke_scenario_name
from apps.utils import time_to_str
from datetime import datetime


def _datahub_dir():
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "datahub")
    )


def _admin_headers():
    return UserRegistry(
        time_to_str(datetime.now()),
        {"email": "sim_admin@test.com", "password": "password"},
        role="admin",
    ).get_headers()


def stress_behavior_generation(iterations: int = 20) -> ValidationReport:
    """Regenerate smoke behaviors repeatedly to catch config override races."""
    report = ValidationReport()
    domain = simulation_domains["container_logistics"]
    datahub = _datahub_dir()
    import shutil

    for i in range(iterations):
        name = f"stress_gen_{i:03d}"
        from apps.container_logistics.scenario.frontend_scenario_spec import (
            container_logistics_scenarios_root,
        )

        behavior_dir = os.path.join(container_logistics_scenarios_root(), name)
        if os.path.isdir(behavior_dir):
            shutil.rmtree(behavior_dir)
        mgr = ScenarioManager(datahub, name, domain=domain, generation_profile="smoke")
        mgr.generate_random_behaviors()
        sub = validate_smoke_behaviors(
            mgr.truck_collection, mgr.order_collection, mgr.orsim_settings
        )
        if not sub.passed:
            report.add(
                ValidationCheck(
                    name=f"behavior_generation_iter_{i}",
                    passed=False,
                    message=f"Iteration {i} failed",
                    details=sub.to_dict(),
                )
            )
            return report
    report.add(
        ValidationCheck(
            name="behavior_generation_stress",
            passed=True,
            message=f"{iterations} smoke generations passed validation",
        )
    )
    return report


def stress_synthetic_haul_samples(n: int = 500) -> ValidationReport:
    from apps.container_logistics.duration_constants import MIN_HAUL_TRIP_SECONDS
    from apps.container_logistics.haul_trip_duration import apply_haul_trip_duration_floors

    report = ValidationReport()
    trips = []
    for i in range(n):
        p, d = apply_haul_trip_duration_floors(
            60 + (i % 100),
            90 + (i % 100),
            order={"pickup_service_time": 120, "dropoff_service_time": 120},
        )
        trips.append(
            {
                "_id": f"synthetic_{i}",
                "stats": {
                    "estimated_time_to_pickup": p,
                    "estimated_time_to_dropoff": d,
                    "pickup_service_time": 120,
                    "dropoff_service_time": 120,
                },
            }
        )
    report.add(validate_completed_haul_trips(trips, min_sample=min(50, n)))
    report.add(
        ValidationCheck(
            name="synthetic_duration_floor",
            passed=all(
                t["stats"]["estimated_time_to_pickup"] + t["stats"]["estimated_time_to_dropoff"] + 240
                >= MIN_HAUL_TRIP_SECONDS
                for t in trips
            ),
            message=f"{n} synthetic haul ETAs respect duration floors",
        )
    )
    return report


def stress_api_health() -> ValidationReport:
    import requests

    report = ValidationReport()
    base = settings["OPENRIDE_SERVER_URL"]
    try:
        r = requests.get(f"{base}/", timeout=5)
        report.add(
            ValidationCheck(
                name="api_reachable",
                passed=r.status_code in (200, 401, 404),
                message=f"GET / -> {r.status_code}",
            )
        )
    except Exception as exc:
        report.add(
            ValidationCheck(name="api_reachable", passed=False, message=str(exc))
        )
        return report

    headers = _admin_headers()
    domain = simulation_domains["container_logistics"]
    run_id = None
    try:
        r = requests.get(f"{base}/run-config", headers=headers, params={"max_results": 1}, timeout=15)
        if r.status_code == 200:
            run_id = ((r.json() or {}).get("_items") or [{}])[0].get("run_id")
    except Exception:
        pass

    paths = ["/run-config", f"/kpis?ecosystem=container_logistics"]
    if run_id:
        paths.append(f"/{domain}/{run_id}/truck")
        paths.append(f"/{domain}/{run_id}/truck/trip")
    for path in paths:
        try:
            r = requests.get(f"{base}{path}", headers=headers, timeout=15)
            ok = r.status_code in (200, 201)
            report.add(
                ValidationCheck(
                    name=f"api_get_{path.replace('/', '_')}",
                    passed=ok,
                    message=f"GET {path} -> {r.status_code}",
                    details={"body_sample": (r.text or "")[:200]},
                )
            )
        except Exception as exc:
            report.add(
                ValidationCheck(name=f"api_get_{path}", passed=False, message=str(exc))
            )
    return report


def run_quick_simulation(max_steps: int) -> tuple[str | None, ValidationReport]:
    """Run smoke scenario for max_steps (patch horizon) — requires RabbitMQ + API."""
    from apps.simulation.container_logistics_wiring import (
        build_scheduler_config,
        get_agent_config,
        get_datahub_dir,
        get_domain,
        get_statemachine_collection,
        new_run_id,
    )
    from apps.simulation.simulation_runtime import SimulationRuntime

    report = ValidationReport()
    domain = get_domain()
    datahub = get_datahub_dir()
    mgr = ScenarioManager(
        datahub,
        smoke_scenario_name(),
        domain=domain,
        generation_profile="smoke",
    )
    if not mgr.behaviors_exist_on_disk():
        mgr.generate_random_behaviors()

    original_steps = mgr.orsim_settings["SIMULATION_LENGTH_IN_STEPS"]
    mgr.orsim_settings["SIMULATION_LENGTH_IN_STEPS"] = min(max_steps, original_steps)

    run_id = new_run_id("stress")
    try:
        sim = SimulationRuntime(
            run_id=run_id,
            scenario_manager=mgr,
            datahub_dir=datahub,
            domain=domain,
            agent_config=get_agent_config(),
            statemachine_collection=get_statemachine_collection(),
            scheduler_config=build_scheduler_config(run_id, mgr.orsim_settings),
        )
        t0 = time.time()
        sim.run_simulation()
        elapsed = time.time() - t0
        report.add(
            ValidationCheck(
                name="quick_simulation_run",
                passed=True,
                message=(
                    f"Completed {mgr.orsim_settings['SIMULATION_LENGTH_IN_STEPS']} steps "
                    f"in {elapsed:.1f}s wall"
                ),
                details={"run_id": run_id, "wall_seconds": elapsed},
            )
        )
        return run_id, report
    except Exception as exc:
        report.add(
            ValidationCheck(name="quick_simulation_run", passed=False, message=str(exc))
        )
        return None, report


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stress test container logistics stack")
    parser.add_argument("--iterations", type=int, default=10, help="Behavior generation iterations")
    parser.add_argument("--synthetic-hauls", type=int, default=500)
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--skip-sim", action="store_true")
    parser.add_argument("--quick-sim-steps", type=int, default=0, help="Run N scheduler steps (needs API+RabbitMQ)")
    parser.add_argument("--validate-run-id", default=None)
    args = parser.parse_args(argv)

    combined = ValidationReport()

    domain = simulation_domains["container_logistics"]
    mgr7 = ScenarioManager(_datahub_dir(), default_scenario_name(), domain=domain)
    if mgr7.behaviors_exist_on_disk():
        sub7 = validate_behaviors(
            mgr7.truck_collection, mgr7.order_collection, mgr7.orsim_settings
        )
        for c in sub7.checks:
            combined.add(c)
    else:
        combined.add(
            ValidationCheck(
                name="production_behaviors",
                passed=True,
                message=f"No cached {default_scenario_name()} (generate before 7d run)",
            )
        )

    smoke_mgr = ScenarioManager(
        _datahub_dir(), smoke_scenario_name(), domain=domain, generation_profile="smoke"
    )
    if not smoke_mgr.behaviors_exist_on_disk():
        smoke_mgr.generate_random_behaviors()
    for c in validate_smoke_behaviors(
        smoke_mgr.truck_collection, smoke_mgr.order_collection, smoke_mgr.orsim_settings
    ).checks:
        combined.add(c)

    for c in stress_synthetic_haul_samples(args.synthetic_hauls).checks:
        combined.add(c)

    for c in stress_behavior_generation(args.iterations).checks:
        combined.add(c)

    if not args.skip_api:
        for c in stress_api_health().checks:
            combined.add(c)

    run_id = args.validate_run_id
    if args.quick_sim_steps > 0 and not args.skip_sim:
        run_id, sim_report = run_quick_simulation(args.quick_sim_steps)
        for c in sim_report.checks:
            combined.add(c)

    if run_id and not args.skip_api:
        for c in validate_run_from_api(
            run_id,
            _admin_headers(),
            smoke_mgr.truck_collection,
            smoke_mgr.orsim_settings,
            simulation_domain=domain,
        ).checks:
            combined.add(c)

    combined.print_summary()
    return 0 if combined.passed else 1


if __name__ == "__main__":
    sys.exit(main())
