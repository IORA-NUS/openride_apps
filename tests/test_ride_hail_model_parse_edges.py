from apps.ride_hail import (
    DriverWorkflowPayload,
    PassengerWorkflowPayload,
    RequestedTripPayload,
    RideHailActions,
)
from apps.ride_hail.models import AssignedPayload


def test_requested_trip_payload_parse_rejects_invalid_shapes():
    assert RequestedTripPayload.parse(None) is None
    assert RequestedTripPayload.parse([]) is None
    assert RequestedTripPayload.parse({"action": "wrong"}) is None
    assert RequestedTripPayload.parse({"action": RideHailActions.REQUESTED_TRIP, "passenger_id": "p1"}) is None
    assert RequestedTripPayload.parse({"action": RideHailActions.REQUESTED_TRIP, "requested_trip": {}}) is None


def test_assigned_payload_parse_rejects_invalid_shapes():
    assert AssignedPayload.parse(None) is None
    assert AssignedPayload.parse("bad") is None
    assert AssignedPayload.parse({"action": "wrong", "driver_id": "d1"}) is None
    assert AssignedPayload.parse({"action": RideHailActions.ASSIGNED}) is None


def test_passenger_workflow_payload_parse_rejects_invalid_shapes():
    assert PassengerWorkflowPayload.parse(None) is None
    assert PassengerWorkflowPayload.parse({"action": "wrong"}) is None
    assert PassengerWorkflowPayload.parse(
        {"action": RideHailActions.PASSENGER_WORKFLOW_EVENT, "passenger_id": "p1", "data": None}
    ) is None
    assert PassengerWorkflowPayload.parse(
        {"action": RideHailActions.PASSENGER_WORKFLOW_EVENT, "passenger_id": "p1", "data": {}}
    ) is None
    assert PassengerWorkflowPayload.parse(
        {"action": RideHailActions.PASSENGER_WORKFLOW_EVENT, "data": {"event": "e"}}
    ) is None


def test_driver_workflow_payload_parse_rejects_invalid_shapes():
    assert DriverWorkflowPayload.parse(None) is None
    assert DriverWorkflowPayload.parse({"action": "wrong"}) is None
    assert DriverWorkflowPayload.parse(
        {"action": RideHailActions.DRIVER_WORKFLOW_EVENT, "driver_id": "d1", "data": None}
    ) is None
    assert DriverWorkflowPayload.parse(
        {"action": RideHailActions.DRIVER_WORKFLOW_EVENT, "driver_id": "d1", "data": {}}
    ) is None
    assert DriverWorkflowPayload.parse(
        {"action": RideHailActions.DRIVER_WORKFLOW_EVENT, "data": {"event": "e"}}
    ) is None
