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

    def _adopt_trip_from_transition_response(self, response):
        """
        Try to use a transition PATCH's own response body as the new trip document.

        Why: the GET in `apply_trip_transition_and_notify` re-reads the exact document the
        PATCH just wrote. A haul trip makes ~8 transitions, so a 1000-truck run pays ~115k
        redundant blocking round-trips *inside* agent ticks, where every blocking call is
        barrier time. When the server echoes the full updated document, that read is free.

        Why `state` is the test (and `_status` is NOT): Eve's DEFAULT thin response carries
        `_id`/`_etag`/`_updated`/`_created`/`_status`/`_links` — it has `_etag` and it has
        `_status`, but it has no `state`. Keying off either of those would adopt the stub and
        blank every field callers read straight off `self.trip` (`truck`/`order`/`sim_clock`
        in `message_template`, plus `meta`/`routes`/`stats` elsewhere), silently corrupting
        the run instead of failing loudly. `state` is the one key that only a full document has.

        Why the caller's `refresh()` fallback is mandatory rather than belt-and-braces:
        ride-hail's endpoints are NOT being changed and will keep returning the thin body
        forever, so the fallback is the *normal* path for a whole ecosystem — correctness must
        never depend on this optimisation firing.

        Returns True only when `self.trip` now holds a usable full document.
        """
        try:
            body = response.json()
        except Exception:
            # Non-JSON / truncated / malformed body is not an error here — it just means
            # there is no shortcut to take. Never let it escape into the transition path.
            return False

        if not isinstance(body, dict):
            return False

        # `_etag` is required (not merged in afterwards) because the NEXT PATCH sends it as
        # `If-Match`. This endpoint folds that etag into the Mongo lookup, so a stale one does
        # not 412 -- it simply matches no document and 404s, which `is_success` then treats as
        # a failed transition. A body with no `_etag` at all is worse still: `_patch_trip`
        # would KeyError on `self.trip['_etag']`.
        #
        # `statemachine` is required ALONGSIDE `state` because `state` alone does not prove a
        # full document: Eve honours `?projection=` on this route, so a projected body can
        # carry `state` and the auto-fields and nothing else. Adopting that would blank
        # `truck`/`order`/`sim_clock`/`meta`/`routes`/`stats` and corrupt the run silently --
        # the §6.7 shape. `statemachine` is schema-`required`, so a genuine full document
        # always has it, and any partial projection that omits it falls back to the GET.
        if not body.get('state') or not body.get('statemachine') or not body.get('_etag'):
            return False

        if not body.get('_id'):
            # Eve always echoes `_id`, but every subsequent URL is built from it
            # (`_trip_item_url`), so never let a body that omits it drop the id we already hold.
            prev_id = (self.trip or {}).get('_id')
            if not prev_id:
                return False
            body['_id'] = prev_id

        self.trip = body
        return True

    def apply_trip_transition_and_notify(self, transition, data, context=None):
        # Save previous state before transition
        # prev_state = self.trip['state'] if self.trip else None
        response = self._patch_trip_transition(transition, data)
        # After transition, get new state
        if is_success(response.status_code):
            # Adopt the PATCH's own response body when it is a full trip document; only pay
            # for the extra GET when it is not. See `_adopt_trip_from_transition_response`.
            if not self._adopt_trip_from_transition_response(response):
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
