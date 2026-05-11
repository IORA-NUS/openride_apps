"""
Order-only smoke scenario runner.

Runs a single OrderAgent through the full order lifecycle by injecting workflow events:
created -> unassigned -> assigned -> pickup_in_progress -> in_transit -> dropoff_in_progress -> completed
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

from apps.config import settings
from apps.common.statemachine_registry import StateMachineRegistry
from apps.utils import time_to_str

from apps.container_logistics.scenario.generate_behavior import GenerateBehavior
from apps.container_logistics.statemachine import ContainerLogisticsActions, ContainerLogisticsEvents, OrderStateMachine


def _register_container_logistics_statemachines(domain: str, headers: dict) -> None:
    statemachines = {
        OrderStateMachine.__name__: OrderStateMachine,
    }
    StateMachineRegistry(statemachines=statemachines, domain=domain).register_state_machines(
        server_url=settings["OPENRIDE_SERVER_URL"],
        headers=headers,
    )


def _evt(event: str, order_id: str, truck_id: Optional[str] = None) -> dict:
    data = {"event": event, "order_id": order_id}
    if truck_id is not None:
        data["truck_id"] = truck_id
    return {"action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, "data": data}


def main():
    run_id = f"order-only-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    domain = "container-logistics-sim"

    # ORSim backend setup
    from orsim import ORSimEnv, ORSimScheduler
    from apps.config import messenger_backend

    ORSimEnv.set_backend(messenger_backend)

    from apps.orsim_config import orsim_settings

    scheduler = ORSimScheduler(
        run_id=run_id,
        scheduler_id="order-only-agent",
        orsim_settings={**orsim_settings, "DOMAIN": domain},
    )

    # Register state machines using an admin user BEFORE creating agents.
    from apps.common.user_registry import UserRegistry

    admin = UserRegistry(time_to_str(datetime.utcnow()), {"email": "sim_admin@test.com", "password": "password"}, role="admin")
    _register_container_logistics_statemachines(domain=domain, headers=admin.get_headers())

    order_id = "order_000001"
    order_behavior = GenerateBehavior.container_order(order_id)
    order_spec = {
        "unique_id": order_id,
        "run_id": run_id,
        "reference_time": datetime.strftime(datetime(2020, 1, 1, 8, 0, 0), "%Y%m%d%H%M%S"),
        "init_time_step": order_behavior.get("request_time_step", 0),
        "behavior": order_behavior,
    }

    order_agent = scheduler.add_agent(
        spec=order_spec,
        project_path=None,
        agent_class="apps.container_logistics.order.OrderAgent",
    )

    # Drive the lifecycle by injecting order workflow events.
    truck_id = "truck_000001"
    assigned = pickup_started = pickup_done = dropoff_started = delivered = cancelled = False

    max_steps = 2000
    for _ in range(max_steps):
        asyncio.run(scheduler.step())

        # Refresh order state for decision making.
        try:
            order_agent.app.manager.refresh()
        except Exception:
            # Might race with initial registration; continue stepping.
            continue

        state = (order_agent.app.manager.as_dict() or {}).get("state")

        # Note: OrderApp.launch best-effort calls publish(), so we wait until it becomes unassigned.
        if state == OrderStateMachine.unassigned.name and not assigned:
            order_agent.app.handle_app_topic_messages(
                _evt(ContainerLogisticsEvents.ORDER_ASSIGNED_TO_TRUCK, order_id=order_id, truck_id=truck_id)
            )
            assigned = True
            continue

        if state == OrderStateMachine.assigned.name and assigned and not pickup_started:
            order_agent.app.handle_app_topic_messages(_evt(ContainerLogisticsEvents.ORDER_PICKUP_STARTED, order_id=order_id))
            pickup_started = True
            continue

        if state == OrderStateMachine.pickup_in_progress.name and pickup_started and not pickup_done:
            order_agent.app.handle_app_topic_messages(_evt(ContainerLogisticsEvents.ORDER_PICKUP_COMPLETED, order_id=order_id))
            pickup_done = True
            continue

        if state == OrderStateMachine.in_transit.name and pickup_done and not dropoff_started:
            order_agent.app.handle_app_topic_messages(_evt(ContainerLogisticsEvents.ORDER_DROPOFF_STARTED, order_id=order_id))
            dropoff_started = True
            continue

        if state == OrderStateMachine.dropoff_in_progress.name and dropoff_started and not delivered:
            order_agent.app.handle_app_topic_messages(_evt(ContainerLogisticsEvents.ORDER_DELIVERED, order_id=order_id))
            delivered = True
            continue

        if state in (OrderStateMachine.completed.name, OrderStateMachine.cancelled.name):
            cancelled = state == OrderStateMachine.cancelled.name
            break

    print(
        json.dumps(
            {
                "run_id": run_id,
                "order_id": order_id,
                "state": (order_agent.app.manager.as_dict() or {}).get("state"),
                "assigned": assigned,
                "pickup_started": pickup_started,
                "pickup_done": pickup_done,
                "dropoff_started": dropoff_started,
                "delivered": delivered,
                "cancelled": cancelled,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

