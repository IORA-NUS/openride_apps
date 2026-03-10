import apps.assignment_app as legacy_assignment_pkg
import apps.driver_app as legacy_driver_pkg
import apps.passenger_app as legacy_passenger_pkg
import apps.ride_hail as ride_hail
import apps.ride_hail.adapters as ride_hail_adapters
import apps.ride_hail.analytics as canonical_analytics_pkg
import apps.ride_hail.assignment as canonical_assignment_pkg
import apps.ride_hail.driver as canonical_driver_pkg
import apps.ride_hail.passenger as canonical_passenger_pkg


def test_ride_hail_root_all_order_is_stable():
    assert ride_hail.__all__ == [
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
    ]


def test_ride_hail_adapters_all_order_is_stable():
    assert ride_hail_adapters.__all__ == [
        "RideHailAssignmentAdapter",
        "RideHailAnalyticsAdapter",
        "RideHailDriverAdapter",
        "RideHailPassengerAdapter",
    ]


def test_canonical_role_package_all_order_is_stable():
    assert canonical_driver_pkg.__all__ == [
        "DriverApp",
        "DriverAgentIndie",
        "DriverManager",
        "DriverTripManager",
    ]
    assert canonical_passenger_pkg.__all__ == [
        "PassengerApp",
        "PassengerAgentIndie",
        "PassengerManager",
        "PassengerTripManager",
    ]
    assert canonical_assignment_pkg.__all__ == [
        "AssignmentApp",
        "AssignmentAgentIndie",
        "AssignmentManager",
    ]
    assert canonical_analytics_pkg.__all__ == [
        "AnalyticsApp",
        "AnalyticsAgentIndie",
    ]


def test_legacy_role_package_all_order_is_stable():
    assert legacy_driver_pkg.__all__ == [
        "DriverManager",
        "DriverTripManager",
        "DriverApp",
        "DriverAgentIndie",
    ]
    assert legacy_passenger_pkg.__all__ == [
        "PassengerApp",
        "PassengerManager",
        "PassengerTripManager",
        "PassengerAgentIndie",
    ]
    assert legacy_assignment_pkg.__all__ == [
        "AssignmentAgentIndie",
        "AssignmentManager",
        "EngineManager",
    ]
