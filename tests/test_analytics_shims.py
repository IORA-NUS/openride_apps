from apps.analytics_app import AnalyticsAgentIndie as LegacyAnalyticsAgentFromPackage
from apps.analytics_app import AnalyticsApp as LegacyAnalyticsAppFromPackage
from apps.analytics_app.analytics_agent_indie import (
    AnalyticsAgentIndie as LegacyAnalyticsAgent,
)
from apps.analytics_app.analytics_app import AnalyticsApp as LegacyAnalyticsApp
from apps.ride_hail.analytics.agent import AnalyticsAgentIndie
from apps.ride_hail.analytics.app import AnalyticsApp


def test_analytics_shim_exports_match_canonical_classes():
    assert LegacyAnalyticsApp is AnalyticsApp
    assert LegacyAnalyticsAgent is AnalyticsAgentIndie


def test_analytics_package_exports_match_canonical_classes():
    assert LegacyAnalyticsAppFromPackage is AnalyticsApp
    assert LegacyAnalyticsAgentFromPackage is AnalyticsAgentIndie


def test_analytics_shims_resolve_to_canonical_module_objects():
    assert AnalyticsApp.__module__ == "apps.ride_hail.analytics.app_impl"
    assert AnalyticsAgentIndie.__module__ == "apps.ride_hail.analytics.agent_impl"
