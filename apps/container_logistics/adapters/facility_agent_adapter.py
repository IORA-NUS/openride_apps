from apps.container_logistics.contracts import validate_haulier_workflow_payload
from apps.container_logistics.events import ContainerLogisticsActions, ContainerLogisticsEvents
from apps.state_machine.container_logistics.facility_agent_sm import (
    FacilityAgentInteractionStateMachine,
)

from .runtime_agent_adapter_base import RuntimeInteractionAgentAdapter


class FacilityAgentAdapter(RuntimeInteractionAgentAdapter):
    """Pilot runtime adapter for facility interactions using shared agent_core runtime."""

    def __init__(self, facility_id):
        self.facility_id = facility_id
        super().__init__(
            ingress_action=ContainerLogisticsActions.HAULIER_WORKFLOW_EVENT,
            validator=validate_haulier_workflow_payload,
            state_machine=FacilityAgentInteractionStateMachine(),
            event_transition_map={
                ContainerLogisticsEvents.HAULIER_ACCEPTS_REQUEST: "receive_haulier_confirmation",
                ContainerLogisticsEvents.ARRIVE_FOR_PICKUP: "receive_haulier_arrival",
                ContainerLogisticsEvents.ARRIVE_FOR_DROPOFF: "receive_haulier_arrival",
            },
        )

    def publish_haul_request(self):
        self._state_machine.publish_haul_request()
        return {
            "action": ContainerLogisticsActions.HAUL_REQUEST,
            "facility_id": self.facility_id,
            "data": {"event": ContainerLogisticsEvents.PUBLISH_HAUL_REQUEST},
        }

    def validate_pickup_checkin(self):
        self._state_machine.validate_checkin()
        return self._facility_message(ContainerLogisticsEvents.FACILITY_ACKNOWLEDGES_PICKUP_CHECKIN)

    def allocate_pickup_slot(self):
        self._state_machine.allocate_handling_slot()
        return self._facility_message(ContainerLogisticsEvents.FACILITY_ALLOCATES_PICKUP_SLOT)

    def release_container(self):
        self._state_machine.start_handoff()
        return self._facility_message(ContainerLogisticsEvents.FACILITY_RELEASES_CONTAINER)

    def complete_and_close_interaction(self):
        self._state_machine.complete_handoff()
        self._state_machine.close_interaction()

    def emit_dropoff_checkin_ack(self):
        return self._facility_message(ContainerLogisticsEvents.FACILITY_ACKNOWLEDGES_DROPOFF_CHECKIN)

    def emit_dropoff_slot(self):
        return self._facility_message(ContainerLogisticsEvents.FACILITY_ALLOCATES_DROPOFF_SLOT)

    def emit_container_accept(self):
        return self._facility_message(ContainerLogisticsEvents.FACILITY_ACCEPTS_CONTAINER)

    def _facility_message(self, event_name):
        return {
            "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
            "facility_id": self.facility_id,
            "data": {"event": event_name},
        }
