import importlib

import pytest


WRAPPER_IMPL_MAPPINGS = [
    ("apps.ride_hail.driver.app", "apps.ride_hail.driver.app_impl", "DriverApp"),
    ("apps.ride_hail.driver.agent", "apps.ride_hail.driver.agent_impl", "DriverAgentIndie"),
    ("apps.ride_hail.driver.manager", "apps.ride_hail.driver.manager_impl", "DriverManager"),
    (
        "apps.ride_hail.driver.trip_manager",
        "apps.ride_hail.driver.trip_manager_impl",
        "DriverTripManager",
    ),
    (
        "apps.ride_hail.passenger.app",
        "apps.ride_hail.passenger.app_impl",
        "PassengerApp",
    ),
    (
        "apps.ride_hail.passenger.agent",
        "apps.ride_hail.passenger.agent_impl",
        "PassengerAgentIndie",
    ),
    (
        "apps.ride_hail.passenger.manager",
        "apps.ride_hail.passenger.manager_impl",
        "PassengerManager",
    ),
    (
        "apps.ride_hail.passenger.trip_manager",
        "apps.ride_hail.passenger.trip_manager_impl",
        "PassengerTripManager",
    ),
    (
        "apps.ride_hail.assignment.app",
        "apps.ride_hail.assignment.app_impl",
        "AssignmentApp",
    ),
    (
        "apps.ride_hail.assignment.agent",
        "apps.ride_hail.assignment.agent_impl",
        "AssignmentAgentIndie",
    ),
    (
        "apps.ride_hail.assignment.manager",
        "apps.ride_hail.assignment.manager_impl",
        "AssignmentManager",
    ),
    (
        "apps.ride_hail.analytics.app",
        "apps.ride_hail.analytics.app_impl",
        "AnalyticsApp",
    ),
    (
        "apps.ride_hail.analytics.agent",
        "apps.ride_hail.analytics.agent_impl",
        "AnalyticsAgentIndie",
    ),
]


@pytest.mark.parametrize("wrapper_mod,impl_mod,class_name", WRAPPER_IMPL_MAPPINGS)
def test_canonical_wrapper_exports_same_class_object_as_impl(
    wrapper_mod, impl_mod, class_name
):
    wrapper_module = importlib.import_module(wrapper_mod)
    impl_module = importlib.import_module(impl_mod)
    assert getattr(wrapper_module, class_name) is getattr(impl_module, class_name)
