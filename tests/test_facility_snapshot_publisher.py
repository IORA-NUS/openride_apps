"""Tests for facility_stream snapshot payloads."""
import json
from unittest.mock import patch

from apps.container_logistics.facility.facility_snapshot_publisher import (
    FacilitySnapshotPublisher,
)
from apps.container_logistics.statemachine import FacilityQueueController, FacilityVisitType


class _StubManager:
    def __init__(self):
        self.queue_controller = FacilityQueueController(gate_count=2)
        self.queue_controller.open_facility()
        self._gate_assignments = {}
        self.profile = {
            "name": "terminal_a",
            "location": {"type": "Point", "coordinates": [103.85, 1.29]},
            "service_time": 120,
        }


def test_snapshot_includes_queue_and_rates():
    mgr = _StubManager()
    mgr.queue_controller.enqueue_truck("truck_001", visit_type=FacilityVisitType.PICKUP)
    pub = FacilitySnapshotPublisher("run_x", "fac_1", mgr.profile)
    pub.record_enqueue("truck_001", FacilityVisitType.PICKUP, "Wed, 15 Apr 2026 12:00:00 GMT")

    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    with patch(
        "apps.container_logistics.facility.facility_snapshot_publisher.push_facility_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.facility.facility_snapshot_publisher.flush_facility_stream_producer"
        ):
            pub.maybe_publish(mgr, {"service_time": 120}, "Wed, 15 Apr 2026 12:00:00 GMT", force=True)

    assert len(captured) == 1
    msg = captured[0]
    assert msg["type"] == "facility_snapshot"
    assert msg["ecosystem"] == "container_logistics"
    assert msg["name"] == "terminal_a"
    assert len(msg["queue"]) == 1
    assert msg["queue"][0]["truck_agent_id"] == "truck_001"
    assert msg["queue"][0]["visit_type"] == "pickup"
    assert msg["service_time_s"] == 120.0
    assert "rates" in msg
    assert msg["rates"]["arrival_rate_per_hour"] is None  # < 60s elapsed at same timestamp


def test_cumulative_service_rate_over_elapsed_sim_time():
    mgr = _StubManager()
    pub = FacilitySnapshotPublisher("run_x", "fac_1", mgr.profile)
    pub.record_service_complete("Wed, 15 Apr 2026 12:00:00 GMT")
    pub.record_service_complete("Wed, 15 Apr 2026 12:30:00 GMT")

    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    with patch(
        "apps.container_logistics.facility.facility_snapshot_publisher.push_facility_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.facility.facility_snapshot_publisher.flush_facility_stream_producer"
        ):
            pub.maybe_publish(mgr, {"service_time": 120}, "Wed, 15 Apr 2026 13:00:00 GMT", force=True)

    rates = captured[0]["rates"]
    assert rates["elapsed_sim_seconds"] == 3600
    assert rates["service_rate_per_hour"] == 2.0


def test_dedupes_unchanged_snapshot():
    mgr = _StubManager()
    pub = FacilitySnapshotPublisher("run_x", "fac_1", mgr.profile)
    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    with patch(
        "apps.container_logistics.facility.facility_snapshot_publisher.push_facility_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.facility.facility_snapshot_publisher.flush_facility_stream_producer"
        ):
            pub.maybe_publish(mgr, {}, "Wed, 15 Apr 2026 12:00:00 GMT", force=True)
            pub.maybe_publish(mgr, {}, "Wed, 15 Apr 2026 12:00:01 GMT", force=False)

    assert len(captured) == 1


def test_persists_snapshot_when_user_provided():
    mgr = _StubManager()
    user = type("U", (), {"get_headers": lambda self: {"Content-Type": "application/json"}})()
    pub = FacilitySnapshotPublisher("run_x", "fac_1", mgr.profile, user=user)

    with patch(
        "apps.container_logistics.facility.facility_snapshot_publisher.push_facility_to_topic"
    ):
        with patch(
            "apps.container_logistics.facility.facility_snapshot_publisher.flush_facility_stream_producer"
        ):
            with patch(
                "apps.container_logistics.facility.facility_snapshot_publisher.persist_facility_snapshot"
            ) as persist:
                pub.maybe_publish(
                    mgr,
                    {},
                    "Wed, 15 Apr 2026 12:00:00 GMT",
                    force=True,
                )
                persist.assert_called_once()
