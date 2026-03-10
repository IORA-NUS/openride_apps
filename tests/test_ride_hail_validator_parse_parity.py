import pytest

from apps.ride_hail import (
    DriverWorkflowPayload,
    PassengerWorkflowPayload,
    RequestedTripPayload,
    RideHailActions,
    validate_assigned_payload,
    validate_driver_workflow_payload,
    validate_passenger_workflow_payload,
    validate_requested_trip_payload,
)
from apps.ride_hail.events import RideHailEvents
from apps.ride_hail.models import AssignedPayload


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": RideHailActions.REQUESTED_TRIP,
            "passenger_id": "p1",
            "requested_trip": {"pickup_loc": "a"},
        },
        {"action": RideHailActions.REQUESTED_TRIP, "passenger_id": "p1"},
        None,
    ],
)
def test_requested_trip_validator_matches_parse_semantics(payload):
    assert validate_requested_trip_payload(payload) == (RequestedTripPayload.parse(payload) is not None)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": RideHailActions.ASSIGNED, "driver_id": "d1"},
        {"action": RideHailActions.ASSIGNED},
        "bad",
    ],
)
def test_assigned_validator_matches_parse_semantics(payload):
    assert validate_assigned_payload(payload) == (AssignedPayload.parse(payload) is not None)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
            "passenger_id": "p1",
            "data": {"event": RideHailEvents.PASSENGER_CONFIRMED_TRIP},
        },
        {
            "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
            "passenger_id": "p1",
            "data": {},
        },
        {"action": "wrong"},
    ],
)
def test_passenger_workflow_validator_matches_parse_semantics(payload):
    assert validate_passenger_workflow_payload(payload) == (
        PassengerWorkflowPayload.parse(payload) is not None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
            "driver_id": "d1",
            "data": {"event": RideHailEvents.DRIVER_CONFIRMED_TRIP},
        },
        {
            "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
            "driver_id": "d1",
            "data": {},
        },
        {"action": "wrong"},
    ],
)
def test_driver_workflow_validator_matches_parse_semantics(payload):
    assert validate_driver_workflow_payload(payload) == (
        DriverWorkflowPayload.parse(payload) is not None
    )
