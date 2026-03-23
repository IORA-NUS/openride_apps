from apps.container_logistics.contracts import validate_facility_workflow_payload
from apps.container_logistics.events import ContainerLogisticsActions

from .base_interaction_adapter import BaseInteractionAdapter


class HaulierInteractionAdapter(BaseInteractionAdapter):
    """Minimal haulier-side adapter to route facility workflow events."""

    def handle_message(self, payload):
        if validate_facility_workflow_payload(payload) is False:
            return False
        return self.dispatch_message(
            action=ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
            event=payload["data"].get("event"),
            payload=payload,
        )
