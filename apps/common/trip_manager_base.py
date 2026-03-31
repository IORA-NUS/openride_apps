from abc import ABC, abstractmethod

from apps.config import settings
from .resource_transition_client import ResourceTransitionClient
from apps.utils import is_success, str_to_time
import json

class TripManagerBase(ABC):
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

    @property
    @abstractmethod
    def StateMachineCls(self):
        """
        Return the state machine class for the trip manager.
        """
        pass

    @property
    @abstractmethod
    def message_channel(self):
        """
        Return the message channel as string for the trip manager.
        """
        pass

    @property
    @abstractmethod
    def statemachine_interaction_mapping(self):
        """
        Return interaction rules dict for given event.
        """
        pass

    @abstractmethod
    def message_template(self, event):
        """
        Return message template dict for given event.
        """
        pass


    def post_transition_hook(self, source_transition, source_new_state, context=None):
        """
        Look up event from mapping using source_transition and source_new_state, then publish message.
        """
        event = None
        for rule in self.statemachine_interaction_mapping:
            if (
                rule.get('source_statemachine') == self.StateMachineCls.__name__ and
                rule.get('source_transition') == source_transition #and
                # rule.get('source_new_state') == source_new_state
            ):
                event = rule.get('event')
                break
        if not event:
            # Optionally log or raise if event not found
            return
        # msg = {
        #     'action': self.action_header,
        #     'driver_id': self.trip.get('driver'),
        #     'data': {
        #         'event': event
        #     }
        # }
        msg = self.message_template(event)

        if context:
            msg['data'].update(context)

        if self.message_channel is not None:
            self.messenger.client.publish(
                self.message_channel,
                json.dumps(msg)
            )

    def apply_trip_transition_and_notify(self, transition, data, context=None):
        # Save previous state before transition
        # prev_state = self.trip['state'] if self.trip else None
        response = self._patch_trip_transition(transition, data)
        # After transition, get new state
        if is_success(response.status_code):
            self.refresh()
            new_state = self.trip['state']
            self.post_transition_hook(transition, new_state, context=context)
        return response



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
