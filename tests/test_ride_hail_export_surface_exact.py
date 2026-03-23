import apps.ride_hail as ride_hail
import apps.ride_hail.adapters as ride_hail_adapters


def test_ride_hail_root_all_matches_expected_symbols_exactly():
    expected = {
        "RideHailActions",
        "RideHailEvents",
        "validate_assigned_payload",
        "validate_driver_workflow_payload",
        "validate_passenger_workflow_payload",
        "validate_requested_trip_payload",
        "RequestedTripPayload",
        "AssignedPayload",
        "PassengerWorkflowPayload",
        "DriverWorkflowPayload",
        "RideHailAssignmentAdapter",
        "RideHailAnalyticsAdapter",
        "RideHailDriverAdapter",
        "RideHailPassengerAdapter",
        "DriverApp",
        "DriverAgentIndie",
        "DriverManager",
        "DriverTripManager",
        "PassengerApp",
        "PassengerAgentIndie",
        "PassengerManager",
        "PassengerTripManager",
        "AssignmentApp",
        "AssignmentAgentIndie",
        "AnalyticsApp",
        "AnalyticsAgentIndie",
    }
    assert set(ride_hail.__all__) == expected


def test_ride_hail_adapters_all_matches_expected_symbols_exactly():
    expected = {
        "RideHailAssignmentAdapter",
        "RideHailAnalyticsAdapter",
        "RideHailDriverAdapter",
        "RideHailPassengerAdapter",
    }
    assert set(ride_hail_adapters.__all__) == expected
