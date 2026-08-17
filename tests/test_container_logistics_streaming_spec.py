"""Phase 0 — streaming spec helpers."""
from apps.container_logistics.analytics.streaming_spec import (
    facility_queue_pressure,
    rate_per_hour_cumulative,
    rate_per_hour_from_events,
)


def test_rate_per_hour_cumulative():
    assert rate_per_hour_cumulative(0, 3600) == 0.0
    assert rate_per_hour_cumulative(2, 3600) == 2.0
    assert rate_per_hour_cumulative(3, 900) == 12.0
    assert rate_per_hour_cumulative(1, 30) is None


def test_rate_per_hour_from_events():
    assert rate_per_hour_from_events(0, 900) == 0.0
    assert rate_per_hour_from_events(3, 900) == 12.0
    assert rate_per_hour_from_events(1, 30) is None


def test_facility_queue_pressure():
    assert facility_queue_pressure(2, 0, 0) == 0.0
    assert facility_queue_pressure(2, 2, 2) == 1.0
    assert facility_queue_pressure(1, 1, 0) == 1.0
