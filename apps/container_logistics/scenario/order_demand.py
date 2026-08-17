"""Hourly order-demand curve: defaults, normalization, and request-time sampling."""

from __future__ import annotations

import random
from typing import Any

HOURS_PER_DAY = 24

# Legacy warm-up stamped up to 600 orders into sim hour 0 (overrides the demand curve).
LEGACY_EARLY_ORDER_COUNT = 600


def recommended_early_order_count(num_trucks: int, num_orders: int) -> int:
    """
    Orders forced into sim hour 0 for dashboard warm-up.

    Must stay small vs fleet size so ``sample_request_time_step`` shapes the day;
    otherwise hundreds of orders launch at once and all trucks stay engaged.
    """
    if num_orders <= 0:
        return 0
    trucks = max(1, int(num_trucks))
    warmup = min(trucks * 2, 64)
    return min(int(num_orders), max(1, warmup))


def default_peak_hours_curve() -> list[dict[str, float]]:
    """Observed daily order-request distribution ("Sum of count_pct" by hour from
    production data): near-zero overnight, ramping through the morning, peaking
    ~9-11 and again in the early afternoon, then tapering off through the evening."""
    raw = [
        0, 0, 0, 0, 0, 1, 3, 5, 8, 9, 10, 10,
        8, 8, 8, 8, 7, 5, 3, 2, 1, 1, 1, 0,
    ]
    return [{"hour": h, "weight": w} for h, w in enumerate(raw)]


def _coerce_points(raw: Any) -> list[dict[str, float]] | None:
    if not isinstance(raw, dict):
        return None
    points = raw.get("points")
    if not isinstance(points, list) or not points:
        return None
    out: list[dict[str, float]] = []
    for item in points:
        if not isinstance(item, dict):
            continue
        try:
            hour = int(item.get("hour", item.get("t", -1)))
            weight = float(item.get("weight", 0))
        except (TypeError, ValueError):
            continue
        if 0 <= hour < HOURS_PER_DAY:
            out.append({"hour": hour, "weight": max(0.0, weight)})
    if not out:
        return None
    return out


def parse_order_demand_curve(raw: Any) -> dict[str, Any]:
    """Validate and normalize curve spec for API / GENERATION_SPEC."""
    points = _coerce_points(raw)
    if points is None:
        points = default_peak_hours_curve()
    weights = normalize_hourly_weights(points)
    return {
        "resolution": "hour",
        "points": [{"hour": h, "weight": weights[h]} for h in range(HOURS_PER_DAY)],
    }


def normalize_hourly_weights(points: list[dict[str, float]]) -> list[float]:
    """Map sparse control points to 24 hourly weights that sum to 1."""
    by_hour = {int(p["hour"]): float(p["weight"]) for p in points if 0 <= int(p["hour"]) < HOURS_PER_DAY}
    if not by_hour:
        by_hour = {int(p["hour"]): float(p["weight"]) for p in default_peak_hours_curve()}

    hours_sorted = sorted(by_hour.keys())
    weights = [0.0] * HOURS_PER_DAY
    for h in range(HOURS_PER_DAY):
        if h in by_hour:
            weights[h] = by_hour[h]
        else:
            lower = max((x for x in hours_sorted if x <= h), default=hours_sorted[0])
            upper = min((x for x in hours_sorted if x >= h), default=hours_sorted[-1])
            if lower == upper:
                weights[h] = by_hour[lower]
            else:
                t = (h - lower) / max(1, upper - lower)
                weights[h] = by_hour[lower] * (1 - t) + by_hour[upper] * t

    total = sum(weights)
    if total <= 0:
        return [1.0 / HOURS_PER_DAY] * HOURS_PER_DAY
    return [w / total for w in weights]


def hourly_weights_from_spec(raw: Any) -> list[float]:
    curve = parse_order_demand_curve(raw)
    return [float(p["weight"]) for p in curve["points"]]


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
    """Sample a scheduler step index weighted by hour-of-day."""
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
