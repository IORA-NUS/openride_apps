"""Curve — a weighted temporal distribution over bins (default: 24 hours).

Wraps the pure, behavior-tested ``sampling.sample_request_time_step`` so the
default order policy's request-time sampling is unchanged. ``sample(rng)`` returns
a weighted hour-of-day; ``sample_step(...)`` returns a scheduler step index within
the calendar (what the order policy actually needs).
"""

from __future__ import annotations

import random
from typing import Optional, Sequence

from ..sampling import sample_request_time_step
from .base import Distribution

HOURS_PER_DAY = 24


class Curve(Distribution):
    def __init__(
        self,
        bin_weights: Sequence[float],
        *,
        business_hour_start: int = 0,
        business_hour_end: int = 24,
    ):
        self.bin_weights = list(bin_weights)
        self.business_hour_start = int(business_hour_start)
        self.business_hour_end = int(business_hour_end)

    @classmethod
    def from_hourly_weights(
        cls,
        weights: Sequence[float],
        *,
        business_hour_start: int = 0,
        business_hour_end: int = 24,
    ) -> "Curve":
        return cls(
            list(weights),
            business_hour_start=business_hour_start,
            business_hour_end=business_hour_end,
        )

    @classmethod
    def from_records(
        cls,
        records,
        *,
        hour_field: str = "hour",
        business_hour_start: int = 0,
        business_hour_end: int = 24,
    ) -> "Curve":
        """Fit 24 hourly weights from records' arrival hours (histogram, normalized)."""
        counts = [0.0] * HOURS_PER_DAY
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            val = rec.get(hour_field)
            if val is None:
                continue
            try:
                hour = int(round(float(val))) % HOURS_PER_DAY
            except (TypeError, ValueError):
                continue
            counts[hour] += 1.0
        total = sum(counts)
        weights = [c / total for c in counts] if total > 0 else [1.0 / HOURS_PER_DAY] * HOURS_PER_DAY
        return cls(
            weights,
            business_hour_start=business_hour_start,
            business_hour_end=business_hour_end,
        )

    @staticmethod
    def records_have_hours(records, hour_field: str = "hour") -> bool:
        return any(isinstance(r, dict) and r.get(hour_field) is not None for r in (records or []))

    def sample(self, rng: random.Random) -> int:
        """Weighted hour-of-day within the business window."""
        bh_start = max(0, min(23, self.business_hour_start))
        bh_end = max(bh_start + 1, min(24, self.business_hour_end))
        masked = list(self.bin_weights)
        mask_sum = sum(masked[h] for h in range(bh_start, bh_end)) if masked else 0
        if mask_sum <= 0:
            return rng.randint(bh_start, bh_end - 1)
        r = rng.random() * mask_sum
        acc = 0.0
        for h in range(bh_start, bh_end):
            acc += masked[h]
            if r <= acc:
                return h
        return bh_end - 1

    def sample_step(
        self,
        rng: random.Random,
        *,
        simulation_end: int,
        simulation_days: int,
        step_interval_seconds: int,
    ) -> int:
        """Scheduler step index weighted by hour-of-day (behavior-preserving)."""
        return sample_request_time_step(
            self.bin_weights,
            simulation_end=simulation_end,
            simulation_days=simulation_days,
            step_interval_seconds=step_interval_seconds,
            business_hour_start=self.business_hour_start,
            business_hour_end=self.business_hour_end,
            rng=rng,
        )
