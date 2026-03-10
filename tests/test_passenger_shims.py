from apps.passenger_app import PassengerAgentIndie as LegacyPassengerAgentFromPackage
from apps.passenger_app import PassengerApp as LegacyPassengerAppFromPackage
from apps.passenger_app import PassengerManager as LegacyPassengerManagerFromPackage
from apps.passenger_app import (
    PassengerTripManager as LegacyPassengerTripManagerFromPackage,
)
from apps.passenger_app.passenger_agent_indie import (
    PassengerAgentIndie as LegacyPassengerAgent,
)
from apps.passenger_app.passenger_app import PassengerApp as LegacyPassengerApp
from apps.passenger_app.passenger_manager import PassengerManager as LegacyPassengerManager
from apps.passenger_app.passenger_trip_manager import (
    PassengerTripManager as LegacyPassengerTripManager,
)
from apps.ride_hail.passenger.agent import PassengerAgentIndie
from apps.ride_hail.passenger.app import PassengerApp
from apps.ride_hail.passenger.manager import PassengerManager
from apps.ride_hail.passenger.trip_manager import PassengerTripManager


def test_passenger_shim_exports_match_canonical_classes():
    assert LegacyPassengerApp is PassengerApp
    assert LegacyPassengerManager is PassengerManager
    assert LegacyPassengerTripManager is PassengerTripManager
    assert LegacyPassengerAgent is PassengerAgentIndie


def test_passenger_package_exports_match_canonical_classes():
    assert LegacyPassengerAppFromPackage is PassengerApp
    assert LegacyPassengerManagerFromPackage is PassengerManager
    assert LegacyPassengerTripManagerFromPackage is PassengerTripManager
    assert LegacyPassengerAgentFromPackage is PassengerAgentIndie


def test_passenger_shims_resolve_to_canonical_module_objects():
    assert PassengerApp.__module__ == "apps.ride_hail.passenger.app_impl"
    assert PassengerManager.__module__ == "apps.ride_hail.passenger.manager_impl"
    assert PassengerTripManager.__module__ == "apps.ride_hail.passenger.trip_manager_impl"
    assert PassengerAgentIndie.__module__ == "apps.ride_hail.passenger.agent_impl"
