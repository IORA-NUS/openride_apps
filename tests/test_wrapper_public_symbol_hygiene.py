import apps.analytics_app.analytics_agent_indie as legacy_analytics_agent_mod
import apps.analytics_app.analytics_app as legacy_analytics_app_mod
import apps.assignment_app.assignment_agent_indie as legacy_assignment_agent_mod
import apps.assignment_app.assignment_app as legacy_assignment_app_mod
import apps.assignment_app.engine_manager as legacy_assignment_manager_mod
import apps.driver_app.driver_agent_indie as legacy_driver_agent_mod
import apps.driver_app.driver_app as legacy_driver_app_mod
import apps.driver_app.driver_manager as legacy_driver_manager_mod
import apps.driver_app.driver_trip_manager as legacy_driver_trip_manager_mod
import apps.passenger_app.passenger_agent_indie as legacy_passenger_agent_mod
import apps.passenger_app.passenger_app as legacy_passenger_app_mod
import apps.passenger_app.passenger_manager as legacy_passenger_manager_mod
import apps.passenger_app.passenger_trip_manager as legacy_passenger_trip_manager_mod
import apps.ride_hail.analytics.agent as canonical_analytics_agent_mod
import apps.ride_hail.analytics.app as canonical_analytics_app_mod
import apps.ride_hail.assignment.agent as canonical_assignment_agent_mod
import apps.ride_hail.assignment.app as canonical_assignment_app_mod
import apps.ride_hail.assignment.manager as canonical_assignment_manager_mod
import apps.ride_hail.driver.agent as canonical_driver_agent_mod
import apps.ride_hail.driver.app as canonical_driver_app_mod
import apps.ride_hail.driver.manager as canonical_driver_manager_mod
import apps.ride_hail.driver.trip_manager as canonical_driver_trip_manager_mod
import apps.ride_hail.passenger.agent as canonical_passenger_agent_mod
import apps.ride_hail.passenger.app as canonical_passenger_app_mod
import apps.ride_hail.passenger.manager as canonical_passenger_manager_mod
import apps.ride_hail.passenger.trip_manager as canonical_passenger_trip_manager_mod


def _public_names(module_obj):
    return {name for name in module_obj.__dict__ if not name.startswith("_")}


def test_canonical_wrapper_modules_only_expose_expected_public_symbols():
    assert _public_names(canonical_driver_app_mod) == {"DriverApp"}
    assert _public_names(canonical_driver_agent_mod) == {"DriverAgentIndie"}
    assert _public_names(canonical_driver_manager_mod) == {"DriverManager"}
    assert _public_names(canonical_driver_trip_manager_mod) == {"DriverTripManager"}

    assert _public_names(canonical_passenger_app_mod) == {"PassengerApp"}
    assert _public_names(canonical_passenger_agent_mod) == {"PassengerAgentIndie"}
    assert _public_names(canonical_passenger_manager_mod) == {"PassengerManager"}
    assert _public_names(canonical_passenger_trip_manager_mod) == {"PassengerTripManager"}

    assert _public_names(canonical_assignment_app_mod) == {"AssignmentApp"}
    assert _public_names(canonical_assignment_agent_mod) == {"AssignmentAgentIndie"}
    assert _public_names(canonical_assignment_manager_mod) == {"AssignmentManager"}

    assert _public_names(canonical_analytics_app_mod) == {"AnalyticsApp"}
    assert _public_names(canonical_analytics_agent_mod) == {"AnalyticsAgentIndie"}


def test_legacy_wrapper_modules_only_expose_expected_public_symbols():
    assert _public_names(legacy_driver_app_mod) == {"DriverApp"}
    assert _public_names(legacy_driver_agent_mod) == {"DriverAgentIndie", "hs"}
    assert _public_names(legacy_driver_manager_mod) == {"DriverManager"}
    assert _public_names(legacy_driver_trip_manager_mod) == {"DriverTripManager"}

    assert _public_names(legacy_passenger_app_mod) == {"PassengerApp"}
    assert _public_names(legacy_passenger_agent_mod) == {"PassengerAgentIndie"}
    assert _public_names(legacy_passenger_manager_mod) == {"PassengerManager"}
    assert _public_names(legacy_passenger_trip_manager_mod) == {"PassengerTripManager"}

    assert _public_names(legacy_assignment_app_mod) == {"AssignmentApp"}
    assert _public_names(legacy_assignment_agent_mod) == {"AssignmentAgentIndie"}
    assert _public_names(legacy_assignment_manager_mod) == {"AssignmentManager", "EngineManager"}

    assert _public_names(legacy_analytics_app_mod) == {"AnalyticsApp"}
    assert _public_names(legacy_analytics_agent_mod) == {"AnalyticsAgentIndie"}
