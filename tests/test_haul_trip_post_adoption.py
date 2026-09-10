"""
`create_new_trip` adopts the POST it just sent instead of GETting the document back.

Background: the create path was `POST` then `refresh()` -- a GET that re-read the
document just written, once per haul trip (~15k per 1000-truck run) inside
`truck.consume_1`, the hottest span in the agent profile. A POST is reconstructible
where a PATCH is not: Eve stores exactly the payload sent, plus its own meta.

These tests pin the two things that make it safe:
  * `_etag` must come from the server, because `assign()` immediately sends it as
    If-Match -- a missing one must fall back to the GET rather than be invented, and
  * the adopted document must be COMPLETE (state, statemachine, truck, order...),
    because a stub would leave the truck holding a trip with no `state` if the
    following `assign()` PATCH failed.
"""
import pytest

from apps.container_logistics.truck.trip_manager import TruckTripManager


class _FakeResponse:
    def __init__(self, body, status_code=201):
        self._body = body
        self.status_code = status_code
        self.url = "http://test/haul_trip"
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Recorder(TruckTripManager):
    def __init__(self, post_body, status=201):
        # Bypass the real __init__ (it builds HTTP/messenger plumbing we do not want).
        self.run_id = "run_x"
        self.persona = {"role": "truck"}
        self.simulation_domain = "container-logistics-sim"
        self.trip = None
        self.refreshes = 0
        self._post_body = post_body
        self._post_status = status
        self.posted = None

    def _post_trip(self, data):
        self.posted = data
        return _FakeResponse(self._post_body, self._post_status)

    def refresh(self):
        self.refreshes += 1
        self.trip = {"_id": "from_get", "state": "created", "statemachine": {}, "_etag": "etag_get"}
        return self.trip

    def _facility_resource_id_by_profile_name(self, name):
        return None


def _order():
    return {"_id": "order1", "pickup_loc": (1.0, 2.0), "dropoff_loc": (3.0, 4.0),
            "profile": {}, "pickup_service_time": 60, "dropoff_service_time": 60}


def _truck():
    return {"_id": "truck1", "profile": {"estimated_time_to_pickup": 100,
                                         "estimated_time_to_dropoff": 200}}


def _create(mgr):
    return mgr.create_new_trip("2020-01-01T00:00:00", (0.0, 0.0), _truck(), _order())


def test_full_post_response_is_adopted_without_a_get():
    m = _Recorder({"_id": "trip1", "_etag": "etag1", "_status": "OK"})
    _create(m)
    assert m.refreshes == 0
    assert m.trip["_id"] == "trip1"
    assert m.trip["_etag"] == "etag1"


def test_adopted_document_is_complete_not_a_stub():
    """A stub with no `state` would strand the truck if the next PATCH failed."""
    m = _Recorder({"_id": "trip1", "_etag": "etag1"})
    _create(m)
    assert m.trip["state"] == "created"
    assert m.trip["statemachine"]["name"] == "HaulTripStateMachine"
    assert m.trip["truck"] == "truck1"
    assert m.trip["order"] == "order1"
    assert m.trip["feasible_transitions"] == []
    # everything we posted survived
    for k in m.posted:
        assert k in m.trip


def test_missing_etag_falls_back_to_the_get():
    """assign() sends _etag as If-Match immediately; never invent one."""
    m = _Recorder({"_id": "trip1"})          # no _etag
    _create(m)
    assert m.refreshes == 1
    assert m.trip["_etag"] == "etag_get"


def test_missing_id_falls_back_to_the_get():
    m = _Recorder({"_etag": "etag1"})        # no _id
    _create(m)
    assert m.refreshes == 1


def test_non_json_body_falls_back_to_the_get():
    m = _Recorder(ValueError("not json"))
    _create(m)
    assert m.refreshes == 1


def test_failed_post_raises_and_adopts_nothing():
    from apps.container_logistics.truck.trip_manager import WriteFailedException
    m = _Recorder({"_status": "ERR"}, status=422)
    with pytest.raises(WriteFailedException):
        _create(m)
    assert m.refreshes == 0
