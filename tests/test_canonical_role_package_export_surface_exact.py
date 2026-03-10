import apps.ride_hail.analytics as analytics_pkg
import apps.ride_hail.assignment as assignment_pkg
import apps.ride_hail.driver as driver_pkg
import apps.ride_hail.passenger as passenger_pkg


def test_driver_role_package_all_matches_expected_symbols_exactly():
    expected = {
        "DriverApp",
        "DriverAgentIndie",
        "DriverManager",
        "DriverTripManager",
    }
    assert set(driver_pkg.__all__) == expected


def test_passenger_role_package_all_matches_expected_symbols_exactly():
    expected = {
        "PassengerApp",
        "PassengerAgentIndie",
        "PassengerManager",
        "PassengerTripManager",
    }
    assert set(passenger_pkg.__all__) == expected


def test_assignment_role_package_all_matches_expected_symbols_exactly():
    expected = {
        "AssignmentApp",
        "AssignmentAgentIndie",
        "AssignmentManager",
    }
    assert set(assignment_pkg.__all__) == expected


def test_analytics_role_package_all_matches_expected_symbols_exactly():
    expected = {
        "AnalyticsApp",
        "AnalyticsAgentIndie",
    }
    assert set(analytics_pkg.__all__) == expected
