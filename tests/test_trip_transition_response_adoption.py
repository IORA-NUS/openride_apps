"""
`apply_trip_transition_and_notify` used to always GET the document it had just PATCHed.
These tests pin the optimisation that skips that GET when the PATCH response already
carries the full trip document — and, just as importantly, pin the fallback: ride-hail's
endpoints still return Eve's thin body and MUST keep working.
"""

import json

from apps.common.trip_manager_base import TripManagerBase
from apps.container_logistics.statemachine import HaulTripStateMachine


class _DummyUser:
    def get_headers(self, etag=None):
        return {}


class _DummyClient:
    def __init__(self):
        self.published = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))


class _DummyMessenger:
    def __init__(self):
        self.client = _DummyClient()


class _Response:
    """Minimal stand-in for a `requests.Response`."""

    def __init__(self, status_code=200, body=None, raise_on_json=False):
        self.status_code = status_code
        self._body = body
        self._raise_on_json = raise_on_json
        self.url = "http://example/trip"
        self.text = "" if body is None else json.dumps(body, default=str)

    def json(self):
        if self._raise_on_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class _ProbeTripManager(TripManagerBase):
    """Concrete TripManagerBase that records GETs instead of doing HTTP."""

    def __init__(self, patch_response, refresh_doc=None):
        super().__init__("run-1", _DummyUser(), _DummyMessenger(), {"role": "truck"})
        self.simulation_domain = "container-logistics-sim"
        self.trip = {"_id": "trip-1", "state": "assigned", "_etag": "etag-old"}
        self._patch_response = patch_response
        self._refresh_doc = refresh_doc
        self.get_calls = 0
        self.refresh_calls = 0
        self.hook_calls = []

    # --- abstract surface -------------------------------------------------
    @property
    def StateMachineCls(self):
        return HaulTripStateMachine

    @property
    def message_channel(self):
        return None

    @property
    def statemachine_interaction_mapping(self):
        return []

    def message_template(self, event):
        return {"action": "x", "data": {"event": event}}

    # --- instrumentation --------------------------------------------------
    def _patch_trip_transition(self, transition, payload):
        return self._patch_response

    def _get_trip(self):
        self.get_calls += 1
        return _Response(200, self._refresh_doc)

    def refresh(self):
        self.refresh_calls += 1
        response = self._get_trip()
        self.trip = response.json()

    def post_transition_hook(self, source_transition, source_new_state, context=None):
        self.hook_calls.append((source_transition, source_new_state, context))


# Shaped to match what the rebuilt server actually returns, verified field-by-field
# against the GET it replaces: the re-read document plus `_status`, with a self-only
# `_links`. Hand-shrinking this fixture is how a stubbed-response test starts
# green-lighting a contract the wire does not honour (CLAUDE.md §6.15).
_FULL_DOC = {
    "_id": "trip-1",
    "_etag": "etag-new",
    "_created": "Mon, 01 Jan 2024 00:00:00 GMT",
    "_updated": "Mon, 01 Jan 2024 00:00:10 GMT",
    "_links": {"self": {"title": "trip", "href": "x/trip/trip-1"}},
    "_status": "OK",
    "state": "repositioning_to_pickup",
    "statemachine": {"name": "HaulTripStateMachine", "id": "sm-1"},
    "truck": "truck-9",
    "order": "order-9",
    "sim_clock": "Mon, 01 Jan 2024 00:00:10 GMT",
    "current_loc": {"type": "Point", "coordinates": [103.8, 1.3]},
    "meta": {"truck_profile": {}},
    "routes": {"planned": {}},
    "stats": {},
}

# Eve's default response to a successful PATCH: an etag, but no `state`.
_THIN_DOC = {
    "_id": "trip-1",
    "_etag": "etag-new",
    "_updated": "Mon, 01 Jan 2024 00:00:10 GMT",
    "_created": "Mon, 01 Jan 2024 00:00:00 GMT",
    "_status": "OK",
    "_links": {"self": {"href": "trip/trip-1"}},
}

# What the GET would have returned in the thin case.
_REFRESHED_DOC = dict(_FULL_DOC)


def test_full_document_response_is_adopted_without_a_get():
    mgr = _ProbeTripManager(_Response(200, _FULL_DOC), refresh_doc=_REFRESHED_DOC)

    mgr.apply_trip_transition_and_notify("start_empty_reposition", {"sim_clock": "x"})

    assert mgr.get_calls == 0, "the redundant GET must not be issued"
    assert mgr.refresh_calls == 0
    assert mgr.trip["state"] == "repositioning_to_pickup"
    assert mgr.trip["_etag"] == "etag-new"
    # message_template reads these three straight off self.trip.
    assert mgr.trip["truck"] == "truck-9"
    assert mgr.trip["order"] == "order-9"
    assert mgr.trip["sim_clock"] == "Mon, 01 Jan 2024 00:00:10 GMT"


def test_thin_eve_response_falls_back_to_refresh():
    mgr = _ProbeTripManager(_Response(200, _THIN_DOC), refresh_doc=_REFRESHED_DOC)

    mgr.apply_trip_transition_and_notify("start_empty_reposition", {"sim_clock": "x"})

    assert mgr.refresh_calls == 1, "_etag alone must not be mistaken for a full document"
    assert mgr.get_calls == 1
    assert mgr.trip["state"] == "repositioning_to_pickup"
    assert mgr.trip["_etag"] == "etag-new"


