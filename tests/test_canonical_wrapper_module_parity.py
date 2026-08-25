# import importlib

# import pytest


# WRAPPER_IMPL_MAPPINGS = [
#     ("apps.ridehail.driver.app", "apps.ridehail.driver.app_impl", "DriverApp"),
#     ("apps.ridehail.driver.agent", "apps.ridehail.driver.agent_impl", "DriverAgentIndie"),
#     ("apps.ridehail.driver.manager", "apps.ridehail.driver.manager_impl", "DriverManager"),
#     (
#         "apps.ridehail.driver.trip_manager",
#         "apps.ridehail.driver.trip_manager_impl",
#         "DriverTripManager",
#     ),
#     (
#         "apps.ridehail.passenger.app",
#         "apps.ridehail.passenger.app_impl",
#         "PassengerApp",
#     ),
#     (
#         "apps.ridehail.passenger.agent",
#         "apps.ridehail.passenger.agent_impl",
#         "PassengerAgentIndie",
#     ),
#     (
#         "apps.ridehail.passenger.manager",
#         "apps.ridehail.passenger.manager_impl",
#         "PassengerManager",
#     ),
#     (
#         "apps.ridehail.passenger.trip_manager",
#         "apps.ridehail.passenger.trip_manager_impl",
#         "PassengerTripManager",
#     ),
#     (
#         "apps.ridehail.assignment.app",
#         "apps.ridehail.assignment.app_impl",
#         "AssignmentApp",
#     ),
#     (
#         "apps.ridehail.assignment.agent",
#         "apps.ridehail.assignment.agent_impl",
#         "AssignmentAgentIndie",
#     ),
#     (
#         "apps.ridehail.assignment.manager",
#         "apps.ridehail.assignment.manager_impl",
#         "AssignmentManager",
#     ),
#     (
#         "apps.ridehail.analytics.app",
#         "apps.ridehail.analytics.app_impl",
#         "AnalyticsApp",
#     ),
#     (
#         "apps.ridehail.analytics.agent",
#         "apps.ridehail.analytics.agent_impl",
#         "AnalyticsAgentIndie",
#     ),
# ]


# @pytest.mark.parametrize("wrapper_mod,impl_mod,class_name", WRAPPER_IMPL_MAPPINGS)
# def test_canonical_wrapper_exports_same_class_object_as_impl(
#     wrapper_mod, impl_mod, class_name
# ):
#     wrapper_module = importlib.import_module(wrapper_mod)
#     impl_module = importlib.import_module(impl_mod)
#     assert getattr(wrapper_module, class_name) is getattr(impl_module, class_name)
