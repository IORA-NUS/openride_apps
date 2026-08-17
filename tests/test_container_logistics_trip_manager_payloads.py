from datetime import datetime
from types import MethodType

from apps.container_logistics.statemachine import HaulTripStateMachine
from apps.container_logistics.truck.trip_manager import TruckTripManager


class _DummyUser:
    def get_headers(self, etag=None):
        return {}


class _DummyClient:
    def publish(self, channel, payload):
        return None


class _DummyMessenger:
    def __init__(self):
        self.client = _DummyClient()


def _capture_apply_calls(manager):
    calls = []

    def _fake_apply(self, transition, data, context=None):
        calls.append((transition, data, context))
        return {"transition": transition, "data": data, "context": context}

    manager.apply_trip_transition_and_notify = MethodType(_fake_apply, manager)
    return calls


def test_start_empty_reposition_includes_route_and_eta_fields():
    manager = TruckTripManager(
        run_id="r1",
        sim_clock="Mon, 01 Jan 2024 00:00:00 GMT",
        user=_DummyUser(),
        messenger=_DummyMessenger(),
        persona={"role": "truck"},
    )
    calls = _capture_apply_calls(manager)

    route = {"duration": 1200, "distance": 7000}
    manager.start_empty_reposition(
        "Mon, 01 Jan 2024 00:00:10 GMT",
        current_loc={"type": "Point", "coordinates": [103.8, 1.3]},
        route=route,
        estimated_time_to_pickup=1200,
    )

    assert len(calls) == 1
    _, data, context = calls[0]
    assert data["routes.planned.repositioning_to_pickup"] == route
    assert data["stats.estimated_time_to_pickup"] == 1200
    assert context["planned_route"] == route
    assert context["estimated_time_to_pickup"] == 1200


def test_estimate_next_event_time_repositioning_uses_eta_from_stats():
    now = datetime(2026, 1, 15, 10, 0, 0)
    manager = TruckTripManager(
        run_id="r1",
        sim_clock="Thu, 15 Jan 2026 10:00:00 GMT",
        user=_DummyUser(),
        messenger=_DummyMessenger(),
        persona={"role": "truck"},
    )
    manager.trip = {
        "_id": "trip-1",
        "state": HaulTripStateMachine.repositioning_to_pickup.name,
        "sim_clock": "Thu, 15 Jan 2026 10:00:00 GMT",
        "stats": {"estimated_time_to_pickup": 600},
        "routes": {"planned": {"repositioning_to_pickup": {"duration": 999}}},
    }

    assert manager.estimate_next_event_time(now) == datetime(2026, 1, 15, 10, 10, 0)


def test_estimate_next_event_time_transit_fallback_route_duration_only():
    now = datetime(2026, 1, 15, 8, 0, 0)
    manager = TruckTripManager(
        run_id="r1",
        sim_clock="Thu, 15 Jan 2026 08:00:00 GMT",
        user=_DummyUser(),
        messenger=_DummyMessenger(),
        persona={"role": "truck"},
    )
    manager.trip = {
        "_id": "trip-1",
        "state": HaulTripStateMachine.loaded_in_transit.name,
        "sim_clock": "Thu, 15 Jan 2026 08:00:00 GMT",
        "stats": {"estimated_time_to_dropoff": 0},
        "routes": {"planned": {"loaded_to_dropoff": {"duration": 1800}}},
    }

    assert manager.estimate_next_event_time(now) == datetime(2026, 1, 15, 8, 30, 0)


def test_estimate_next_event_time_pickup_gate_uses_service_time():
    now = datetime(2026, 6, 1, 12, 0, 0)
    manager = TruckTripManager(
        run_id="r1",
        sim_clock="Mon, 01 Jun 2026 12:00:00 GMT",
        user=_DummyUser(),
        messenger=_DummyMessenger(),
        persona={"role": "truck"},
    )
    manager.trip = {
        "_id": "trip-1",
        "state": HaulTripStateMachine.at_pickup_gate.name,
        "sim_clock": "Mon, 01 Jun 2026 12:00:00 GMT",
        "stats": {"pickup_service_time": 120},
    }

    assert manager.estimate_next_event_time(now) == datetime(2026, 6, 1, 12, 2, 0)


