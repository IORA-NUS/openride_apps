from apps.config import settings
from apps.common.resource_transition_client import ResourceTransitionClient
from typing import Any, Optional

class RoleTripManagerBase:
    def __init__(self, user_id: str, role: str):
        self.user_id = user_id
        self.role = role
        self.client = ResourceTransitionClient(settings.RESOURCE_TRANSITION_CLIENT_URL)

    def create_trip(self, trip_data: dict) -> Any:
        return self.client.create_trip(trip_data)

    def update_trip(self, trip_data: dict) -> Any:
        return self.client.update_trip(trip_data)

    def delete_trip(self, trip_id: str) -> Any:
        return self.client.delete_trip(trip_id)
