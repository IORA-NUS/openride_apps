#!/usr/bin/env python3
"""Phase 4: validate container logistics behaviors and/or a completed simulation run."""

import argparse
import json
import logging
import os
import sys

from apps.common.user_registry import UserRegistry
from apps.config import simulation_domains
from apps.container_logistics.scenario.scenario_manager import ScenarioManager
from apps.container_logistics.scenario.simulation_validation import (
    ValidationCheck,
    ValidationReport,
    validate_behaviors,
    validate_run_from_api,
    validate_smoke_behaviors,
)
from apps.container_logistics.scenario.validation_config import smoke_scenario_name
from apps.utils import time_to_str
from datetime import datetime


def _default_datahub_dir():
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "datahub")
    )


def _admin_headers():
    user = UserRegistry(
        time_to_str(datetime.now()),
        {"email": "sim_admin@test.com", "password": "password"},
        role="admin",
    )
    return user.get_headers()


def validate_behaviors_on_disk(datahub_dir, scenario_name, domain, *, smoke: bool) -> ValidationReport:
    manager = ScenarioManager(
        datahub_dir,
        scenario_name,
        domain=domain,
        generation_profile="smoke" if smoke else None,
    )
    if not manager.behaviors_exist_on_disk():
        report = ValidationReport()
        report.add(
            ValidationCheck(
                name="behaviors_exist",
                passed=False,
                message=f"No behaviors under {manager.behavior_dir}",
            )
        )
        return report
    if smoke:
        return validate_smoke_behaviors(
            manager.truck_collection,
            manager.order_collection,
            manager.orsim_settings,
        )
    return validate_behaviors(
        manager.truck_collection,
        manager.order_collection,
        manager.orsim_settings,
    )


def generate_smoke_behaviors(datahub_dir, domain, force: bool) -> str:
    name = smoke_scenario_name()
    manager = ScenarioManager(
        datahub_dir,
        name,
        domain=domain,
        generation_profile="smoke",
    )
    if manager.behaviors_exist_on_disk() and not force:
        print(f"Smoke behaviors already exist at {manager.behavior_dir} (use --force to regenerate)")
    else:
        if force and manager.behaviors_exist_on_disk():
            for fname in (
                "truck_behavior.json",
                "order_behavior.json",
                "facility_behavior.json",
                "assignment_behavior.json",
                "analytics_behavior.json",
                "orsim_settings.json",
            ):
                path = os.path.join(manager.behavior_dir, fname)
                if os.path.exists(path):
                    os.remove(path)
        manager.generate_random_behaviors()
        print(f"Generated smoke behaviors at {manager.behavior_dir}")
    return name


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate container logistics simulation setup or run results")
    parser.add_argument("--datahub-dir", default=_default_datahub_dir())
    parser.add_argument("--domain", default=None, help="Simulation domain key (default: container_logistics)")
    parser.add_argument("--scenario", default=None, help="Scenario folder name under scenarios/")
    parser.add_argument("--smoke", action="store_true", help="Use smoke scenario thresholds (1d / 5 trucks / 50 orders)")
    parser.add_argument("--generate-smoke", action="store_true", help="Generate smoke scenario behaviors then validate")
    parser.add_argument("--force", action="store_true", help="Regenerate smoke behaviors if they exist")
    parser.add_argument("--run-id", default=None, help="Validate completed haul trips for this run via API")
    parser.add_argument("--json", action="store_true", help="Emit report as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    domain = args.domain or simulation_domains["container_logistics"]
    scenario_name = args.scenario or (smoke_scenario_name() if args.smoke or args.generate_smoke else None)
    if scenario_name is None:
        from apps.container_logistics.scenario.scenario_config import default_scenario_name

        scenario_name = default_scenario_name()

    if args.generate_smoke:
        scenario_name = generate_smoke_behaviors(args.datahub_dir, domain, args.force)

    report = validate_behaviors_on_disk(
        args.datahub_dir,
        scenario_name,
        domain,
        smoke=args.smoke or args.generate_smoke or scenario_name == smoke_scenario_name(),
    )

    if args.run_id:
        manager = ScenarioManager(args.datahub_dir, scenario_name, domain=domain)
        run_report = validate_run_from_api(
            args.run_id,
            _admin_headers(),
            manager.truck_collection,
            manager.orsim_settings,
            simulation_domain=domain,
        )
        for check in run_report.checks:
            report.add(check)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        report.print_summary()

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
