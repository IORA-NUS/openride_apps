"""Tests for Kafka trip_geo_stream payload construction."""
from unittest.mock import patch

import polyline

from apps.ridehail.analytics.trip_geo_publisher import TripGeoPublisher, sim_clock_gmt_to_iso_z
from apps.ridehail.statemachine import RidehailDriverTripStateMachine


def test_sim_clock_gmt_to_iso_z():
    assert sim_clock_gmt_to_iso_z("Wed, 15 Apr 2026 12:00:00 GMT") == "2026-04-15T12:00:00.000Z"


def test_trip_route_emitted_with_polyline():
    sm = RidehailDriverTripStateMachine
    coords = [(1.290 + i * 0.002, 103.850 + i * 0.002) for i in range(10)]
    enc = polyline.encode(coords, geojson=False)

    trip = {
        "_id": "69dc8e42892961b424f14875",
        "driver": "69dc8e41af6060a99409c2e9",
        "passenger": "69dc8e41892961b424f1485d",
        "ridehail_passenger_trip": "69dc8e42892961b424f14867",
        "is_occupied": True,
        "state": sm.driver_moving_to_pickup.name,
        "routes": {"planned": {"moving_to_pickup": {"geometry": enc, "duration": 100, "distance": 500}}},
    }

    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    pub = TripGeoPublisher("run_test")
    with patch("apps.ridehail.analytics.trip_geo_publisher.push_trip_geo_to_topic", side_effect=capture):
        with patch("apps.ridehail.analytics.trip_geo_publisher.flush_trip_geo_producer"):
            pub.publish_from_driver_trips({"d1": trip}, "Wed, 15 Apr 2026 12:00:00 GMT")

    assert len(captured) >= 1
    msg = captured[0]
    assert msg["type"] == "trip_route"
    assert msg["run_id"] == "run_test"
    assert msg["passenger_trip_id"] == "69dc8e42892961b424f14867"
    assert msg["geometry_encoding"] == "polyline"
    assert msg["geometry"] == enc
    assert msg["leg"] == "moving_to_pickup"


def test_trip_end_when_trip_disappears():
    sm = RidehailDriverTripStateMachine
    coords = [(1.290 + i * 0.002, 103.850 + i * 0.002) for i in range(10)]
    enc = polyline.encode(coords, geojson=False)

    trip = {
        "_id": "dt",
        "driver": "drv",
        "passenger": "pas",
        "ridehail_passenger_trip": "ptx",
        "is_occupied": True,
        "state": sm.driver_moving_to_pickup.name,
        "routes": {"planned": {"moving_to_pickup": {"geometry": enc, "duration": 1, "distance": 1}}},
    }

    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    pub = TripGeoPublisher("run_x")
    with patch("apps.ridehail.analytics.trip_geo_publisher.push_trip_geo_to_topic", side_effect=capture):
        with patch("apps.ridehail.analytics.trip_geo_publisher.flush_trip_geo_producer"):
            pub.publish_from_driver_trips({"d1": trip}, "Wed, 15 Apr 2026 12:00:00 GMT")
            captured.clear()
            pub.publish_from_driver_trips({}, "Wed, 15 Apr 2026 12:00:01 GMT")

    assert any(m.get("type") == "trip_end" and m.get("passenger_trip_id") == "ptx" for m in captured)
