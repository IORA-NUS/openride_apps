"""Phase 4: offline validation checks (no shapely / live server required)."""

import json

from apps.container_logistics.duration_constants import MIN_HAUL_TRIP_SECONDS
from apps.container_logistics.haul_trip_duration import (
    apply_haul_trip_duration_floors,
    modeled_haul_duration_seconds,
)
from apps.container_logistics.scenario.simulation_validation import (
    validate_completed_haul_trips,
    validate_order_request_times,
    validate_simulation_horizon,
    validate_truck_profile_trip_durations,
    validate_truck_shifts,
)
from apps.container_logistics.scenario.validation_config import (
    build_smoke_orsim_settings,
    smoke_simulation_length_in_steps,
)


def test_apply_haul_trip_duration_floors_meets_minimum():
    pickup, dropoff = apply_haul_trip_duration_floors(
        60,
        90,
        order={"pickup_service_time": 120, "dropoff_service_time": 120},
    )
    total = pickup + dropoff + 240
    assert total >= MIN_HAUL_TRIP_SECONDS


def test_modeled_haul_duration_seconds():
    trip = {
        "stats": {
            "estimated_time_to_pickup": 900,
            "estimated_time_to_dropoff": 960,
            "pickup_service_time": 120,
            "dropoff_service_time": 120,
        }
    }
    assert modeled_haul_duration_seconds(trip) >= MIN_HAUL_TRIP_SECONDS


def test_validate_simulation_horizon_smoke():
    settings = build_smoke_orsim_settings(domain="container-logistics-sim")
    check = validate_simulation_horizon(settings)
    assert check.passed
    assert settings["SIMULATION_LENGTH_IN_STEPS"] == smoke_simulation_length_in_steps()


def test_validate_truck_shifts_and_orders():
    end = smoke_simulation_length_in_steps() - 1
    trucks = {
        "truck_000001": {
            "shift_start_time": 0,
            "shift_end_time": end,
            "profile": {
                "estimated_time_to_pickup": 900,
                "estimated_time_to_dropoff": 960,
            },
        }
    }
    # Steps within smoke horizon (1 sim day @ 240s/step → 0–359).
    orders = {
        f"order_{i:06d}": {"request_time_step": 10 + i * 15}
        for i in range(20)
    }
    settings = build_smoke_orsim_settings(domain="test")
    assert validate_truck_shifts(trucks, settings).passed
    assert validate_order_request_times(orders, settings).passed
    assert validate_truck_profile_trip_durations(trucks).passed


def test_validate_completed_haul_trips_sample():
    trips = []
    for i in range(10):
        trips.append(
            {
                "_id": f"t{i}",
                "stats": {
                    "estimated_time_to_pickup": 900,
                    "estimated_time_to_dropoff": 960,
                    "pickup_service_time": 120,
                    "dropoff_service_time": 120,
                },
            }
        )
    check = validate_completed_haul_trips(trips, min_sample=5)
    assert check.passed
    details = check.details
    assert details["p50_seconds"] >= MIN_HAUL_TRIP_SECONDS


def test_validate_completed_haul_trips_fails_short():
    trips = [
        {
            "_id": "short",
            "stats": {
                "estimated_time_to_pickup": 60,
                "estimated_time_to_dropoff": 60,
                "pickup_service_time": 60,
                "dropoff_service_time": 60,
            },
        }
    ]
    check = validate_completed_haul_trips(trips, min_sample=1, min_fraction_above_min=1.0)
    assert not check.passed
