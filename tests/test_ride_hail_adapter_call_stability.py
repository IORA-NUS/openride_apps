from apps.ridehail import (
    RideHailAnalyticsAdapter,
    RideHailAssignmentAdapter,
    RideHailDriverAdapter,
    RideHailPassengerAdapter,
)


def _assert_adapter_calls_are_stable(adapter_cls):
    app_first = adapter_cls.get_app_class()
    app_second = adapter_cls.get_app_class()
    agent_first = adapter_cls.get_agent_class()
    agent_second = adapter_cls.get_agent_class()

    assert app_first is app_second
    assert agent_first is agent_second


def test_driver_adapter_calls_are_stable():
    _assert_adapter_calls_are_stable(RideHailDriverAdapter)


def test_passenger_adapter_calls_are_stable():
    _assert_adapter_calls_are_stable(RideHailPassengerAdapter)


def test_assignment_adapter_calls_are_stable():
    _assert_adapter_calls_are_stable(RideHailAssignmentAdapter)


def test_analytics_adapter_calls_are_stable():
    _assert_adapter_calls_are_stable(RideHailAnalyticsAdapter)
