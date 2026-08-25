"""
Facility-only smoke scenario runner.

Runs a single FacilityAgent using the full OpenRide + ORSim + messaging stack,
but manually simulates the *minimal* external inputs (truck-arrival events).

If the required infrastructure isn't running (OpenRide gateway, RabbitMQ/MQTT),
this script will fail fast with a clear message rather than hanging.
"""

import json
import os
import socket
import time
from datetime import datetime, timedelta

import requests

from apps.config import messenger_backend, settings, simulation_domains
from apps.common.statemachine_registry import StateMachineRegistry
from apps.container_logistics.scenario.generate_behavior import GenerateBehavior
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    GateStateMachine,
)
from apps.utils import time_to_str


def main():
    # Default to the local OpenRide nginx gateway (see `start.py`, default 11654).
    os.environ.setdefault("OPENRIDE_SERVER_URL", settings["OPENRIDE_SERVER_URL"])

    run_id = f"facility-only-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    domain = simulation_domains.get("container_logistics", "container-logistics-sim")

    # Preflight: OpenRide must be reachable for managers to create resources.
    try:
        # Some deployments expose `/health`, others only respond on `/`.
        base = settings["OPENRIDE_SERVER_URL"].rstrip("/")
        resp = requests.get(f"{base}/health", timeout=2)
        if resp.status_code == 404:
            resp = requests.get(f"{base}/", timeout=2)
        # 401 is "reachable but protected"; that's fine for this smoke runner.
        if resp.status_code not in (200, 201, 204, 401):
            raise RuntimeError(f"unexpected status {resp.status_code}")
    except Exception as e:
        raise SystemExit(
            f"OpenRide not reachable at {settings['OPENRIDE_SERVER_URL']}. "
            f"Start infra (e.g. `./start_simulation.sh`) then retry. Root cause: {e}"
        )

    # ORSim backend setup
    # Fail fast if broker isn't reachable (otherwise kombu can hang on connect).
    mgmt = messenger_backend.get("RABBITMQ_MANAGEMENT_SERVER", "")
    if mgmt:
        try:
            requests.get(mgmt, auth=(messenger_backend.get("RABBITMQ_ADMIN_USER", "guest"), messenger_backend.get("RABBITMQ_ADMIN_PASSWORD", "guest")), timeout=2)
        except Exception as e:
            raise SystemExit(
                f"RabbitMQ management not reachable at {mgmt}. "
                f"Start infra then retry. Root cause: {e}"
            )

    def _tcp_check(host: str, port: int, label: str) -> None:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except Exception as e:
            raise SystemExit(f"{label} not reachable at {host}:{port}. Start infra then retry. Root cause: {e}")

    # Default local ports; adjust if your infra is different.
    _tcp_check("localhost", 5672, "RabbitMQ AMQP")
    _tcp_check(messenger_backend.get("MQTT_BROKER", "localhost"), 1883, "MQTT broker")

    from orsim import ORSimEnv, ORSimScheduler

    ORSimEnv.set_backend(messenger_backend)

    from apps.orsim_config import orsim_settings

    scheduler = ORSimScheduler(
        run_id=run_id,
        scheduler_id="facility-only-agent",
        orsim_settings={**orsim_settings, "DOMAIN": domain},
    )

    # Register state machines using an admin user BEFORE creating agents.
    from apps.common.user_registry import UserRegistry

    admin = UserRegistry(
        time_to_str(datetime.utcnow()),
        {"email": "sim_admin@test.com", "password": "password"},
        role="admin",
    )
    StateMachineRegistry(statemachines={GateStateMachine.__name__: GateStateMachine}, domain=domain).register_state_machines(
        server_url=settings["OPENRIDE_SERVER_URL"],
        headers=admin.get_headers(),
    )

    facility_id = "facility_000001"
    facility_behavior = GenerateBehavior.container_facility(facility_id, facility_index=0)

    # Keep the smoke run fast/deterministic.
    facility_behavior["service_time"] = 3
    if isinstance(facility_behavior.get("profile"), dict):
        facility_behavior["profile"]["service_time"] = 3

    facility_spec = {
        "unique_id": facility_id,
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

    pickup_truck_id = "truck_pickup_000001"
    dropoff_truck_id = "truck_dropoff_000001"

    pickup_assigned = False
    pickup_completed = False
    dropoff_assigned = False
    dropoff_completed = False

    def _arrival(event: str, truck_id: str) -> dict:
        return {
            "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
            "truck_id": truck_id,
            "data": {"event": event},
        }

    # Manual "inputs" (simulating only the other agents' necessary messages).
    pickup_arrival_sent = False
    dropoff_arrival_sent = False

    start_wall = time.time()
    max_wall_s = 30
    max_steps = 200

    import asyncio

    for _ in range(max_steps):
        if time.time() - start_wall > max_wall_s:
            raise SystemExit(
                "Timed out waiting for facility workflow to progress. "
                "Infra might be down (RabbitMQ/MQTT/OpenRide)."
            )

        asyncio.run(scheduler.step())

        app = facility_agent.app
        qc = app.manager.queue_controller

        if app.active and not pickup_arrival_sent:
            app.handle_app_topic_messages(
                _arrival(ContainerLogisticsEvents.TRUCK_ARRIVED_PICKUP_QUEUE, truck_id=pickup_truck_id)
            )
            pickup_arrival_sent = True

        # Observe assignment/completion via in-memory controller state.
        pickup_assigned = pickup_assigned or (pickup_truck_id in qc.active_truck_ids())
        if pickup_assigned and (pickup_truck_id not in qc.active_truck_ids()):
            pickup_completed = True

        if pickup_completed and (not dropoff_arrival_sent):
            app.handle_app_topic_messages(
                _arrival(ContainerLogisticsEvents.TRUCK_ARRIVED_DROPOFF_QUEUE, truck_id=dropoff_truck_id)
            )
            dropoff_arrival_sent = True

        dropoff_assigned = dropoff_assigned or (dropoff_truck_id in qc.active_truck_ids())
        if dropoff_assigned and (dropoff_truck_id not in qc.active_truck_ids()):
            dropoff_completed = True

        if pickup_completed and dropoff_completed:
            break

    print(
        json.dumps(
            {
                "run_id": run_id,
                "facility_id": facility_id,
                "pickup_assigned": pickup_assigned,
                "pickup_completed": pickup_completed,
                "dropoff_assigned": dropoff_assigned,
                "dropoff_completed": dropoff_completed,
                "gate_assignments": app.manager.queue_controller.gate_assignments,
                "gate_states": [g.current_state.id for g in app.manager.queue_controller.gates],
                "queue_len": len(app.manager.queue_controller.queue),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()

