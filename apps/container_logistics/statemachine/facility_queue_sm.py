from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional

from .gate_sm import GateStateMachine


class FacilityQueueState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class FacilityVisitType(str, Enum):
    PICKUP = "pickup"
    DROPOFF = "dropoff"


@dataclass(frozen=True)
class QueueEntry:
    """Truck waiting at or being served through a gate."""

    truck_id: str
    visit_type: FacilityVisitType


@dataclass
class FacilityQueueController:
    """FIFO gate queue for a facility — one queue, N parallel gates."""

    gate_count: int
    state: FacilityQueueState = FacilityQueueState.CLOSED
    queue: Deque[QueueEntry] = field(default_factory=deque)
    gate_assignments: Dict[int, Optional[QueueEntry]] = field(default_factory=dict)
    gates: List[GateStateMachine] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.gate_count < 1:
            raise ValueError("gate_count must be >= 1")
        self.gates = [GateStateMachine() for _ in range(self.gate_count)]
        self.gate_assignments = {i: None for i in range(self.gate_count)}

    def open_facility(self) -> None:
        self.state = FacilityQueueState.OPEN
        for gate in self.gates:
            if gate.current_state.id == "closed":
                gate.open()

    def close_facility(self) -> None:
        self.state = FacilityQueueState.CLOSED

    def enqueue_truck(self, truck_id: str, *, visit_type: FacilityVisitType | str) -> None:
        if isinstance(visit_type, str):
            visit_type = FacilityVisitType(visit_type)
        self.queue.append(QueueEntry(truck_id=truck_id, visit_type=visit_type))

    def assign_available_gates(self) -> Dict[int, QueueEntry]:
        """Assign trucks from the FIFO queue to every available gate."""
        assignments: Dict[int, QueueEntry] = {}
        if self.state != FacilityQueueState.OPEN:
            return assignments

        for idx, gate in enumerate(self.gates):
            if not self.queue:
                break
            if gate.current_state.id != "available":
                continue
            entry = self.queue.popleft()
            gate.assign_truck()
            self.gate_assignments[idx] = entry
            assignments[idx] = entry

        return assignments

    def release_gate(self, gate_index: int) -> Optional[QueueEntry]:
        """Mark gate service complete and free it for the next truck."""
        if gate_index < 0 or gate_index >= len(self.gates):
            raise IndexError("Invalid gate index")
        gate = self.gates[gate_index]
        if gate.current_state.id == "busy":
            gate.complete_service()
        entry = self.gate_assignments[gate_index]
        self.gate_assignments[gate_index] = None
        return entry

    def active_truck_ids(self) -> set[str]:
        """Truck ids currently assigned to a gate (in service)."""
        return {
            entry.truck_id
            for entry in self.gate_assignments.values()
            if entry is not None
        }
