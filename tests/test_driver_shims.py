from apps.driver_app import DriverAgentIndie as LegacyDriverAgentFromPackage
from apps.driver_app import DriverApp as LegacyDriverAppFromPackage
from apps.driver_app import DriverManager as LegacyDriverManagerFromPackage
from apps.driver_app import DriverTripManager as LegacyDriverTripManagerFromPackage
from apps.driver_app.driver_agent_indie import DriverAgentIndie as LegacyDriverAgent
from apps.driver_app.driver_app import DriverApp as LegacyDriverApp
from apps.driver_app.driver_manager import DriverManager as LegacyDriverManager
from apps.driver_app.driver_trip_manager import DriverTripManager as LegacyDriverTripManager
from apps.ride_hail.driver.agent import DriverAgentIndie
from apps.ride_hail.driver.app import DriverApp
from apps.ride_hail.driver.manager import DriverManager
from apps.ride_hail.driver.trip_manager import DriverTripManager


def test_driver_shim_exports_match_canonical_classes():
    assert LegacyDriverApp is DriverApp
    assert LegacyDriverManager is DriverManager
    assert LegacyDriverTripManager is DriverTripManager
    assert LegacyDriverAgent is DriverAgentIndie


def test_driver_package_exports_match_canonical_classes():
    assert LegacyDriverAppFromPackage is DriverApp
    assert LegacyDriverManagerFromPackage is DriverManager
    assert LegacyDriverTripManagerFromPackage is DriverTripManager
    assert LegacyDriverAgentFromPackage is DriverAgentIndie


def test_driver_shims_resolve_to_canonical_module_objects():
    assert DriverApp.__module__ == "apps.ride_hail.driver.app_impl"
    assert DriverManager.__module__ == "apps.ride_hail.driver.manager_impl"
    assert DriverTripManager.__module__ == "apps.ride_hail.driver.trip_manager_impl"
    assert DriverAgentIndie.__module__ == "apps.ride_hail.driver.agent_impl"
