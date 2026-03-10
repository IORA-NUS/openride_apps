import pytest

import apps.assignment_app as assignment_pkg
import apps.driver_app as driver_pkg
import apps.passenger_app as passenger_pkg
from apps.assignment_app.engine_manager import AssignmentManager, EngineManager
from apps.driver_app.driver_agent_indie import DriverAgentIndie
from apps.driver_app.driver_app import DriverApp
from apps.driver_app.driver_manager import DriverManager
from apps.driver_app.driver_trip_manager import DriverTripManager
from apps.passenger_app.passenger_agent_indie import PassengerAgentIndie
from apps.passenger_app.passenger_app import PassengerApp
from apps.passenger_app.passenger_manager import PassengerManager
from apps.passenger_app.passenger_trip_manager import PassengerTripManager


def test_driver_package_getattr_dispatches_to_expected_objects():
    assert driver_pkg.__getattr__("DriverApp") is DriverApp
    assert driver_pkg.__getattr__("DriverAgentIndie") is DriverAgentIndie
    assert driver_pkg.__getattr__("DriverManager") is DriverManager
    assert driver_pkg.__getattr__("DriverTripManager") is DriverTripManager


def test_passenger_package_getattr_dispatches_to_expected_objects():
    assert passenger_pkg.__getattr__("PassengerApp") is PassengerApp
    assert passenger_pkg.__getattr__("PassengerAgentIndie") is PassengerAgentIndie
    assert passenger_pkg.__getattr__("PassengerManager") is PassengerManager
    assert passenger_pkg.__getattr__("PassengerTripManager") is PassengerTripManager


def test_assignment_package_getattr_dispatches_to_expected_objects():
    assert assignment_pkg.__getattr__("AssignmentManager") is AssignmentManager
    assert assignment_pkg.__getattr__("EngineManager") is EngineManager


def test_legacy_package_getattr_unknown_errors_include_attribute_name():
    for pkg in (driver_pkg, passenger_pkg, assignment_pkg):
        with pytest.raises(AttributeError) as exc:
            pkg.__getattr__("DoesNotExist")
        assert "DoesNotExist" in str(exc.value)
