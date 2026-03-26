from openride_apps.apps.ride_hail.statemachine.events import RideHailActions, RideHailEvents


def test_ride_hail_action_constants_match_expected_literals():
    assert RideHailActions.REQUESTED_TRIP == "requested_trip"
    assert RideHailActions.ASSIGNED == "assigned"
    assert RideHailActions.PASSENGER_WORKFLOW_EVENT == "passenger_workflow_event"
    assert RideHailActions.DRIVER_WORKFLOW_EVENT == "driver_workflow_event"


def test_ride_hail_action_constants_are_unique():
    values = {
        RideHailActions.REQUESTED_TRIP,
        RideHailActions.ASSIGNED,
        RideHailActions.PASSENGER_WORKFLOW_EVENT,
        RideHailActions.DRIVER_WORKFLOW_EVENT,
    }
    assert len(values) == 4


def test_ride_hail_event_constants_match_expected_literals():
    assert RideHailEvents.PASSENGER_CONFIRMED_TRIP == "passenger_confirmed_trip"
    assert RideHailEvents.PASSENGER_REJECTED_TRIP == "passenger_rejected_trip"
    assert RideHailEvents.PASSENGER_CANCEL_TRIP == "passenger_cancel_trip"
    assert RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP == "passenger_acknowledge_pickup"
    assert RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF == "passenger_acknowledge_dropoff"

    assert RideHailEvents.DRIVER_CONFIRMED_TRIP == "driver_confirmed_trip"
    assert RideHailEvents.DRIVER_CANCELLED_TRIP == "driver_cancelled_trip"
    assert RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP == "driver_arrived_for_pickup"
    assert RideHailEvents.DRIVER_MOVE_FOR_DROPOFF == "driver_move_for_dropoff"
    assert RideHailEvents.DRIVER_WAITING_FOR_DROPOFF == "driver_waiting_for_dropoff"
    assert RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF == "driver_arrived_for_dropoff"


def test_ride_hail_event_constants_are_unique():
    values = {
        RideHailEvents.PASSENGER_CONFIRMED_TRIP,
        RideHailEvents.PASSENGER_REJECTED_TRIP,
        RideHailEvents.PASSENGER_CANCEL_TRIP,
        RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP,
        RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF,
        RideHailEvents.DRIVER_CONFIRMED_TRIP,
        RideHailEvents.DRIVER_CANCELLED_TRIP,
        RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP,
        RideHailEvents.DRIVER_MOVE_FOR_DROPOFF,
        RideHailEvents.DRIVER_WAITING_FOR_DROPOFF,
        RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF,
    }
    assert len(values) == 11
