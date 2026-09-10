"""
Publish facility_snapshot messages to Kafka facility_stream for the analytics map.

See: docs/container_logistics_map_streaming_spec.md
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from apps.container_logistics.analytics.streaming_spec import (
    DEFAULT_FACILITY_STREAM_HEARTBEAT_S,
    ECOSYSTEM,
    rate_per_hour_cumulative,
)
from apps.container_logistics.facility.manager import FacilityManager
from apps.container_logistics.facility.service_time import resolve_service_time
from apps.container_logistics.statemachine import FacilityVisitType, QueueEntry
from apps.ridehail.analytics.trip_geo_publisher import sim_clock_gmt_to_iso_z
from apps.utils import str_to_time
from apps.container_logistics.analytics.facility_snapshot_persist import (
    persist_facility_snapshot,
)
from apps.utils.kafka_utils import flush_facility_stream_producer, push_facility_to_topic
from apps.utils.step_profile import span


def _sim_epoch_seconds(sim_clock_gmt: str) -> float:
    dt = str_to_time(sim_clock_gmt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _location_lng_lat(profile: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    loc = profile.get("location") or {}
    coords = loc.get("coordinates") if isinstance(loc, dict) else None
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            return float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            return None, None
    return None, None


def _truck_agent_label(truck_id: Any) -> str:
    tid = str(truck_id)
    if tid.startswith("truck_"):
        return tid
    if len(tid) > 10:
        return f"…{tid[-6:]}"
    return tid


class FacilitySnapshotPublisher:
    def __init__(
        self,
        run_id: str,
        facility_id: str,
        profile: Dict[str, Any],
        *,
        user=None,
        heartbeat_sim_s: int = DEFAULT_FACILITY_STREAM_HEARTBEAT_S,
    ):
        self.run_id = run_id
        self.facility_id = facility_id
        self.profile = profile
        self.user = user
        self.persist_snapshots = bool(profile.get("persist_facility_snapshots", True))
        self.heartbeat_sim_s = heartbeat_sim_s
        self._total_arrivals = 0
        self._total_services = 0
        self._opened_at_sim_s: Optional[float] = None
        self._queue_meta: Dict[str, str] = {}
        self._completed_wait_s: List[float] = []
        self._peak_queue_length: int = 0
        self._last_fingerprint: Optional[str] = None
        self._last_publish_sim_s: Optional[float] = None

    def _ensure_opened(self, sim_clock_gmt: str) -> float:
        sim_s = _sim_epoch_seconds(sim_clock_gmt)
        if self._opened_at_sim_s is None:
            self._opened_at_sim_s = sim_s
        return sim_s

    def record_enqueue(self, truck_id: Any, visit_type: FacilityVisitType | str, sim_clock_gmt: str) -> None:
        self._ensure_opened(sim_clock_gmt)
        self._total_arrivals += 1
        if isinstance(visit_type, FacilityVisitType):
            visit_type = visit_type.value
        self._queue_meta[f"{visit_type}:{truck_id}"] = sim_clock_gmt_to_iso_z(sim_clock_gmt)

    def record_service_complete(
        self,
        sim_clock_gmt: str,
        truck_id: Any = None,
        visit_type: Any = None,
    ) -> None:
        now_s = self._ensure_opened(sim_clock_gmt)
        self._total_services += 1
        if truck_id is not None and visit_type is not None:
            vt = visit_type.value if isinstance(visit_type, FacilityVisitType) else str(visit_type)
            key = f"{vt}:{truck_id}"
            queued_at_iso = self._queue_meta.pop(key, None)
            if queued_at_iso:
                try:
                    qt = datetime.fromisoformat(queued_at_iso.replace("Z", "+00:00"))
                    wait = now_s - qt.timestamp()
                    if wait >= 0:
                        self._completed_wait_s.append(wait)
                except Exception:
                    pass

    def kpi_stats(self) -> Dict[str, Any]:
        """Current queue KPI stats for persisting to the facility REST document."""
        avg_wait = (
            sum(self._completed_wait_s) / len(self._completed_wait_s)
            if self._completed_wait_s
            else 0.0
        )
        return {
            "avg_queue_wait_seconds": round(avg_wait, 2),
            "peak_queue_length": self._peak_queue_length,
        }

    def _elapsed_sim_seconds(self, now_sim_s: float) -> float:
        if self._opened_at_sim_s is None:
            return 0.0
        return max(0.0, now_sim_s - self._opened_at_sim_s)

    def _rates(self, now_sim_s: float) -> Dict[str, Any]:
        elapsed = self._elapsed_sim_seconds(now_sim_s)
        elapsed_int = int(elapsed)
        return {
            "elapsed_sim_seconds": elapsed_int,
            "window_sim_seconds": elapsed_int,
            "arrival_rate_per_hour": rate_per_hour_cumulative(self._total_arrivals, elapsed),
            "service_rate_per_hour": rate_per_hour_cumulative(self._total_services, elapsed),
        }

    def _queue_entry_payload(self, entry: QueueEntry) -> Dict[str, Any]:
        visit_type = (
            entry.visit_type.value
            if isinstance(entry.visit_type, FacilityVisitType)
            else str(entry.visit_type)
        )
        payload: Dict[str, Any] = {
            "truck_id": str(entry.truck_id),
            "truck_agent_id": _truck_agent_label(entry.truck_id),
            "visit_type": visit_type,
        }
        queued_at = self._queue_meta.get(f"{visit_type}:{entry.truck_id}")
        if queued_at:
            payload["queued_at_sim_clock"] = queued_at
        return payload

    def build_snapshot(
        self,
        manager: FacilityManager,
        behavior: Dict[str, Any],
        sim_clock_gmt: str,
    ) -> Optional[Dict[str, Any]]:
        lng, lat = _location_lng_lat(self.profile)
        if lng is None or lat is None:
            logging.debug(
                "facility_stream: skip publish — no profile.location for facility %s",
                self.facility_id,
            )
            return None

        qc = manager.queue_controller
        self._peak_queue_length = max(self._peak_queue_length, len(qc.queue))
        gate_assignments = manager._gate_assignments  # noqa: SLF001

        gates: List[Dict[str, Any]] = []
        for gate_index, entry in sorted(gate_assignments.items()):
            if entry is None:
                continue
            visit_type = (
                entry.visit_type.value
                if isinstance(entry.visit_type, FacilityVisitType)
                else str(entry.visit_type)
            )
            gates.append(
                {
                    "gate_index": int(gate_index),
                    "truck_id": str(entry.truck_id),
                    "truck_agent_id": _truck_agent_label(entry.truck_id),
                    "visit_type": visit_type,
                }
            )

        sim_iso = sim_clock_gmt_to_iso_z(sim_clock_gmt)
        now_sim_s = _sim_epoch_seconds(sim_clock_gmt)
        if self._opened_at_sim_s is None:
            self._opened_at_sim_s = now_sim_s

        service_time_s = resolve_service_time(behavior, self.profile)

        return {
            "type": "facility_snapshot",
            "ecosystem": ECOSYSTEM,
            "run_id": self.run_id,
            "sim_clock": sim_iso,
            "facility_id": self.facility_id,
            "name": str(self.profile.get("name") or self.facility_id),
            "lng": lng,
            "lat": lat,
            "facility_type": self.profile.get("facility_type"),
            "gate_count": int(qc.gate_count),
            "service_time_s": float(service_time_s or 0),
            "queue": [self._queue_entry_payload(entry) for entry in list(qc.queue)],
            "gates": gates,
            "rates": self._rates(now_sim_s),
            **self.kpi_stats(),
        }

    def _fingerprint(self, payload: Dict[str, Any]) -> str:
        subset = {
            "queue": payload.get("queue"),
            "gates": payload.get("gates"),
        }
        return json.dumps(subset, sort_keys=True, default=str)

    def maybe_publish(
        self,
        manager: FacilityManager,
        behavior: Dict[str, Any],
        sim_clock_gmt: str,
        *,
        force: bool = False,
    ) -> bool:
        with span("snap.build"):
            payload = self.build_snapshot(manager, behavior, sim_clock_gmt)
        if payload is None:
            return False

        sim_s = _sim_epoch_seconds(sim_clock_gmt)
        with span("snap.fingerprint"):
            fp = self._fingerprint(payload)
        heartbeat_due = (
            self._last_publish_sim_s is not None
            and sim_s - self._last_publish_sim_s >= self.heartbeat_sim_s
        )
        changed = fp != self._last_fingerprint
        gates_busy = bool(payload.get("gates"))

        if not force and not changed and not heartbeat_due and not gates_busy:
            return False
        if (
            not force
            and not changed
            and gates_busy
            and self._last_publish_sim_s is not None
            and sim_s - self._last_publish_sim_s < self.heartbeat_sim_s
        ):
            return False

        with span("snap.kafka_produce"):
            push_facility_to_topic(self.run_id, payload)
        if self.user and self.persist_snapshots:
            try:
                persist_facility_snapshot(self.user, self.run_id, payload)
            except Exception as exc:
                logging.warning(
                    "facility_snapshot persist failed for %s: %s",
                    self.facility_id,
                    exc,
                )
        self._last_fingerprint = fp
        self._last_publish_sim_s = sim_s
        with span("snap.kafka_flush"):
            flush_facility_stream_producer(timeout=0.5)
        return True
