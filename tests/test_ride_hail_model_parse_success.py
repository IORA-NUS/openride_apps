from apps.ridehail import (
    DriverWorkflowPayload,
    PassengerWorkflowPayload,
    RequestedTripPayload,
    RideHailActions,
)
from openride_apps.apps.ridehail.statemachine.events import RideHailEvents
from apps.ridehail.models import AssignedPayload


def test_requested_trip_payload_parse_preserves_fields():
    payload = {
        "action": RideHailActions.REQUESTED_TRIP,
        "passenger_id": "p1",
        "requested_trip": {"pickup_loc": "a", "dropoff_loc": "b"},
    }
    parsed = RequestedTripPayload.parse(payload)
    assert parsed is not None
    assert parsed.action == payload["action"]
    assert parsed.passenger_id == payload["passenger_id"]
    assert parsed.requested_trip == payload["requested_trip"]


def test_assigned_payload_parse_preserves_fields():
    payload = {"action": RideHailActions.ASSIGNED, "driver_id": "d1"}
    parsed = AssignedPayload.parse(payload)
    assert parsed is not None
    assert parsed.action == payload["action"]
    assert parsed.driver_id == payload["driver_id"]


def test_passenger_workflow_payload_parse_preserves_fields():
    payload = {
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "passenger_id": "p1",
        "data": {"event": RideHailEvents.PASSENGER_CONFIRMED_TRIP, "meta": {"k": "v"}},
    }
    parsed = PassengerWorkflowPayload.parse(payload)
    assert parsed is not None
    assert parsed.action == payload["action"]
    assert parsed.passenger_id == payload["passenger_id"]
    assert parsed.data == payload["data"]


def test_driver_workflow_payload_parse_preserves_fields():
    payload = {
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "driver_id": "d1",
        "data": {"event": RideHailEvents.DRIVER_CONFIRMED_TRIP, "meta": {"k": "v"}},
    }
    parsed = DriverWorkflowPayload.parse(payload)
    assert parsed is not None
    assert parsed.action == payload["action"]
    assert parsed.driver_id == payload["driver_id"]
    assert parsed.data == payload["data"]
