"""Tests for FacilityQueueController — single FIFO gate queue."""
from __future__ import annotations

from apps.container_logistics.statemachine import (
    FacilityQueueController,
    FacilityVisitType,
)


def _open(gate_count: int) -> FacilityQueueController:
    c = FacilityQueueController(gate_count=gate_count)
    c.open_facility()
    return c


def test_fifo_order_single_gate():
    c = _open(gate_count=1)
    c.enqueue_truck("first", visit_type=FacilityVisitType.PICKUP)
    c.enqueue_truck("second", visit_type=FacilityVisitType.DROPOFF)

    assignments = c.assign_available_gates()
    assert len(assignments) == 1
    entry = next(iter(assignments.values()))
    assert entry.truck_id == "first"
    assert entry.visit_type == FacilityVisitType.PICKUP

    c.release_gate(0)
    assignments = c.assign_available_gates()
    assert len(assignments) == 1
    entry = next(iter(assignments.values()))
    assert entry.truck_id == "second"
    assert entry.visit_type == FacilityVisitType.DROPOFF


def test_multi_gate_drains_fifo():
    c = _open(gate_count=2)
    for i in range(4):
        c.enqueue_truck(f"truck_{i}", visit_type=FacilityVisitType.PICKUP)

    assignments = c.assign_available_gates()
    assert len(assignments) == 2
    served = [assignments[i].truck_id for i in sorted(assignments)]
    assert served == ["truck_0", "truck_1"]


def test_closed_facility_assigns_nothing():
    c = FacilityQueueController(gate_count=2)
    c.enqueue_truck("pick", visit_type=FacilityVisitType.PICKUP)
    assert c.assign_available_gates() == {}
