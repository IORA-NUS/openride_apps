from apps.container_logistics.contracts import validate_haulier_workflow_payload
from apps.container_logistics.events import ContainerLogisticsActions

from .base_interaction_adapter import BaseInteractionAdapter


class FacilityInteractionAdapter(BaseInteractionAdapter):
    """Minimal facility-side adapter to route haulier workflow events."""

    def handle_message(self, payload):
        if validate_haulier_workflow_payload(payload) is False:
            return False
        return self.dispatch_message(
            action=ContainerLogisticsActions.HAULIER_WORKFLOW_EVENT,
            event=payload["data"].get("event"),
            payload=payload,
        )
