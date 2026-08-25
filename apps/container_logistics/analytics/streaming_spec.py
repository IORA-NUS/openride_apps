"""
Container logistics map streaming contracts (Phase 0).

Authoritative prose spec:
  /home/user/docs/container_logistics_map_streaming_spec.md

TypeScript mirror:
  openride_server/analytics/types/containerLogisticsStreaming.ts
"""
from __future__ import annotations

from typing import Final, Literal, TypedDict

ECOSYSTEM: Final = "container_logistics"

# Kafka topics (see openride_apps/apps/config.py kafka_config)
TRIP_GEO_TOPIC_KEY: Final = "trip_geo"
FACILITY_STREAM_TOPIC_KEY: Final = "facility_stream"  # Phase 2 — add to kafka_config.topic_bootstrap

DEFAULT_FACILITY_RATE_MIN_ELAPSED_SIM_S: Final = 60
DEFAULT_FACILITY_STREAM_HEARTBEAT_S: Final = 30
# Legacy alias (pre-unified cumulative rates).
DEFAULT_FACILITY_RATE_WINDOW_SIM_S: Final = DEFAULT_FACILITY_RATE_MIN_ELAPSED_SIM_S

HaulTripLeg = Literal["repositioning_to_pickup", "loaded_to_dropoff"]
FacilityVisitType = Literal["pickup", "dropoff"]

# Haul states that emit route geometry (see spec §1)
HAUL_ROUTE_GEOMETRY_STATES: Final = frozenset(
    {"repositioning_to_pickup", "loaded_in_transit"}
)

HAUL_TERMINAL_STATES: Final = frozenset({"completed", "cancelled"})


class ContainerTripRoutePayload(TypedDict, total=False):
    type: Literal["trip_route"]
    ecosystem: Literal["container_logistics"]
    run_id: str
    sim_clock: str
    haul_trip_id: str
    truck_id: str
    truck_agent_id: str
    order_id: str
    haulier_id: str
    haulier_name: str
    leg: HaulTripLeg
    state: str
    geometry_encoding: Literal["polyline"]
    polyline_precision: Literal[5]
    geometry: str
    trip_start_sim_clock: str
    trip_end_sim_clock: None
    # Collaboration (haulier job sharing) — present only on cross-haulier hauls:
    # the truck's haulier (carrier) differs from the order's (owner). benefit_km =
    # deadhead saved vs the owner's best still-free own option at assignment time.
    shared: bool
    owner_haulier_id: str
    carrier_haulier_id: str
    benefit_km: float


class ContainerTripEndPayload(TypedDict, total=False):
    type: Literal["trip_end"]
    ecosystem: Literal["container_logistics"]
    run_id: str
    sim_clock: str
    haul_trip_id: str
    lifecycle_scope: Literal["haul_trip"]
    note: str


class TruckQueueEntryPayload(TypedDict, total=False):
    truck_id: str
    truck_agent_id: str
    visit_type: FacilityVisitType
    queued_at_sim_clock: str


class GateAssignmentPayload(TypedDict, total=False):
    gate_index: int
    truck_id: str
    truck_agent_id: str
    visit_type: FacilityVisitType


class FacilityRatesPayload(TypedDict, total=False):
    elapsed_sim_seconds: int
    window_sim_seconds: int  # legacy alias of elapsed_sim_seconds
    arrival_rate_per_hour: float | None
    service_rate_per_hour: float | None


class FacilitySnapshotPayload(TypedDict, total=False):
    type: Literal["facility_snapshot"]
    ecosystem: Literal["container_logistics"]
    run_id: str
    sim_clock: str
    facility_id: str
    name: str
    lng: float
    lat: float
    facility_type: str
    gate_count: int
    service_time_s: float
    queue: list[TruckQueueEntryPayload]
    gates: list[GateAssignmentPayload]
    rates: FacilityRatesPayload


def rate_per_hour_cumulative(
    total_events: int,
    elapsed_sim_seconds: float,
    *,
    min_elapsed_sim_seconds: float = DEFAULT_FACILITY_RATE_MIN_ELAPSED_SIM_S,
) -> float | None:
    """Average trucks per hour since facility open: total_events / elapsed_sim_hours."""
    if elapsed_sim_seconds < min_elapsed_sim_seconds or total_events < 0:
        return None
    return (total_events / elapsed_sim_seconds) * 3600.0


def rate_per_hour_from_events(event_count: int, window_sim_seconds: float) -> float | None:
    """Deprecated rolling-window helper; prefer ``rate_per_hour_cumulative``."""
    return rate_per_hour_cumulative(event_count, window_sim_seconds)


def facility_queue_pressure(
    gate_count: int,
    queue_len: int,
    busy_gates: int,
) -> float:
    """Normalized queue pressure in [0, 1] for UI glimmer (spec §5.4)."""
    gates = max(1, gate_count)
    total = queue_len + busy_gates
    return min(1.0, total / gates)
