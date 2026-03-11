from typing import Optional, Dict, Any

import json

import requests

from apps.config import settings
from apps.state_machine import WorkflowStateMachine
from apps.utils import is_success


class LifecycleManagerBase:
    """Shared state-transition helper methods for role managers."""

    def _get_transition_event(self, current_state: str, target_state: str) -> Optional[str]:
        machine = WorkflowStateMachine(start_value=current_state)
        for transition in machine.current_state.transitions:
            if transition.target.name == target_state:
                return transition.event
        return None

    def _transition_item_to_state(
        self,
        collection_url: str,
        item_doc: Dict[str, Any],
        sim_clock: str,
        target_state: str
    ) -> Dict[str, Any]:
        event = self._get_transition_event(item_doc["state"], target_state)
        if event is None:
            raise Exception(f"No transition from {item_doc['state']} to {target_state}")

        item_url = f"{collection_url}/{item_doc['_id']}"
        data = {
            "transition": event,
            "sim_clock": sim_clock,
        }

        requests.patch(
            item_url,
            headers=self.user.get_headers(etag=item_doc["_etag"]),
            data=json.dumps(data),
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )

        response = requests.get(
            item_url,
            headers=self.user.get_headers(),
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )
        if is_success(response.status_code):
            return response.json()
        raise Exception(f"{response.url}, {response.text}")
