"""
Phase 4 smoke run: 1 simulated day, 5 trucks, 50 orders.

  python apps/simulation/run_container_logistics_smoke.py
  python apps/simulation/validate_container_logistics.py --smoke --run-id <run_id>
"""

import logging
import sys
from datetime import datetime

from apps.container_logistics.scenario.scenario_manager import ScenarioManager
from apps.container_logistics.scenario.simulation_validation import validate_smoke_behaviors
from apps.container_logistics.scenario.validation_config import smoke_scenario_name
from apps.simulation.container_logistics_wiring import (
    build_scheduler_config,
    get_agent_config,
    get_datahub_dir,
    get_domain,
    get_statemachine_collection,
    kafka_progress_listener,
    new_run_id,
)
from apps.simulation.simulation_runtime import SimulationRuntime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    datahub_dir = get_datahub_dir()
    domain = get_domain()
    scenario_name = smoke_scenario_name()
    manager = ScenarioManager(
        datahub_dir,
        scenario_name,
        domain=domain,
        generation_profile="smoke",
    )
    if not manager.behaviors_exist_on_disk():
        logger.info("Generating smoke scenario behaviors...")
        manager.generate_random_behaviors()

    behavior_report = validate_smoke_behaviors(
        manager.truck_collection,
        manager.order_collection,
        manager.orsim_settings,
    )
    behavior_report.print_summary()
    if not behavior_report.passed:
        logger.error("Smoke behavior validation failed; aborting simulation")
        return 1

    run_id = new_run_id("smoke")
    sim = SimulationRuntime(
        run_id=run_id,
        scenario_manager=manager,
        datahub_dir=datahub_dir,
        domain=domain,
        agent_config=get_agent_config(),
        statemachine_collection=get_statemachine_collection(),
        scheduler_config=build_scheduler_config(run_id, manager.orsim_settings),
        progress_listener=kafka_progress_listener,
    )

    from apps.utils import kafka_utils

    print(f"Initializing Kafka for smoke run {run_id}...")
    run_status_topic = None
    try:
        kafka_utils.initialize_kafka_topics()
        run_status_topic = kafka_utils.resolve_topic("run_status")
        kafka_utils.push_run_status(run_status_topic, run_id, "RUNNING")
        kafka_utils.flush_producer(10)
    except Exception as exc:
        logger.warning("Kafka init skipped: %s", exc)

    try:
        sim.run_simulation()
        print(f"Smoke simulation completed: {run_id}")
        if run_status_topic:
            kafka_utils.push_run_status(run_status_topic, run_id, "COMPLETED")
    except Exception as exc:
        print(f"Smoke simulation error: {exc}")
        if run_status_topic:
            kafka_utils.push_run_status(run_status_topic, run_id, "FAILED", msg=str(exc))
        return 1
    finally:
        try:
            kafka_utils.flush_producer(10)
        except Exception:
            pass

    print(
        f"\nValidate run results:\n"
        f"  python apps/simulation/validate_container_logistics.py --smoke --run-id {run_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
