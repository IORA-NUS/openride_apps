"""Tests for container logistics Kafka trip_geo_stream payloads."""
from unittest.mock import patch

import polyline

from apps.container_logistics.analytics.trip_geo_publisher import ContainerTripGeoPublisher
from apps.container_logistics.statemachine import HaulTripStateMachine


def _sample_polyline():
    coords = [(1.290 + i * 0.002, 103.850 + i * 0.002) for i in range(10)]
    return polyline.encode(coords, geojson=False)


def test_trip_route_emitted_for_repositioning_leg():
    enc = _sample_polyline()
    sm = HaulTripStateMachine
    trip = {
        "_id": "haul001",
        "truck": "truck001",
        "order": "order001",
        "state": sm.repositioning_to_pickup.name,
        "routes": {
            "planned": {
                "repositioning_to_pickup": {"geometry": enc, "duration": 100},
            }
        },
    }
    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    pub = ContainerTripGeoPublisher("run_cl")
    with patch(
        "apps.container_logistics.analytics.trip_geo_publisher.push_trip_geo_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.analytics.trip_geo_publisher.flush_trip_geo_producer"
        ):
            pub.publish_from_haul_trips([trip], "Wed, 15 Apr 2026 12:00:00 GMT")

    assert len(captured) == 1
    msg = captured[0]
    assert msg["type"] == "trip_route"
    assert msg["ecosystem"] == "container_logistics"
    assert msg["haul_trip_id"] == "haul001"
    assert msg["truck_id"] == "truck001"
    assert msg["order_id"] == "order001"
    assert msg["leg"] == "repositioning_to_pickup"
    assert msg["geometry"] == enc


def test_trip_end_when_haul_trip_leaves_active_set():
    enc = _sample_polyline()
    sm = HaulTripStateMachine
    trip = {
        "_id": "haul001",
        "truck": "truck001",
        "state": sm.loaded_in_transit.name,
        "routes": {
            "planned": {
                "loaded_to_dropoff": {"geometry": enc, "duration": 50},
            }
        },
    }
    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    pub = ContainerTripGeoPublisher("run_cl")
    with patch(
        "apps.container_logistics.analytics.trip_geo_publisher.push_trip_geo_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.analytics.trip_geo_publisher.flush_trip_geo_producer"
        ):
            pub.publish_from_haul_trips([trip], "Wed, 15 Apr 2026 12:00:00 GMT")
            captured.clear()
            pub.publish_from_haul_trips([], "Wed, 15 Apr 2026 12:00:01 GMT")

    assert any(
        m.get("type") == "trip_end"
        and m.get("haul_trip_id") == "haul001"
        and m.get("lifecycle_scope") == "haul_trip"
        for m in captured
    )


def test_fallback_geometry_from_trip_endpoints_when_planned_empty():
    sm = HaulTripStateMachine
    trip = {
        "_id": "haul003",
        "truck": "truck001",
        "state": sm.repositioning_to_pickup.name,
        "sim_clock": "Wed, 01 Jan 2020 22:50:30 GMT",
        "current_loc": {"type": "Point", "coordinates": [103.85, 1.29]},
        "pickup_loc": {"type": "Point", "coordinates": [103.90, 1.31]},
        "routes": {"planned": {"repositioning_to_pickup": None}},
    }
    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    pub = ContainerTripGeoPublisher("run_cl")
    with patch(
        "apps.container_logistics.analytics.trip_geo_publisher.push_trip_geo_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.analytics.trip_geo_publisher._osrm_polyline",
            return_value=None,
        ):
            with patch(
                "apps.container_logistics.analytics.trip_geo_publisher.flush_trip_geo_producer"
            ):
                pub.publish_from_haul_trips([trip], "Wed, 01 Jan 2020 22:50:30 GMT")

    assert len(captured) == 1
    assert captured[0]["type"] == "trip_route"
    assert captured[0]["geometry"]


def test_preview_geometry_for_queued_with_endpoints():
    sm = HaulTripStateMachine
    trip = {
        "_id": "haul002",
        "truck": "truck001",
        "order": "order001",
        "state": sm.queued_for_pickup.name,
        "current_loc": {"type": "Point", "coordinates": [103.85, 1.29]},
        "pickup_loc": {"type": "Point", "coordinates": [103.90, 1.31]},
        "dropoff_loc": {"type": "Point", "coordinates": [103.82, 1.35]},
        "routes": {"planned": {"repositioning_to_pickup": None, "loaded_to_dropoff": None}},
    }
    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    pub = ContainerTripGeoPublisher("run_cl")
    with patch(
        "apps.container_logistics.analytics.trip_geo_publisher.push_trip_geo_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.analytics.trip_geo_publisher.flush_trip_geo_producer"
        ):
            pub.publish_from_haul_trips([trip], "Wed, 15 Apr 2026 12:00:00 GMT")

    assert len(captured) == 2
    legs = {m["leg"] for m in captured if m.get("type") == "trip_route"}
    assert legs == {"repositioning_to_pickup", "loaded_to_dropoff"}
    assert all(m.get("geometry") for m in captured)


def test_collaboration_fields_surface_on_shared_hauls():
    enc = _sample_polyline()
    sm = HaulTripStateMachine
    base = {
        "truck": "truck001",
        "order": "order001",
        "state": sm.repositioning_to_pickup.name,
        "routes": {"planned": {"repositioning_to_pickup": {"geometry": enc, "duration": 100}}},
    }
    shared_trip = {
        **base,
        "_id": "haul_shared",
        "meta": {
            "truck_profile": {"haulier_id": "borax", "haulier_name": "Borax"},
            "collaboration": {
                "shared": True,
                "owner_haulier_id": "acme",
                "carrier_haulier_id": "borax",
                "benefit_km": 7.25,
            },
        },
    }
    own_trip = {
        **base,
        "_id": "haul_own",
        "meta": {"truck_profile": {"haulier_id": "acme", "haulier_name": "Acme"}},
    }
    captured = []

    def capture(_run_id, payload):
        captured.append(payload)

    pub = ContainerTripGeoPublisher("run_cl")
    with patch(
        "apps.container_logistics.analytics.trip_geo_publisher.push_trip_geo_to_topic",
        side_effect=capture,
    ):
        with patch(
            "apps.container_logistics.analytics.trip_geo_publisher.flush_trip_geo_producer"
        ):
            pub.publish_from_haul_trips([shared_trip, own_trip], "Wed, 15 Apr 2026 12:00:00 GMT")

    by_id = {m["haul_trip_id"]: m for m in captured}
    shared = by_id["haul_shared"]
    assert shared["shared"] is True
    assert shared["owner_haulier_id"] == "acme"
    assert shared["carrier_haulier_id"] == "borax"
    assert shared["benefit_km"] == 7.25
    assert shared["haulier_id"] == "borax"  # carrier stays the trip's haulier
    own = by_id["haul_own"]
    assert "shared" not in own and "owner_haulier_id" not in own
