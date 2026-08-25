"""Pure time-of-day request sampling for order generation.

Generic weighted-hour math kept inside the isolated package so generation does
not depend on the scenario layer. The hourly weights themselves are computed by
the adapter (from the scenario's demand curve) and passed in.
"""

from __future__ import annotations

import random


def sample_request_time_step(
    hourly_weights: list[float],
    *,
    simulation_end: int,
    simulation_days: int,
    step_interval_seconds: int,
    business_hour_start: int = 0,
    business_hour_end: int = 24,
    rng: random.Random | None = None,
) -> int:
    """Sample a scheduler step index weighted by hour-of-day within business hours."""
    rng = rng or random
    steps_per_day = (24 * 3600) // step_interval_seconds
    bh_start = max(0, min(23, int(business_hour_start)))
    bh_end = max(bh_start + 1, min(24, int(business_hour_end)))

    masked = list(hourly_weights)
    mask_sum = sum(masked[h] for h in range(bh_start, bh_end))
    if mask_sum <= 0:
        hour = rng.randint(bh_start, bh_end - 1)
    else:
        r = rng.random() * mask_sum
        acc = 0.0
        hour = bh_start
        for h in range(bh_start, bh_end):
            acc += masked[h]
            if r <= acc:
                hour = h
                break

    day = rng.randint(0, max(0, simulation_days - 1))
    minute = rng.randint(0, 59)
    step_in_day = (hour * 3600 + minute * 60) // step_interval_seconds
    step = day * steps_per_day + step_in_day
    return min(max(0, step), simulation_end)
