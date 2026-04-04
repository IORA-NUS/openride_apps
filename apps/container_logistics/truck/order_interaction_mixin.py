from orsim.messenger.interaction import message_handler, state_handler

from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    HaulTripStateMachine,
)


class OrderInteractionMixin:
    @message_handler(ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, ContainerLogisticsEvents.ORDER_CANCELLED)
    def _on_order_cancelled(self, payload, data):
        if self.get_trip() is None:
            return
        self.trip.cancel(self.current_time_str, current_loc=self.current_loc)

    @state_handler(HaulTripStateMachine.assigned.name)
    def _on_state_assigned(self, time_since_last_event=0):
        trip = self.get_trip()
        if trip is None:
            return
        pickup_loc = trip.get("pickup_loc")
        estimated_time_to_pickup = trip.get("stats", {}).get("estimated_time_to_pickup", 0)
        reposition_route = trip.get("routes", {}).get("planned", {}).get("repositioning_to_pickup")
        if pickup_loc and pickup_loc != self.current_loc:
            self.trip.start_empty_reposition(
                self.current_time_str,
                current_loc=self.current_loc,
                route=reposition_route,
                estimated_time_to_pickup=estimated_time_to_pickup,
            )
        else:
            self.trip.arrive_pickup_queue(
                self.current_time_str,
                current_loc=self.current_loc,
                queue_arrival_time=self.current_time_str,
            )
