from apps.config import settings

from .resource_transition_client import ResourceTransitionClient


class TripManagerBase:
    """Shared base utilities for ride-hail trip manager implementations."""

    # trip = None

    def __init__(self, run_id, user, messenger, persona):
        self.run_id = run_id
        self.user = user
        self.messenger = messenger
        self.persona = persona
        self._resource_client = ResourceTransitionClient()
        self.simulation_domain = None

        self.trip = None

    def _trip_collection_url(self):
        # return f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/{self.persona.get('role')}/ride_hail/trip"
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/{self.persona.get('role')}/trip"


    def _trip_item_url(self, suffix=None):
        if self.trip is None:
            raise Exception("trip is not set")
        base = f"{self._trip_collection_url()}/{self.trip['_id']}"
        return f"{base}/{suffix}" if suffix else base

    def _patch_trip(self, payload, suffix=None):
        return self._resource_client.patch(
            self._trip_item_url(suffix=suffix),
            headers=self.user.get_headers(etag=self.trip["_etag"]),
            payload=payload,
        )

    def _patch_trip_transition(self, transition, payload):
        return self._patch_trip(payload, suffix=transition)

    def _post_trip(self, payload):
        return self._resource_client.post(
            self._trip_collection_url(),
            headers=self.user.get_headers(),
            payload=payload,
        )

    def _get_trip(self):
        return self._resource_client.get(
            self._trip_item_url(),
            headers=self.user.get_headers(),
        )
