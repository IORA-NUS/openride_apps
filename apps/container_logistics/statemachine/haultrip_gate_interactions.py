from .events import ContainerLogisticsActions, ContainerLogisticsEvents
from .gate_sm import GateStateMachine
from .haul_trip_sm import HaulTripStateMachine


haultrip_gate_interactions = [
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.arrive_pickup_queue.name,
        "source_new_state": HaulTripStateMachine.queued_for_pickup.name,
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.TRUCK_ARRIVED_PICKUP_QUEUE,
        "target_statemachine": GateStateMachine.__name__,
        "target_new_state": GateStateMachine.available.name,
        "description": "Truck joins pickup queue and waits for an available gate slot",
    },
    {
        "source_statemachine": GateStateMachine.__name__,
        "source_transition": GateStateMachine.assign_pickup_truck.name,
        "source_new_state": GateStateMachine.busy_pickup.name,
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_PICKUP,
        "target_statemachine": HaulTripStateMachine.__name__,
        "target_new_state": HaulTripStateMachine.at_pickup_gate.name,
        "description": "Facility assigns a pickup gate slot to the waiting haul trip",
    },
    {
        "source_statemachine": GateStateMachine.__name__,
        "source_transition": GateStateMachine.complete_service.name,
        "source_new_state": GateStateMachine.available.name,
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.PICKUP_GATE_SERVICE_COMPLETED,
        "target_statemachine": HaulTripStateMachine.__name__,
        "target_new_state": HaulTripStateMachine.loaded_in_transit.name,
        "description": "Pickup service finishes and haul trip departs loaded",
    },
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.arrive_dropoff_queue.name,
        "source_new_state": HaulTripStateMachine.queued_for_dropoff.name,
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.TRUCK_ARRIVED_DROPOFF_QUEUE,
        "target_statemachine": GateStateMachine.__name__,
        "target_new_state": GateStateMachine.available.name,
        "description": "Truck joins dropoff queue and waits for an available gate slot",
    },
    {
        "source_statemachine": GateStateMachine.__name__,
        "source_transition": GateStateMachine.assign_dropoff_truck.name,
        "source_new_state": GateStateMachine.busy_dropoff.name,
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_DROPOFF,
        "target_statemachine": HaulTripStateMachine.__name__,
        "target_new_state": HaulTripStateMachine.at_dropoff_gate.name,
        "description": "Facility assigns a dropoff gate slot to the waiting haul trip",
    },
    {
        "source_statemachine": GateStateMachine.__name__,
        "source_transition": GateStateMachine.complete_service.name,
        "source_new_state": GateStateMachine.available.name,
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.DROPOFF_GATE_SERVICE_COMPLETED,
        "target_statemachine": HaulTripStateMachine.__name__,
        "target_new_state": HaulTripStateMachine.completed.name,
        "description": "Dropoff service finishes and haul trip completes",
    },
]
