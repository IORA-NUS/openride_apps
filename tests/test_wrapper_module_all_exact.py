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


def test_legacy_wrapper_module_all_values_match_expected_symbols():
    assert legacy_driver_app_mod.__all__ == ["DriverApp"]
    assert legacy_driver_agent_mod.__all__ == ["DriverAgentIndie"]
    assert legacy_driver_manager_mod.__all__ == ["DriverManager"]
    assert legacy_driver_trip_manager_mod.__all__ == ["DriverTripManager"]

    assert legacy_passenger_app_mod.__all__ == ["PassengerApp"]
    assert legacy_passenger_agent_mod.__all__ == ["PassengerAgentIndie"]
    assert legacy_passenger_manager_mod.__all__ == ["PassengerManager"]
    assert legacy_passenger_trip_manager_mod.__all__ == ["PassengerTripManager"]

    assert legacy_assignment_app_mod.__all__ == ["AssignmentApp"]
    assert legacy_assignment_agent_mod.__all__ == ["AssignmentAgentIndie"]
    assert legacy_assignment_manager_mod.__all__ == ["AssignmentManager", "EngineManager"]

    assert legacy_analytics_app_mod.__all__ == ["AnalyticsApp"]
    assert legacy_analytics_agent_mod.__all__ == ["AnalyticsAgentIndie"]


def test_canonical_wrapper_module_all_values_match_expected_symbols():
    assert canonical_driver_app_mod.__all__ == ["DriverApp"]
    assert canonical_driver_agent_mod.__all__ == ["DriverAgentIndie"]
    assert canonical_driver_manager_mod.__all__ == ["DriverManager"]
    assert canonical_driver_trip_manager_mod.__all__ == ["DriverTripManager"]

    assert canonical_passenger_app_mod.__all__ == ["PassengerApp"]
    assert canonical_passenger_agent_mod.__all__ == ["PassengerAgentIndie"]
    assert canonical_passenger_manager_mod.__all__ == ["PassengerManager"]
    assert canonical_passenger_trip_manager_mod.__all__ == ["PassengerTripManager"]

    assert canonical_assignment_app_mod.__all__ == ["AssignmentApp"]
    assert canonical_assignment_agent_mod.__all__ == ["AssignmentAgentIndie"]
    assert canonical_assignment_manager_mod.__all__ == ["AssignmentManager"]

    assert canonical_analytics_app_mod.__all__ == ["AnalyticsApp"]
    assert canonical_analytics_agent_mod.__all__ == ["AnalyticsAgentIndie"]
