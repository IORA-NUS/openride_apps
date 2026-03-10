from apps.ride_hail import (
    RideHailAnalyticsAdapter,
    RideHailAssignmentAdapter,
    RideHailDriverAdapter,
    RideHailPassengerAdapter,
)


def test_assignment_adapter_resolves_existing_classes():
    app_cls = RideHailAssignmentAdapter.get_app_class()
    agent_cls = RideHailAssignmentAdapter.get_agent_class()

    assert app_cls.__name__ == "AssignmentApp"
    assert agent_cls.__name__ == "AssignmentAgentIndie"


def test_analytics_adapter_resolves_existing_classes():
    app_cls = RideHailAnalyticsAdapter.get_app_class()
    agent_cls = RideHailAnalyticsAdapter.get_agent_class()

    assert app_cls.__name__ == "AnalyticsApp"
    assert agent_cls.__name__ == "AnalyticsAgentIndie"


def test_driver_adapter_resolves_existing_classes():
    app_cls = RideHailDriverAdapter.get_app_class()
    agent_cls = RideHailDriverAdapter.get_agent_class()

    assert app_cls.__name__ == "DriverApp"
    assert agent_cls.__name__ == "DriverAgentIndie"


def test_passenger_adapter_resolves_existing_classes():
    app_cls = RideHailPassengerAdapter.get_app_class()
    agent_cls = RideHailPassengerAdapter.get_agent_class()

    assert app_cls.__name__ == "PassengerApp"
    assert agent_cls.__name__ == "PassengerAgentIndie"
