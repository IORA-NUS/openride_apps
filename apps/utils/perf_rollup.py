"""Running step-time aggregates for live perf_stream rollups."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class PerfRollup:
    step_count: int = 0
    sum_step_ms: float = 0.0
    max_step_ms: float = 0.0
    max_step_index: int = -1

    def observe(self, sim_step: int, step_wall_ms: float) -> bool:
        """Record a step and return True when this step sets a new running maximum."""
        self.step_count += 1
        self.sum_step_ms += step_wall_ms
        if step_wall_ms >= self.max_step_ms:
            is_new_max = step_wall_ms > self.max_step_ms or self.max_step_index < 0
            self.max_step_ms = step_wall_ms
            self.max_step_index = sim_step
            return is_new_max
        return False

    def to_dict(self) -> Dict[str, float | int]:
        avg = self.sum_step_ms / max(self.step_count, 1)
        return {
            "step_count": self.step_count,
            "avg_step_ms": round(avg, 2),
            "max_step_ms": round(self.max_step_ms, 2),
            "max_step_index": self.max_step_index,
        }
