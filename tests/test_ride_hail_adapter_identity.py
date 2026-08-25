from apps.ridehail import (
    AssignmentAgentIndie,
    AssignmentApp,
    DriverAgentIndie,
    DriverApp,
    PassengerAgentIndie,
    PassengerApp,
    RideHailAnalyticsAdapter,
    RideHailAssignmentAdapter,
    RideHailDriverAdapter,
    RideHailPassengerAdapter,
)
from apps.ridehail.adapters import (
    RideHailAnalyticsAdapter as AnalyticsAdapterFromAdapters,
)
from apps.ridehail.adapters import (
    RideHailAssignmentAdapter as AssignmentAdapterFromAdapters,
)
from apps.ridehail.adapters import RideHailDriverAdapter as DriverAdapterFromAdapters
from apps.ridehail.adapters import (
    RideHailPassengerAdapter as PassengerAdapterFromAdapters,
)
from apps.ridehail.analytics import AnalyticsAgentIndie, AnalyticsApp


def test_ride_hail_adapter_exports_are_consistent_between_packages():
    assert RideHailAssignmentAdapter is AssignmentAdapterFromAdapters
    assert RideHailAnalyticsAdapter is AnalyticsAdapterFromAdapters
    assert RideHailDriverAdapter is DriverAdapterFromAdapters
    assert RideHailPassengerAdapter is PassengerAdapterFromAdapters


def test_ride_hail_adapters_resolve_exact_canonical_class_objects():
    assert RideHailAssignmentAdapter.get_app_class() is AssignmentApp
    assert RideHailAssignmentAdapter.get_agent_class() is AssignmentAgentIndie

    assert RideHailAnalyticsAdapter.get_app_class() is AnalyticsApp
    assert RideHailAnalyticsAdapter.get_agent_class() is AnalyticsAgentIndie

    assert RideHailDriverAdapter.get_app_class() is DriverApp
    assert RideHailDriverAdapter.get_agent_class() is DriverAgentIndie

    assert RideHailPassengerAdapter.get_app_class() is PassengerApp
    assert RideHailPassengerAdapter.get_agent_class() is PassengerAgentIndie
