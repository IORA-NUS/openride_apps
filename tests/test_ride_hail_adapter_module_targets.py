from apps.ride_hail import (
    RideHailAnalyticsAdapter,
    RideHailAssignmentAdapter,
    RideHailDriverAdapter,
    RideHailPassengerAdapter,
)


def test_driver_adapter_targets_impl_modules():
    assert RideHailDriverAdapter.get_app_class().__module__ == "apps.ride_hail.driver.app_impl"
    assert RideHailDriverAdapter.get_agent_class().__module__ == "apps.ride_hail.driver.agent_impl"


def test_passenger_adapter_targets_impl_modules():
    assert RideHailPassengerAdapter.get_app_class().__module__ == "apps.ride_hail.passenger.app_impl"
    assert RideHailPassengerAdapter.get_agent_class().__module__ == "apps.ride_hail.passenger.agent_impl"


def test_assignment_adapter_targets_impl_modules():
    assert RideHailAssignmentAdapter.get_app_class().__module__ == "apps.ride_hail.assignment.app_impl"
    assert RideHailAssignmentAdapter.get_agent_class().__module__ == "apps.ride_hail.assignment.agent_impl"


def test_analytics_adapter_targets_impl_modules():
    assert RideHailAnalyticsAdapter.get_app_class().__module__ == "apps.ride_hail.analytics.app_impl"
    assert RideHailAnalyticsAdapter.get_agent_class().__module__ == "apps.ride_hail.analytics.agent_impl"
