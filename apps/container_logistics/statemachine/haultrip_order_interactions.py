from .events import ContainerLogisticsActions, ContainerLogisticsEvents
from .haul_trip_sm import HaulTripStateMachine
from .order_sm import OrderStateMachine


haultrip_order_interactions = [
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.assign.name,
        "source_new_state": HaulTripStateMachine.assigned.name,
        "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.ORDER_ASSIGNED_TO_TRUCK,
        "target_statemachine": OrderStateMachine.__name__,
        "target_new_state": OrderStateMachine.assigned.name,
        "description": "Assignment model allocates order to truck and creates haul trip",
    },
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.enter_pickup_gate.name,
        "source_new_state": HaulTripStateMachine.at_pickup_gate.name,
        "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.ORDER_PICKUP_STARTED,
        "target_statemachine": OrderStateMachine.__name__,
        "target_new_state": OrderStateMachine.pickup_in_progress.name,
        "description": "Haul trip enters pickup gate and order pickup starts",
    },
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.finish_pickup_service.name,
        "source_new_state": HaulTripStateMachine.loaded_in_transit.name,
        "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.ORDER_PICKUP_COMPLETED,
        "target_statemachine": OrderStateMachine.__name__,
        "target_new_state": OrderStateMachine.in_transit.name,
        "description": "Pickup gate service completes and order becomes in transit",
    },
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.enter_dropoff_gate.name,
        "source_new_state": HaulTripStateMachine.at_dropoff_gate.name,
        "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.ORDER_DROPOFF_STARTED,
        "target_statemachine": OrderStateMachine.__name__,
        "target_new_state": OrderStateMachine.dropoff_in_progress.name,
        "description": "Haul trip enters dropoff gate and order dropoff starts",
    },
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.finish_dropoff_service.name,
        "source_new_state": HaulTripStateMachine.completed.name,
        "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.ORDER_DELIVERED,
        "target_statemachine": OrderStateMachine.__name__,
        "target_new_state": OrderStateMachine.completed.name,
        "description": "Haul trip completes dropoff service and order is delivered",
    },
    {
        "source_statemachine": HaulTripStateMachine.__name__,
        "source_transition": HaulTripStateMachine.cancel.name,
        "source_new_state": HaulTripStateMachine.cancelled.name,
        "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
        "event": ContainerLogisticsEvents.ORDER_CANCELLED,
        "target_statemachine": OrderStateMachine.__name__,
        "target_new_state": OrderStateMachine.cancelled.name,
        "description": "Haul trip cancellation propagates to the order",
    },
]
