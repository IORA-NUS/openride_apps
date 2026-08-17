import random

from apps.container_logistics.scenario.order_demand import (
    default_peak_hours_curve,
    hourly_weights_from_spec,
    normalize_hourly_weights,
    parse_order_demand_curve,
    recommended_early_order_count,
    sample_request_time_step,
)


def test_default_curve_has_24_normalized_hours():
    curve = parse_order_demand_curve(None)
    assert len(curve["points"]) == 24
    weights = hourly_weights_from_spec(curve)
    assert len(weights) == 24
    assert abs(sum(weights) - 1.0) < 1e-9


def test_peak_hours_receive_more_samples():
    weights = hourly_weights_from_spec({"points": default_peak_hours_curve()})
    rng = random.Random(42)
    hours = []
    for _ in range(5000):
        step = sample_request_time_step(
            weights,
            simulation_end=10_000,
            simulation_days=1,
            step_interval_seconds=240,
            business_hour_start=0,
            business_hour_end=24,
            rng=rng,
        )
        hours.append((step * 240) // 3600 % 24)
    morning = sum(1 for h in hours if 7 <= h <= 10)
    night = sum(1 for h in hours if h <= 3 or h >= 22)
    assert morning > night * 2


def test_recommended_early_order_count_scales_with_fleet():
    assert recommended_early_order_count(200, 2000) == 64
    assert recommended_early_order_count(5, 50) == 10
    assert recommended_early_order_count(200, 10) == 10


def test_hour_six_gets_fewer_samples_than_morning_peak():
    weights = hourly_weights_from_spec({"points": default_peak_hours_curve()})
    rng = random.Random(99)
    hours = []
    for _ in range(8000):
        step = sample_request_time_step(
            weights,
            simulation_end=10_000,
            simulation_days=1,
            step_interval_seconds=240,
            rng=rng,
        )
        hours.append((step * 240) // 3600 % 24)
    at_six = sum(1 for h in hours if h == 6)
    at_nine = sum(1 for h in hours if h == 9)
    assert at_nine > at_six * 1.3


def test_default_curve_matches_observed_hourly_percentages():
    """default_peak_hours_curve should reflect the real observed 'Sum of count_pct'
    by-hour distribution (0,0,0,0,0,1,3,5,8,9,10,10,8,8,8,8,7,5,3,2,1,1,1,0),
    renormalized to sum to 1 (the raw percentages sum to 98 due to rounding)."""
    observed_pct = [0, 0, 0, 0, 0, 1, 3, 5, 8, 9, 10, 10,
                    8, 8, 8, 8, 7, 5, 3, 2, 1, 1, 1, 0]
    expected = [p / sum(observed_pct) for p in observed_pct]

    weights = hourly_weights_from_spec({"points": default_peak_hours_curve()})
    assert len(weights) == 24
    for hour, (got, want) in enumerate(zip(weights, expected)):
        assert abs(got - want) < 1e-9, f"hour {hour}: expected {want}, got {got}"


def test_sparse_points_interpolate():
    points = [{"hour": 0, "weight": 0.1}, {"hour": 12, "weight": 1.0}, {"hour": 23, "weight": 0.1}]
    weights = normalize_hourly_weights(points)
    assert weights[12] > weights[0]