def test_estimate_next_event_time_queued_returns_clock():
    now = datetime(2026, 1, 1, 0, 0, 0)
    manager = TruckTripManager(
        run_id="r1",
        sim_clock="Thu, 01 Jan 2026 00:00:00 GMT",
        user=_DummyUser(),
        messenger=_DummyMessenger(),
        persona={"role": "truck"},
    )
    manager.trip = {
        "_id": "trip-1",
        "state": HaulTripStateMachine.queued_for_pickup.name,
        "sim_clock": "Wed, 31 Dec 2025 23:00:00 GMT",
        "stats": {},
    }

    assert manager.estimate_next_event_time(now) == now


def test_finish_pickup_service_includes_dropoff_route_eta_and_service_time():
    manager = TruckTripManager(
        run_id="r1",
        sim_clock="Mon, 01 Jan 2024 00:00:00 GMT",
        user=_DummyUser(),
        messenger=_DummyMessenger(),
        persona={"role": "truck"},
    )
    calls = _capture_apply_calls(manager)

    dropoff_route = {"duration": 2400, "distance": 14000}
    manager.finish_pickup_service(
        "Mon, 01 Jan 2024 00:02:00 GMT",
        current_loc={"type": "Point", "coordinates": [103.81, 1.31]},
        route_to_dropoff=dropoff_route,
        estimated_time_to_dropoff=2400,
        service_time=300,
    )

    assert len(calls) == 1
    _, data, context = calls[0]
    assert data["routes.planned.loaded_to_dropoff"] == dropoff_route
    assert data["stats.estimated_time_to_dropoff"] == 2400
    assert data["stats.pickup_service_time"] == 300
    assert context["planned_route"] == dropoff_route
    assert context["estimated_time_to_dropoff"] == 2400
    assert context["service_time"] == 300


# --- order-lifecycle service mode: shared-topic emitter (WP2) ------------------


def _trip_manager(order_events_topic=None):
    return TruckTripManager(
        run_id="r1",
        sim_clock="Mon, 01 Jan 2024 00:00:00 GMT",
        user=_DummyUser(),
        messenger=_DummyMessenger(),
        persona={"role": "truck"},
        order_events_topic=order_events_topic,
    )


def test_order_event_topic_defaults_to_the_per_order_topic():
    """Pins the ``agents``-mode path: unstamped trucks keep publishing to run_id/<order_id>."""
    manager = _trip_manager()
    manager.trip = {"_id": "trip-1", "order": "order-42", "sim_clock": "Mon, 01 Jan 2024 00:00:00 GMT"}

    assert manager._mqtt_topic_for_workflow_event("order_delivered") == "r1/order-42"


def test_order_event_topic_uses_the_shared_topic_when_stamped():
    manager = _trip_manager(order_events_topic="order_lifecycle")
    manager.trip = {"_id": "trip-1", "order": "order-42", "sim_clock": "Mon, 01 Jan 2024 00:00:00 GMT"}

    assert manager._mqtt_topic_for_workflow_event("order_delivered") == "r1/order_lifecycle"


def test_order_event_topic_is_none_without_an_order_in_both_modes():
    for topic in (None, "order_lifecycle"):
        manager = _trip_manager(order_events_topic=topic)
        manager.trip = {"_id": "trip-1", "order": None}
        assert manager._mqtt_topic_for_workflow_event("order_delivered") is None


def test_order_message_template_carries_event_order_id_and_trip_sim_clock():
    manager = _trip_manager(order_events_topic="order_lifecycle")
    manager.trip = {
        "_id": "trip-1",
        "truck": "truck-7",
        "order": "order-42",
        "sim_clock": "Mon, 01 Jan 2024 03:04:05 GMT",
    }

    msg = manager.message_template("order_delivered")

    assert msg["truck_id"] == "truck-7"
    assert msg["data"]["event"] == "order_delivered"
    assert msg["data"]["order_id"] == "order-42"
    assert msg["data"]["sim_clock"] == "Mon, 01 Jan 2024 03:04:05 GMT"


def test_facility_message_template_is_unchanged():
    manager = _trip_manager(order_events_topic="order_lifecycle")
    manager.trip = {"_id": "trip-1", "truck": "truck-7", "order": "order-42", "sim_clock": "x"}

    msg = manager.message_template("truck_arrived_pickup_queue")

    assert set(msg["data"]) == {"event"}
