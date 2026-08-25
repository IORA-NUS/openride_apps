from orsim.messenger.interaction import message_handler

from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    FacilityVisitType,
)


class HaulTripInteractionMixin:
    @message_handler(
        ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        ContainerLogisticsEvents.TRUCK_ARRIVED_PICKUP_QUEUE,
    )
    def _on_truck_arrived_pickup_queue(self, payload, data):
        self.enqueue_arrival(payload.get("truck_id"), visit_type=FacilityVisitType.PICKUP)

    @message_handler(
        ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        ContainerLogisticsEvents.TRUCK_ARRIVED_DROPOFF_QUEUE,
    )
    def _on_truck_arrived_dropoff_queue(self, payload, data):
        self.enqueue_arrival(payload.get("truck_id"), visit_type=FacilityVisitType.DROPOFF)
