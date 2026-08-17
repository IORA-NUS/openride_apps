import random

import pytest

from apps.container_logistics.scenario import location_sampler, scenario_config


def test_sample_pickup_delivery_codes_matches_provided_matrix():
    """Empirical frequencies converge to the (normalized) matrix that is *provided*
    to the sampler. datagen holds no built-in default; the config-layer
    DEFAULT_TRIP_MATRIX is used here as the supplied matrix. Codes are whatever the
    matrix contains — no hardcoded code list."""
    rng = random.Random(1234)
    samples = 20000
    matrix = location_sampler.parse_trip_matrix(scenario_config.DEFAULT_TRIP_MATRIX)

    counts: dict[tuple[str, str], int] = {}
    for _ in range(samples):
        pair = location_sampler.sample_pickup_delivery_codes(rng=rng, matrix=matrix)
        counts[pair] = counts.get(pair, 0) + 1

    for pickup, row in matrix.items():
        for delivery, prob in row.items():
            expected_pct = 100 * prob
            empirical_pct = 100 * counts.get((pickup, delivery), 0) / samples
            assert abs(empirical_pct - expected_pct) <= 1.5, (
                f"{pickup}->{delivery}: expected ~{expected_pct:.2f}%, got {empirical_pct:.2f}%"
            )


def test_sample_requires_a_matrix():
    """No matrix provided -> explicit error (no hidden default in datagen)."""
    with pytest.raises(ValueError):
        location_sampler.sample_pickup_delivery_codes(rng=random.Random(0), matrix=None)


def test_parse_trip_matrix_raises_on_empty():
    with pytest.raises(ValueError):
        location_sampler.parse_trip_matrix({"CT": {"CT": 5}})  # all-diagonal -> empty


def test_sample_pickup_delivery_codes_only_returns_matrix_codes():
    rng = random.Random(42)
    matrix = location_sampler.parse_trip_matrix(scenario_config.DEFAULT_TRIP_MATRIX)
    known_codes = set(matrix)
    for _ in range(200):
        pickup, delivery = location_sampler.sample_pickup_delivery_codes(rng=rng, matrix=matrix)
        assert pickup in known_codes
        assert delivery in known_codes


def test_location_type_for_code_mapping():
    # Labels come from the config-layer metadata (no hardcoded table in datagen).
    assert location_sampler.location_type_for_code("CT") == "Port"
    assert location_sampler.location_type_for_code("CU") == "Warehouse"
    assert location_sampler.location_type_for_code("MT") == "Depot"
    # A code with no metadata entry derives its label from the code itself.
    assert location_sampler.location_type_for_code("YD") == "Yd"
    assert location_sampler.location_type_for_code("RL") == "Rl"