def test_non_json_body_falls_back_and_does_not_raise():
    mgr = _ProbeTripManager(
        _Response(200, None, raise_on_json=True), refresh_doc=_REFRESHED_DOC
    )

    mgr.apply_trip_transition_and_notify("start_empty_reposition", {"sim_clock": "x"})

    assert mgr.refresh_calls == 1
    assert mgr.trip["state"] == "repositioning_to_pickup"
    assert mgr.trip["_etag"] == "etag-new"


def test_non_dict_json_body_falls_back():
    mgr = _ProbeTripManager(_Response(200, ["not", "a", "doc"]), refresh_doc=_REFRESHED_DOC)

    mgr.apply_trip_transition_and_notify("start_empty_reposition", {"sim_clock": "x"})

    assert mgr.refresh_calls == 1
    assert mgr.trip["_etag"] == "etag-new"


def test_document_missing_etag_falls_back():
    """A body with `state` but no `_etag` would leave a stale If-Match -> 412 next PATCH."""
    body = {k: v for k, v in _FULL_DOC.items() if k != "_etag"}
    mgr = _ProbeTripManager(_Response(200, body), refresh_doc=_REFRESHED_DOC)

    mgr.apply_trip_transition_and_notify("start_empty_reposition", {"sim_clock": "x"})

    assert mgr.refresh_calls == 1
    assert mgr.trip["_etag"] == "etag-new"


def test_document_missing_id_keeps_the_id_we_already_hold():
    body = {k: v for k, v in _FULL_DOC.items() if k != "_id"}
    mgr = _ProbeTripManager(_Response(200, body), refresh_doc=_REFRESHED_DOC)

    mgr.apply_trip_transition_and_notify("start_empty_reposition", {"sim_clock": "x"})

    assert mgr.get_calls == 0
    assert mgr.trip["_id"] == "trip-1", "every later item URL is built from _id"


def test_post_transition_hook_gets_the_new_state_on_the_adopted_path():
    mgr = _ProbeTripManager(_Response(200, _FULL_DOC), refresh_doc=_REFRESHED_DOC)

    mgr.apply_trip_transition_and_notify(
        "start_empty_reposition", {"sim_clock": "x"}, context={"k": "v"}
    )

    assert mgr.hook_calls == [
        ("start_empty_reposition", "repositioning_to_pickup", {"k": "v"})
    ]


def test_post_transition_hook_gets_the_new_state_on_the_fallback_path():
    mgr = _ProbeTripManager(_Response(200, _THIN_DOC), refresh_doc=_REFRESHED_DOC)

    mgr.apply_trip_transition_and_notify(
        "start_empty_reposition", {"sim_clock": "x"}, context={"k": "v"}
    )

    assert mgr.hook_calls == [
        ("start_empty_reposition", "repositioning_to_pickup", {"k": "v"})
    ]


def test_failed_patch_neither_adopts_nor_refreshes():
    mgr = _ProbeTripManager(_Response(412, _FULL_DOC), refresh_doc=_REFRESHED_DOC)

    response = mgr.apply_trip_transition_and_notify("start_empty_reposition", {})

    assert response.status_code == 412
    assert mgr.refresh_calls == 0
    assert mgr.get_calls == 0
    assert mgr.hook_calls == []
    assert mgr.trip["state"] == "assigned"


def test_json_string_body_falls_back_without_raising():
    """A JSON body that parses to a STRING must fall back, not AttributeError.

    Guards the `isinstance(body, dict)` check specifically. A list body does NOT
    exercise it -- `'state' not in [...]` is already True, so the guard is skipped
    and the mutation that deletes it survives. A string is the shape that reaches
    `.get()` on a non-dict and escapes into the transition path.
    """
    mgr = _ProbeTripManager(_Response(200, '{"state": "not-a-dict"}'), refresh_doc=_REFRESHED_DOC)
    mgr.apply_trip_transition_and_notify("start_empty_reposition", {})
    assert mgr.get_calls == 1, "a string body must fall back to the GET"
    assert mgr.trip["_etag"] == "etag-new"


def test_projected_body_with_state_but_no_statemachine_falls_back():
    """`state` alone does not prove a full document.

    Eve honours `?projection=` on this route, so a projected body can carry `state`
    plus the auto-fields and nothing else. Adopting it would blank truck/order/
    sim_clock/meta/routes/stats -- a silent corruption, not a visible failure.
    """
    projected = {"_id": "trip-1", "_etag": "etag-new", "_status": "OK", "state": "repositioning_to_pickup"}
    mgr = _ProbeTripManager(_Response(200, projected), refresh_doc=_REFRESHED_DOC)
    mgr.apply_trip_transition_and_notify("start_empty_reposition", {})
    assert mgr.get_calls == 1, "a projected body must fall back to the GET"
    assert mgr.trip.get("truck") == "truck-9", "the refreshed doc must be intact"


def test_adoption_replaces_the_trip_rather_than_merging_into_it():
    """The adopted document must REPLACE the old trip, not merge over it.

    A merge leaves keys the server has since removed alive forever. Here the stale
    trip carries a `gate_index` the new document does not; after adoption it must
    be gone.
    """
    mgr = _ProbeTripManager(_Response(200, _FULL_DOC), refresh_doc=_REFRESHED_DOC)
    mgr.trip = {"_id": "trip-1", "_etag": "etag-old", "state": "assigned", "gate_index": 7}
    mgr.apply_trip_transition_and_notify("start_empty_reposition", {})
    assert mgr.get_calls == 0
    assert "gate_index" not in mgr.trip, "adoption must replace, not merge"
    assert mgr.trip["state"] == "repositioning_to_pickup"
