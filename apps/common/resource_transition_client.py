import json

from apps.common.resource_client_mixin import get_http_session
from apps.config import settings


class ResourceTransitionClient:
    """Shared thin HTTP client for resource transition requests.

    State-machine transitions (every haul-trip PATCH, every gate transition,
    …) flow through this client. Bare ``requests.patch`` here used to open a
    fresh TCP connection per call, undoing the pooling on the mixin side.
    We now go through the shared connection-pooled session.
    """

    def __init__(self, timeout=None):
        self.timeout = settings.get("NETWORK_REQUEST_TIMEOUT", 10) if timeout is None else timeout

    def patch(self, item_url: str, headers: dict, payload: dict):
        return get_http_session().patch(
            item_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=self.timeout,
        )

    def post(self, item_url: str, headers: dict, payload: dict):
        return get_http_session().post(
            item_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=self.timeout,
        )

    def get(self, item_url: str, headers: dict):
        return get_http_session().get(
            item_url,
            headers=headers,
            timeout=self.timeout,
        )
