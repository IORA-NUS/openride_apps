from apps.ride_hail.analytics import AnalyticsAgentIndie, AnalyticsApp
from apps.ride_hail.assignment import AssignmentAgentIndie, AssignmentApp
from apps.ride_hail.driver import DriverAgentIndie, DriverApp, DriverManager, DriverTripManager
from apps.ride_hail.passenger import PassengerAgentIndie, PassengerApp, PassengerManager, PassengerTripManager


def test_ride_hail_driver_package_exports_existing_classes():
    assert DriverApp.__name__ == "DriverApp"
    assert DriverAgentIndie.__name__ == "DriverAgentIndie"
    assert DriverManager.__name__ == "DriverManager"
    assert DriverTripManager.__name__ == "DriverTripManager"


def test_ride_hail_passenger_package_exports_existing_classes():
    assert PassengerApp.__name__ == "PassengerApp"
    assert PassengerAgentIndie.__name__ == "PassengerAgentIndie"
    assert PassengerManager.__name__ == "PassengerManager"
    assert PassengerTripManager.__name__ == "PassengerTripManager"


def test_ride_hail_assignment_package_exports_existing_classes():
    assert AssignmentApp.__name__ == "AssignmentApp"
    assert AssignmentAgentIndie.__name__ == "AssignmentAgentIndie"


def test_ride_hail_analytics_package_exports_existing_classes():
    assert AnalyticsApp.__name__ == "AnalyticsApp"
    assert AnalyticsAgentIndie.__name__ == "AnalyticsAgentIndie"
