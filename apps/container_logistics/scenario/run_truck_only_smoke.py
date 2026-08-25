"""
Truck-only smoke scenario runner.

Runs a single TruckAgent through one full haul cycle:
idle -> assignment -> pickup queue/gate -> dropoff queue/gate -> idle

External actors (assignment + facility) are simulated in-process by directly
invoking FacilityApp methods (which publish messages to the truck over ORSim
messaging) and injecting the assignment payload.
"""

import asyncio
import json
from datetime import datetime

from apps.config import settings
from apps.common.statemachine_registry import StateMachineRegistry
from apps.utils import time_to_str

from apps.container_logistics.scenario.generate_behavior import GenerateBehavior
from apps.container_logistics.statemachine import GateStateMachine, HaulTripStateMachine, OrderStateMachine
from orsim.utils import WorkflowStateMachine

from apps.container_logistics.facility.app import FacilityApp


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
    run_id = f"truck-only-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    domain = "container-logistics-sim"

    # ORSim backend setup
    from orsim import ORSimEnv, ORSimScheduler
    from apps.config import messenger_backend

    ORSimEnv.set_backend(messenger_backend)

    # Create a scheduler and a single truck agent.
    from apps.orsim_config import orsim_settings

    scheduler = ORSimScheduler(
        run_id=run_id,
        scheduler_id="truck-only-agent",
        orsim_settings={**orsim_settings, "DOMAIN": domain},
    )

    truck_id = "truck_000001"
    truck_behavior = GenerateBehavior.container_truck(truck_id)
    truck_spec = {
        "unique_id": truck_id,
        "run_id": run_id,
        "reference_time": datetime.strftime(datetime(2020, 1, 1, 8, 0, 0), "%Y%m%d%H%M%S"),
        "init_time_step": truck_behavior.get("shift_start_time", 0),
        "behavior": truck_behavior,
    }

    truck_agent = scheduler.add_agent(
        spec=truck_spec,
        project_path=None,
        agent_class="apps.container_logistics.truck.TruckAgent",
    )

    # Register state machines using the truck's auth headers (after app init).
    headers = truck_agent.app.user.get_headers()
    _register_container_logistics_statemachines(domain=domain, headers=headers)

    # Build a simulated facility app (not scheduled as an agent).
    facility_id = "facility_000001"
    facility_behavior = GenerateBehavior.container_facility(facility_id, facility_index=0)
    facility_app = FacilityApp(
        run_id=run_id,
        sim_clock=time_to_str(datetime.utcnow()),
        behavior=facility_behavior,
        messenger=truck_agent.messenger,
        agent_helper=None,
    )
    facility_app.launch(sim_clock=time_to_str(datetime.utcnow()))

    # Create a single synthetic order payload for assignment.
    order_id = "order_000001"
    order_behavior = GenerateBehavior.container_order(order_id)
    order_payload = {
        "_id": order_id,
        "pickup_loc": order_behavior["pickup_loc"],
        "dropoff_loc": order_behavior["dropoff_loc"],
        "pickup_service_time": order_behavior.get("pickup_service_time", 120),
        "dropoff_service_time": order_behavior.get("dropoff_service_time", 120),
        # Routes and leg ETAs are on the truck profile; TruckApp fills them at assignment via OSRM.
        "profile": order_behavior.get("profile", {}),
    }

    # Drive the loop until we complete one cycle.
    assigned = False
    pickup_gate_assigned = False
    pickup_done = False
    dropoff_gate_assigned = False
    done = False

    max_steps = 2000
    for _ in range(max_steps):
        asyncio.run(scheduler.step())

        # Inject assignment once truck is online and idle.
        truck = truck_agent.app.get_truck()
        trip = truck_agent.app.get_trip()

        if not assigned and truck and truck.get("state") == WorkflowStateMachine.online.name and trip is None:
            truck_agent.app.handle_app_topic_messages(
                {"action": "assigned_haul_trip", "truck_id": truck.get("_id"), "order": order_payload}
            )
            assigned = True

        trip = truck_agent.app.get_trip()
        if not trip:
            if assigned and pickup_done:
                done = True
                break
            continue

        state = trip.get("state")

        # When the truck arrives pickup queue, simulate facility queue/gate.
        if state == HaulTripStateMachine.queued_for_pickup.name and not pickup_gate_assigned:
            facility_app.enqueue_arrival(truck_id=truck.get("_id"), visit_type="pickup")
            pickup_gate_assigned = True

        # After gate assignment, the truck will enter pickup gate. Complete service immediately.
        if state == HaulTripStateMachine.at_pickup_gate.name and not pickup_done:
            # Gate index 0 in our tiny facility.
            facility_app.complete_gate_service(gate_index=0)
            pickup_done = True

        # When the truck arrives dropoff queue, simulate gate assignment.
        if state == HaulTripStateMachine.queued_for_dropoff.name and not dropoff_gate_assigned:
            facility_app.enqueue_arrival(truck_id=truck.get("_id"), visit_type="dropoff")
            dropoff_gate_assigned = True

        if state == HaulTripStateMachine.at_dropoff_gate.name:
            facility_app.complete_gate_service(gate_index=0)

        if state == HaulTripStateMachine.completed.name:
            # TruckApp.refresh() clears completed trips and re-enters idle.
            continue

    print(
        json.dumps(
            {
                "run_id": run_id,
                "truck_id": truck_id,
                "assigned": assigned,
                "pickup_gate_assigned": pickup_gate_assigned,
                "pickup_done": pickup_done,
                "dropoff_gate_assigned": dropoff_gate_assigned,
                "done": done,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

