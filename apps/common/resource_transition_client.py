import json
import requests
from apps.config import settings

class ResourceTransitionClient:
    """Shared thin HTTP client for resource transition requests."""

    def __init__(self, timeout=None):
        self.timeout = settings.get("NETWORK_REQUEST_TIMEOUT", 10) if timeout is None else timeout

    def patch(self, item_url: str, headers: dict, payload: dict):
        return requests.patch(
            item_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=self.timeout,
        )

    def post(self, item_url: str, headers: dict, payload: dict):
        return requests.post(
            item_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=self.timeout,
        )

    def get(self, item_url: str, headers: dict):
        return requests.get(
            item_url,
            headers=headers,
            timeout=self.timeout,
        )
