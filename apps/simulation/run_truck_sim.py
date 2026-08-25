import os
import json
from datetime import datetime

from apps.config import settings
from apps.config import simulation_domains
from apps.utils import time_to_str
from apps.common.statemachine_registry import StateMachineRegistry
from apps.container_logistics.scenario.generate_behavior import GenerateBehavior
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    GateStateMachine,
    HaulTripStateMachine,
    OrderStateMachine,
)
from orsim.utils import WorkflowStateMachine


run_id = f"truck_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _register_container_logistics_statemachines(domain: str, headers: dict) -> None:
    statemachines = {
        "WorkflowStateMachine": WorkflowStateMachine,
        HaulTripStateMachine.__name__: HaulTripStateMachine,
        GateStateMachine.__name__: GateStateMachine,
        OrderStateMachine.__name__: OrderStateMachine,
    }
    StateMachineRegistry(statemachines=statemachines, domain=domain).register_state_machines(
        server_url=settings["OPENRIDE_SERVER_URL"],
        headers=headers,
    )


def main():
    global run_id

    kafka_utils = None
    from orsim import ORSimEnv
    from apps.config import messenger_backend
    from apps.orsim_config import orsim_settings

    # Domain config
    domain = simulation_domains.get("container_logistics", "container-logistics-sim")

    # Ensure ORSim uses the configured backend.
    ORSimEnv.set_backend(messenger_backend)

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

    # Create one truck agent in-process (no Celery), while still using the real
    # OpenRide + routing + messaging backends.
    from apps.container_logistics.analytics.agent import AnalyticsAgentIndie
    from apps.container_logistics.truck.agent import TruckAgent

    truck_unique_id = "truck_000001"
    truck_behavior = GenerateBehavior.container_truck(truck_unique_id)
    truck_behavior["shift_start_time"] = 0
    truck_behavior["shift_end_time"] = max(truck_behavior.get("shift_end_time", 0), 3600)

    _ref = datetime.strftime(datetime(2020, 1, 1, 8, 0, 0), "%Y%m%d%H%M%S")
    _sched = {"id": "local", "orsim_settings": {**orsim_settings, "DOMAIN": domain}}

    truck_agent = TruckAgent(
        unique_id=truck_unique_id,
        run_id=run_id,
        reference_time=_ref,
        init_time_step=0,
        scheduler=_sched,
        behavior=truck_behavior,
    )

    analytics_unique_id = "analytics_000"
    analytics_agent = AnalyticsAgentIndie(
        unique_id=analytics_unique_id,
        run_id=run_id,
        reference_time=_ref,
        init_time_step=0,
        scheduler=_sched,
        behavior=GenerateBehavior.container_analytics(analytics_unique_id),
    )

    # Register state machines using an admin user (independent of agent object return value).
    from apps.common.user_registry import UserRegistry

    admin = UserRegistry(time_to_str(datetime.utcnow()), {"email": "sim_admin@test.com", "password": "password"}, role="admin")
    _register_container_logistics_statemachines(domain=domain, headers=admin.get_headers())

    # One synthetic order that goes pickup facility -> dropoff facility.
    order_behavior = GenerateBehavior.container_order("order_000001")
    order_payload = {
        "_id": "order_000001",
        "pickup_loc": order_behavior["pickup_loc"],
        "dropoff_loc": order_behavior["dropoff_loc"],
        "pickup_service_time": order_behavior.get("pickup_service_time", 120),
        "dropoff_service_time": order_behavior.get("dropoff_service_time", 120),
        "profile": order_behavior.get("profile", {}),
    }

    # State tracking.
    assigned = False
    pickup_gate_sent = False
    pickup_complete_sent = False
    dropoff_gate_sent = False
    dropoff_complete_sent = False
    completed = False
    returned_to_idle = False

    # Run until: truck becomes online -> assignment -> haul trip completed -> idle trip resumed.
    max_steps = 4000
    for step_idx in range(max_steps):
        # Advance local simulation clock.
        truck_agent.bootstrap_step(step_idx)
        analytics_agent.bootstrap_step(step_idx)
        # Ensure agent has launched.
        truck_agent.entering_market(step_idx)
        analytics_agent.entering_market(step_idx)
        truck_agent.step(step_idx)
        analytics_agent.step(step_idx)

        truck_res = truck_agent.app.get_truck()
        haul_trip = truck_agent.app.get_trip()

        # Inject assignment once the truck is online and not already on a haul trip.
        if (
            (not assigned)
            and truck_res
            and truck_res.get("state") == WorkflowStateMachine.online.name
            and haul_trip is None
        ):
            truck_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
                    "truck_id": truck_res.get("_id"),
                    "order": order_payload,
                }
            )
            assigned = True

        if haul_trip is None:
            if assigned and completed:
                # TruckApp.refresh() should have cleared the haul trip and restarted idle.
                returned_to_idle = True
                break
            continue

        state = haul_trip.get("state")

        # Pickup facility flow (simulated messages).
        if state == HaulTripStateMachine.queued_for_pickup.name and (not pickup_gate_sent):
            truck_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": truck_res.get("_id"),
                    "data": {
                        "event": ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_PICKUP,
                        "gate_index": 0,
                        "service_time": order_payload.get("pickup_service_time", 120),
                    },
                }
            )
            pickup_gate_sent = True

        if state == HaulTripStateMachine.at_pickup_gate.name and (not pickup_complete_sent):
            # Provide the truck with the planned route + ETA to dropoff to keep state progression consistent.
            planned_dropoff = ((haul_trip.get("routes") or {}).get("planned") or {}).get("loaded_to_dropoff")
            truck_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": truck_res.get("_id"),
                    "data": {
                        "event": ContainerLogisticsEvents.PICKUP_GATE_SERVICE_COMPLETED,
                        "service_time": order_payload.get("pickup_service_time", 120),
                        "planned_route": planned_dropoff,
                        "estimated_time_to_dropoff": (haul_trip.get("stats") or {}).get("estimated_time_to_dropoff", 0),
                    },
                }
            )
            pickup_complete_sent = True

        # Dropoff facility flow (simulated messages).
        if state == HaulTripStateMachine.queued_for_dropoff.name and (not dropoff_gate_sent):
            truck_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": truck_res.get("_id"),
                    "data": {
                        "event": ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_DROPOFF,
                        "gate_index": 0,
                        "service_time": order_payload.get("dropoff_service_time", 120),
                    },
                }
            )
            dropoff_gate_sent = True

        if state == HaulTripStateMachine.at_dropoff_gate.name and (not dropoff_complete_sent):
            truck_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": truck_res.get("_id"),
                    "data": {
                        "event": ContainerLogisticsEvents.DROPOFF_GATE_SERVICE_COMPLETED,
                        "service_time": order_payload.get("dropoff_service_time", 120),
                    },
                }
            )
            dropoff_complete_sent = True

        if state == HaulTripStateMachine.completed.name:
            completed = True

    summary = {
        "run_id": run_id,
        "truck_unique_id": truck_unique_id,
        "truck_resource_id": (truck_res or {}).get("_id") if 'truck_res' in locals() else None,
        "assigned": assigned,
        "pickup_gate_sent": pickup_gate_sent,
        "pickup_complete_sent": pickup_complete_sent,
        "dropoff_gate_sent": dropoff_gate_sent,
        "dropoff_complete_sent": dropoff_complete_sent,
        "completed": completed,
        "returned_to_idle": returned_to_idle,
    }

    print(json.dumps(summary, indent=2))
    try:
        if kafka_utils is None:
            raise RuntimeError("kafka_utils unavailable")
        kafka_utils.push_run_status(kafka_utils.resolve_topic("run_status"), run_id, "COMPLETED", summary=summary)
    except Exception:
        pass


if __name__ == "__main__":
    main()

