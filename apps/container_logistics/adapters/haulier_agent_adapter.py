from apps.container_logistics.contracts import validate_facility_workflow_payload
from apps.container_logistics.events import ContainerLogisticsActions, ContainerLogisticsEvents
from apps.state_machine.container_logistics.haulier_workflow_sm import (
    HaulierContainerWorkflowStateMachine,
)

from .runtime_agent_adapter_base import RuntimeInteractionAgentAdapter


class HaulierAgentAdapter(RuntimeInteractionAgentAdapter):
    """Pilot runtime adapter for haulier workflow using shared agent_core runtime."""

    def __init__(self, haulier_id):
        self.haulier_id = haulier_id
        super().__init__(
            ingress_action=ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
            validator=validate_facility_workflow_payload,
            state_machine=HaulierContainerWorkflowStateMachine(),
            event_transition_map={
                ContainerLogisticsEvents.FACILITY_ACKNOWLEDGES_PICKUP_CHECKIN: "facility_acknowledges_pickup_checkin",
                ContainerLogisticsEvents.FACILITY_ALLOCATES_PICKUP_SLOT: "facility_allocates_pickup_slot",
                ContainerLogisticsEvents.FACILITY_RELEASES_CONTAINER: "facility_releases_container",
                ContainerLogisticsEvents.FACILITY_ACKNOWLEDGES_DROPOFF_CHECKIN: "facility_acknowledges_dropoff_checkin",
                ContainerLogisticsEvents.FACILITY_ALLOCATES_DROPOFF_SLOT: "facility_allocates_dropoff_slot",
                ContainerLogisticsEvents.FACILITY_ACCEPTS_CONTAINER: "facility_accepts_container",
            },
        )

    def receive_haul_request(self):
        self._state_machine.facility_publishes_request()

    def accept_request(self):
        self._state_machine.haulier_accepts_request()
        self._state_machine.drive_to_pickup()
        return self._haulier_message(ContainerLogisticsEvents.HAULIER_ACCEPTS_REQUEST)

    def arrive_for_pickup(self):
        self._state_machine.arrive_for_pickup()
        return self._haulier_message(ContainerLogisticsEvents.ARRIVE_FOR_PICKUP)

    def leave_pickup(self):
        self._state_machine.leave_pickup()
        self._state_machine.drive_to_dropoff()

    def arrive_for_dropoff(self):
        self._state_machine.arrive_for_dropoff()
        return self._haulier_message(ContainerLogisticsEvents.ARRIVE_FOR_DROPOFF)

    def close_haul(self):
        self._state_machine.close_haul()

    def _haulier_message(self, event_name):
        return {
            "action": ContainerLogisticsActions.HAULIER_WORKFLOW_EVENT,
            "haulier_id": self.haulier_id,
            "data": {"event": event_name},
        }
