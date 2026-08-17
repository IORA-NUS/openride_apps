"""Tests for facility service_time resolution."""
from apps.container_logistics.facility.service_time import resolve_service_time


def test_resolve_service_time_prefers_unified_key():
    assert resolve_service_time({"service_time": 90}, {}) == 90


def test_resolve_service_time_legacy_fallback():
    assert resolve_service_time(
        {"pickup_service_time": 60, "dropoff_service_time": 120},
        {},
    ) == 120
