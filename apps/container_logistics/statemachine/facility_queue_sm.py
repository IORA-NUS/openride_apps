from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional

from .gate_sm import GateStateMachine


class FacilityQueueState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class FacilityQueueController:
    """FIFO queue allocator over one or more gates within a facility."""

    gate_count: int
    state: FacilityQueueState = FacilityQueueState.CLOSED
    queue: Deque[str] = field(default_factory=deque)
    gate_assignments: Dict[int, Optional[str]] = field(default_factory=dict)
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

    def enqueue_truck(self, truck_id: str) -> None:
        self.queue.append(truck_id)

    def assign_waiting_trucks(self, is_pickup_leg: bool) -> Dict[int, str]:
        """
        Assign trucks from queue to available gates in FIFO order.
        Returns map gate_index -> truck_id for assignments performed in this call.
        """
        assignments: Dict[int, str] = {}
        if self.state != FacilityQueueState.OPEN:
            return assignments

        for idx, gate in enumerate(self.gates):
            if not self.queue:
                break
            if gate.current_state.id != "available":
                continue
            truck_id = self.queue.popleft()
            if is_pickup_leg:
                gate.assign_pickup_truck()
            else:
                gate.assign_dropoff_truck()
            self.gate_assignments[idx] = truck_id
            assignments[idx] = truck_id

        return assignments

    def release_gate(self, gate_index: int) -> Optional[str]:
        """Marks gate service complete and frees it for next truck."""
        if gate_index < 0 or gate_index >= len(self.gates):
            raise IndexError("Invalid gate index")
        gate = self.gates[gate_index]
        if gate.current_state.id in {"busy_pickup", "busy_dropoff"}:
            gate.complete_service()
        truck_id = self.gate_assignments[gate_index]
        self.gate_assignments[gate_index] = None
        return truck_id
