from orsim.messenger.interaction import message_handler, state_handler

from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    HaulTripStateMachine,
)


class FacilityInteractionMixin:
    @message_handler(
        ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_PICKUP,
    )
    def _on_gate_slot_assigned_for_pickup(self, payload, data):
        self.trip.enter_pickup_gate(
            self.current_time_str,
            current_loc=self.current_loc,
            gate_index=(data or {}).get("gate_index"),
            service_time=(data or {}).get("service_time", 0),
        )

    @message_handler(
        ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        ContainerLogisticsEvents.PICKUP_GATE_SERVICE_COMPLETED,
    )
    def _on_pickup_service_completed(self, payload, data):
        self.trip.finish_pickup_service(
            self.current_time_str,
            current_loc=self.current_loc,
            route_to_dropoff=(data or {}).get("planned_route"),
            estimated_time_to_dropoff=(data or {}).get("estimated_time_to_dropoff", 0),
            service_time=(data or {}).get("service_time"),
        )

    @message_handler(
        ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_DROPOFF,
    )
    def _on_gate_slot_assigned_for_dropoff(self, payload, data):
        self.trip.enter_dropoff_gate(
            self.current_time_str,
            current_loc=self.current_loc,
            gate_index=(data or {}).get("gate_index"),
            service_time=(data or {}).get("service_time", 0),
        )

    @message_handler(
        ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        ContainerLogisticsEvents.DROPOFF_GATE_SERVICE_COMPLETED,
    )
    def _on_dropoff_service_completed(self, payload, data):
        self.trip.finish_dropoff_service(
            self.current_time_str,
            current_loc=self.current_loc,
            service_time=(data or {}).get("service_time"),
        )

    @state_handler(HaulTripStateMachine.repositioning_to_pickup.name)
    def _on_state_repositioning_to_pickup(self, time_since_last_event=0):
        self.trip.arrive_pickup_queue(
            self.current_time_str,
            current_loc=self.current_loc,
            queue_arrival_time=self.current_time_str,
        )

    @state_handler(HaulTripStateMachine.loaded_in_transit.name)
    def _on_state_loaded_in_transit(self, time_since_last_event=0):
        self.trip.arrive_dropoff_queue(
            self.current_time_str,
            current_loc=self.current_loc,
            queue_arrival_time=self.current_time_str,
        )
