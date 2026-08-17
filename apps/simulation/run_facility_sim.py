import os
import json
from datetime import datetime

from apps.config import settings
from apps.config import simulation_domains
from apps.config import messenger_backend
from apps.utils import time_to_str
from apps.common.statemachine_registry import StateMachineRegistry

from apps.container_logistics.scenario.generate_behavior import GenerateBehavior
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    GateStateMachine,
)


run_id = f"facility_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _register_container_logistics_statemachines(domain: str, headers: dict) -> None:
    statemachines = {
        GateStateMachine.__name__: GateStateMachine,
    }
    StateMachineRegistry(statemachines=statemachines, domain=domain).register_state_machines(
        server_url=settings["OPENRIDE_SERVER_URL"],
        headers=headers,
    )


def main():
    global run_id

    kafka_utils = None
    # Fail fast if RabbitMQ isn't reachable (kombu can otherwise hang on connect).
    import socket
    import requests
    import time

    def _tcp_check(host: str, port: int, label: str) -> None:
        with socket.create_connection((host, port), timeout=2):
            return

    try:
        _tcp_check("localhost", 5672, "RabbitMQ AMQP")
    except Exception as e:
        raise SystemExit(
            f"RabbitMQ AMQP not reachable at localhost:5672. "
            f"Start infra (e.g. `./start_simulation.sh`) then retry. Root cause: {e}"
        )

    try:
        _tcp_check(messenger_backend.get("MQTT_BROKER", "localhost"), 1883, "MQTT broker")
    except Exception as e:
        raise SystemExit(
            f"MQTT broker not reachable at {messenger_backend.get('MQTT_BROKER','localhost')}:1883. "
            f"Start infra then retry. Root cause: {e}"
        )

    # OpenRide must be reachable for managers to create resources.
    try:
        base = settings["OPENRIDE_SERVER_URL"].rstrip("/")
        resp = requests.get(f"{base}/health", timeout=2)
        if resp.status_code == 404:
            resp = requests.get(f"{base}/", timeout=2)
        if resp.status_code not in (200, 201, 204, 401):
            raise RuntimeError(f"unexpected status {resp.status_code}")
    except Exception as e:
        raise SystemExit(
            f"OpenRide not reachable at {settings['OPENRIDE_SERVER_URL']}. "
            f"Start infra then retry. Root cause: {e}"
        )

    from orsim import ORSimEnv, ORSimScheduler
    from apps.orsim_config import orsim_settings

    # Domain config
    domain = simulation_domains.get("container_logistics", "container-logistics-sim")

    # Ensure ORSim uses the configured backend.
    ORSimEnv.set_backend(messenger_backend)

    # Create a scheduler and a single facility agent.
    scheduler = ORSimScheduler(
        run_id=run_id,
        scheduler_id="facility-only-agent",
        orsim_settings={**orsim_settings, "DOMAIN": domain},
    )

    # Kafka is opt-in for this smoke test (avoids noisy failures when Kafka isn't running).
    if os.getenv("ENABLE_KAFKA", "").lower() in {"1", "true", "yes"}:
        try:
            from apps.utils import kafka_utils as _kafka_utils

            kafka_utils = _kafka_utils
            print(f"Initializing Kafka for {run_id}...")
            kafka_utils.initialize_kafka_topics()
            kafka_utils.push_run_status(kafka_utils.resolve_topic("run_status"), run_id, "RUNNING")
        except Exception as e:
            print(f"Kafka init skipped/failed: {e}")

    # Register state machines using an admin user (independent of agent object return value).
    from apps.common.user_registry import UserRegistry

    admin = UserRegistry(
        time_to_str(datetime.utcnow()),
        {"email": "sim_admin@test.com", "password": "password"},
        role="admin",
    )
    _register_container_logistics_statemachines(domain=domain, headers=admin.get_headers())

    facility_unique_id = "facility_000001"
    facility_behavior = GenerateBehavior.container_facility(facility_unique_id, facility_index=0)
    facility_behavior["service_time"] = 3
    facility_behavior["step_only_on_events"] = False
    if isinstance(facility_behavior.get("profile"), dict):
        facility_behavior["profile"]["service_time"] = 3

    facility_spec = {
        "unique_id": facility_unique_id,
        "run_id": run_id,
        "reference_time": datetime.strftime(datetime(2020, 1, 1, 8, 0, 0), "%Y%m%d%H%M%S"),
        "init_time_step": 0,
        "behavior": facility_behavior,
    }
    facility_agent = scheduler.add_agent(
        spec=facility_spec,
        project_path=None,
        agent_class="apps.container_logistics.facility.FacilityAgent",
    )

    # Manual simulation inputs: we only simulate the minimal messages the facility
    # would receive from haul trips.
    pickup_truck_id = "truck_pickup_000001"
    dropoff_truck_id = "truck_dropoff_000001"

    pickup_arrival_sent = False
    dropoff_arrival_sent = False
    pickup_assigned = pickup_completed = False
    dropoff_assigned = dropoff_completed = False

    start_wall = time.time()
    max_wall_s = 30
    max_steps = 2000
    for step_idx in range(max_steps):
        if time.time() - start_wall > max_wall_s:
            raise SystemExit(
                "Timed out waiting for facility sim to complete. "
                "If infra is running, increase max_wall_s; otherwise check RabbitMQ/MQTT/OpenRide."
            )
        import asyncio

        asyncio.run(scheduler.step())

        app = facility_agent.app
        qc = app.manager.queue_controller

        if app.active and (not pickup_arrival_sent):
            app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": pickup_truck_id,
                    "data": {"event": ContainerLogisticsEvents.TRUCK_ARRIVED_PICKUP_QUEUE},
                }
            )
            pickup_arrival_sent = True

        pickup_assigned = pickup_assigned or (pickup_truck_id in qc.active_truck_ids())
        if pickup_assigned and (pickup_truck_id not in qc.active_truck_ids()):
            pickup_completed = True

        if pickup_completed and (not dropoff_arrival_sent):
            app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": dropoff_truck_id,
                    "data": {"event": ContainerLogisticsEvents.TRUCK_ARRIVED_DROPOFF_QUEUE},
                }
            )
            dropoff_arrival_sent = True

        dropoff_assigned = dropoff_assigned or (dropoff_truck_id in qc.active_truck_ids())
        if dropoff_assigned and (dropoff_truck_id not in qc.active_truck_ids()):
            dropoff_completed = True

        if pickup_completed and dropoff_completed:
            break

    summary = {
        "run_id": run_id,
        "facility_unique_id": facility_unique_id,
        "facility_resource_id": (facility_agent.app.manager.as_dict() or {}).get("_id"),
        "pickup_arrival_sent": pickup_arrival_sent,
        "dropoff_arrival_sent": dropoff_arrival_sent,
        "pickup_assigned": pickup_assigned,
        "pickup_completed": pickup_completed,
        "dropoff_assigned": dropoff_assigned,
        "dropoff_completed": dropoff_completed,
        "gate_assignments": facility_agent.app.manager.queue_controller.gate_assignments,
        "gate_states": [g.current_state.id for g in facility_agent.app.manager.queue_controller.gates],
    }

    print(json.dumps(summary, indent=2, default=str))
    try:
        if kafka_utils is None:
            raise RuntimeError("kafka_utils unavailable")
        kafka_utils.push_run_status(kafka_utils.resolve_topic("run_status"), run_id, "COMPLETED", summary=summary)
    except Exception:
        pass


if __name__ == "__main__":
    main()

