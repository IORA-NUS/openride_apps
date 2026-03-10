import apps.assignment_app as assignment_pkg
import apps.driver_app as driver_pkg
import apps.passenger_app as passenger_pkg


def test_driver_package_all_matches_expected_symbols_exactly():
    expected = {
        "DriverManager",
        "DriverTripManager",
        "DriverApp",
        "DriverAgentIndie",
    }
    assert set(driver_pkg.__all__) == expected


def test_passenger_package_all_matches_expected_symbols_exactly():
    expected = {
        "PassengerApp",
        "PassengerManager",
        "PassengerTripManager",
        "PassengerAgentIndie",
    }
    assert set(passenger_pkg.__all__) == expected


def test_assignment_package_all_matches_expected_symbols_exactly():
    expected = {
        "AssignmentAgentIndie",
        "AssignmentManager",
        "EngineManager",
    }
    assert set(assignment_pkg.__all__) == expected
