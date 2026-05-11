import os
import json
from datetime import datetime

# Default to the local OpenRide nginx gateway (see `start.py`, default 11654).
# Override as needed:
#   OPENRIDE_SERVER_URL="http://localhost:11654" ./venv/bin/python -m apps.simulation.run_order_sim
os.environ.setdefault("OPENRIDE_SERVER_URL", "http://localhost:11654")

from apps.config import settings
from apps.config import simulation_domains
from apps.utils import time_to_str
from apps.common.statemachine_registry import StateMachineRegistry

from apps.container_logistics.scenario.generate_behavior import GenerateBehavior
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    OrderStateMachine,
)


run_id = f"order_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _register_container_logistics_statemachines(domain: str, headers: dict) -> None:
    statemachines = {
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

    # Register state machines using an admin user BEFORE creating agents.
    # Order/Truck/Facility managers create server resources on init, which requires
    # statemachine definitions to already exist server-side.
    from apps.common.user_registry import UserRegistry

    admin = UserRegistry(
        time_to_str(datetime.utcnow()),
        {"email": "sim_admin@test.com", "password": "password"},
        role="admin",
    )
    _register_container_logistics_statemachines(domain=domain, headers=admin.get_headers())

    # Kafka is opt-in for this smoke test (avoids noisy failures when Kafka isn't running).
    if os.getenv("ENABLE_KAFKA", "").lower() in {"1", "true", "yes"}:
        try:
            from apps.utils import kafka_utils as _kafka_utils

            kafka_utils = _kafka_utils
            print(f"Initializing Kafka for {run_id}...")
            kafka_utils.initialize_kafka_topics()
            kafka_utils.push_event("run_status", {"status": "RUNNING"}, key=run_id)
        except Exception as e:
            print(f"Kafka init skipped/failed: {e}")

    # Create one order agent in-process (no Celery), while still using the real
    # OpenRide + routing + messaging backends.
    from apps.container_logistics.order.agent import OrderAgent

    order_unique_id = "order_000001"
    behavior = GenerateBehavior.container_order(order_unique_id)
    behavior["request_time_step"] = 0

    order_agent = OrderAgent(
        unique_id=order_unique_id,
        run_id=run_id,
        reference_time=datetime.strftime(datetime(2020, 1, 1, 8, 0, 0), "%Y%m%d%H%M%S"),
        init_time_step=0,
        scheduler={"id": "local", "orsim_settings": {**orsim_settings, "DOMAIN": domain}},
        behavior=behavior,
    )

    # State tracking.
    assigned = pickup_started = pickup_done = dropoff_started = delivered = False
    terminal_state = None

    # Run until terminal state.
    max_steps = 2000
    for step_idx in range(max_steps):
        order_agent.bootstrap_step(step_idx)
        order_agent.entering_market(step_idx)
        order_agent.step(step_idx)

        # Refresh the server-backed order resource for state decisions.
        try:
            order_agent.app.manager.refresh()
        except Exception:
            continue

        order = order_agent.app.manager.as_dict() or {}
        state = order.get("state")

        # OrderApp.launch best-effort publishes; wait for unassigned before assigning.
        if state == OrderStateMachine.unassigned.name and not assigned:
            order_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
                    "data": {
                        "event": ContainerLogisticsEvents.ORDER_ASSIGNED_TO_TRUCK,
                        "order_id": order.get("_id"),
                        "truck_id": "truck_000001",
                    },
                }
            )
            assigned = True
            continue

        if state == OrderStateMachine.assigned.name and assigned and not pickup_started:
            order_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
                    "data": {"event": ContainerLogisticsEvents.ORDER_PICKUP_STARTED, "order_id": order.get("_id")},
                }
            )
            pickup_started = True
            continue

        if state == OrderStateMachine.pickup_in_progress.name and pickup_started and not pickup_done:
            order_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
                    "data": {"event": ContainerLogisticsEvents.ORDER_PICKUP_COMPLETED, "order_id": order.get("_id")},
                }
            )
            pickup_done = True
            continue

        if state == OrderStateMachine.in_transit.name and pickup_done and not dropoff_started:
            order_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
                    "data": {"event": ContainerLogisticsEvents.ORDER_DROPOFF_STARTED, "order_id": order.get("_id")},
                }
            )
            dropoff_started = True
            continue

        if state == OrderStateMachine.dropoff_in_progress.name and dropoff_started and not delivered:
            order_agent.app.handle_app_topic_messages(
                {
                    "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
                    "data": {"event": ContainerLogisticsEvents.ORDER_DELIVERED, "order_id": order.get("_id")},
                }
            )
            delivered = True
            continue

        if state in (OrderStateMachine.completed.name, OrderStateMachine.cancelled.name):
            terminal_state = state
            break

    summary = {
        "run_id": run_id,
        "order_unique_id": order_unique_id,
        "order_resource_id": (order_agent.app.manager.as_dict() or {}).get("_id"),
        "assigned": assigned,
        "pickup_started": pickup_started,
        "pickup_done": pickup_done,
        "dropoff_started": dropoff_started,
        "delivered": delivered,
        "terminal_state": terminal_state,
    }

    print(json.dumps(summary, indent=2))
    try:
        if kafka_utils is None:
            raise RuntimeError("kafka_utils unavailable")
        kafka_utils.push_event("run_status", {"status": "COMPLETED", "summary": summary}, key=run_id)
    except Exception:
        pass


if __name__ == "__main__":
    main()

