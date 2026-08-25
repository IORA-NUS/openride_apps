"""Order-demand curve normalization (pure, datagen-owned).

Copied verbatim from ``scenario.order_demand`` so datagen stays isolated; a parity
test guards the copy. Maps a sparse ``{points:[{hour,weight}]}`` spec into 24
normalized hourly weights the ``Curve`` distribution samples from.
"""

from __future__ import annotations

from typing import Any

HOURS_PER_DAY = 24
LEGACY_EARLY_ORDER_COUNT = 600


def recommended_early_order_count(num_trucks: int, num_orders: int) -> int:
    if num_orders <= 0:
        return 0
    trucks = max(1, int(num_trucks))
    warmup = min(trucks * 2, 64)
    return min(int(num_orders), max(1, warmup))


def default_peak_hours_curve() -> list[dict[str, float]]:
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


def normalize_hourly_weights(points: list[dict[str, float]]) -> list[float]:
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


def parse_order_demand_curve(raw: Any) -> dict[str, Any]:
    points = _coerce_points(raw)
    if points is None:
        points = default_peak_hours_curve()
    weights = normalize_hourly_weights(points)
    return {
        "resolution": "hour",
        "points": [{"hour": h, "weight": weights[h]} for h in range(HOURS_PER_DAY)],
    }


def hourly_weights_from_spec(raw: Any) -> list[float]:
    curve = parse_order_demand_curve(raw)
    return [float(p["weight"]) for p in curve["points"]]
