"""OrderLifecycleApp / Manager / Agent: step semantics, op ordering and topic parity."""

from __future__ import annotations

from datetime import datetime

import pytest

from apps.container_logistics.order_lifecycle import (
    ORDER_LIFECYCLE_TOPIC_SUFFIX,
    OrderLifecycleManager,
    order_lifecycle_topic,
)
from apps.container_logistics.order_lifecycle.app import OrderLifecycleApp
from apps.container_logistics.order_lifecycle.batch_writer import ApplyStats
from apps.utils import time_to_str

RUN_ID = "run_test"
NOW = datetime(2026, 7, 29, 10, 0, 0)
NOW_STR = time_to_str(NOW)

BEHAVIOR = {
    "email": "order_lifecycle_main@test.com",
    "password": "password",
    "persona": {"role": "order_lifecycle", "domain": "container-logistics-sim"},
    "steps_per_action": 1,
    "response_rate": 1.0,
    "step_only_on_events": False,
    "profile": {"haulier_filter": None, "sweep_interval_steps": 30},
}


class RecordingWriter:
    """Stands in for OrderBatchWriter; records the op sequence."""

    def __init__(self):
        self.calls = []
        self.drained = []

    def apply_events(self, raw_payloads, fallback_sim_clock):
        self.calls.append("apply_events")
        self.drained.append(list(raw_payloads))
        return ApplyStats(events_seen=len(raw_payloads), parsed=len(raw_payloads))

    def publish_due(self, step, sim_clock):
        self.calls.append("publish_due")
        return 3

    def cancel_overdue_unassigned(self, step, max_wait_steps, open_ids, sim_clock):
        self.calls.append("cancel_overdue_unassigned")
        return 2


class StubManager(OrderLifecycleManager):
    def __init__(self):
        self.run_id = RUN_ID
        self.profile = {}
        self.persona = {}
        self.open_haul_calls = 0
        self.resource = {"_id": ORDER_LIFECYCLE_TOPIC_SUFFIX, "state": "online"}

    def order_ids_with_open_haul(self):
        self.open_haul_calls += 1
        return {"aaaaaaaaaaaaaaaaaaaaaaaa"}

    def login(self, sim_clock):
        return self.resource

    def logout(self, sim_clock):
        return self.resource


class StubApp(OrderLifecycleApp):
    """Real app, stubbed IO seams (no Eve login, no Mongo)."""

    def _create_user(self):
        return object()

    def _create_manager(self):
        return StubManager()


def make_app(profile=None):
    behavior = dict(BEHAVIOR)
    if profile is not None:
        behavior["profile"] = profile
    app = StubApp(run_id=RUN_ID, sim_clock=NOW_STR, behavior=behavior, messenger=None)
    app.writer = RecordingWriter()
    return app


# --------------------------------------------------------------------------- topics


def test_manager_id_is_the_shared_topic_suffix():
    manager = OrderLifecycleManager(RUN_ID, NOW_STR, user=object(), profile={}, persona={})
    assert manager.get_id() == ORDER_LIFECYCLE_TOPIC_SUFFIX == "order_lifecycle"


def test_app_subscribes_to_exactly_the_topic_the_truck_publishes_to():
    from apps.container_logistics.truck.trip_manager import TruckTripManager

    app = make_app()
    assert list(app.topic_params) == [order_lifecycle_topic(RUN_ID)]

    tm = TruckTripManager.__new__(TruckTripManager)
    tm.run_id = RUN_ID
    tm.trip = {"order": "0" * 24, "sim_clock": NOW_STR}
    tm._order_events_topic = ORDER_LIFECYCLE_TOPIC_SUFFIX
    assert tm._mqtt_topic_for_workflow_event("order_delivered") == order_lifecycle_topic(RUN_ID)
    assert tm._mqtt_topic_for_workflow_event("order_delivered") in app.topic_params


# --------------------------------------------------------------------------- run_step


def test_run_step_applies_events_then_publishes_then_sweeps():
    app = make_app()
    app.handle_app_topic_messages({"a": 1})
    app.handle_app_topic_messages({"b": 2})

    out = app.run_step(NOW_STR, time_step=30, max_wait_steps=360, horizon_steps=1000)

    assert app.writer.calls == ["apply_events", "publish_due", "cancel_overdue_unassigned"]
    assert app.writer.drained[0] == [{"a": 1}, {"b": 2}]
    assert app.message_queue == []  # fully drained
    assert out["published"] == 3 and out["swept"] == 2
    assert out["stats"].events_seen == 2


def test_sweep_is_gated_on_the_policy_and_the_cadence():
    app = make_app()
    # policy disabled
    app.run_step(NOW_STR, time_step=30, max_wait_steps=0, horizon_steps=1000)
    assert "cancel_overdue_unassigned" not in app.writer.calls
    # off-cadence step
    app.run_step(NOW_STR, time_step=31, max_wait_steps=360, horizon_steps=1000)
    assert "cancel_overdue_unassigned" not in app.writer.calls
    # on-cadence step
    app.run_step(NOW_STR, time_step=60, max_wait_steps=360, horizon_steps=1000)
    assert app.writer.calls.count("cancel_overdue_unassigned") == 1
    assert app.manager.open_haul_calls == 1  # exactly once, only on a due tick


def test_sweep_interval_comes_from_the_profile():
    app = make_app(profile={"haulier_filter": None, "sweep_interval_steps": 5})
    assert app.sweep_interval == 5
    app.run_step(NOW_STR, time_step=5, max_wait_steps=360, horizon_steps=1000)
    assert "cancel_overdue_unassigned" in app.writer.calls


@pytest.mark.parametrize("step", [1000, 1200])
def test_publishing_stops_at_the_horizon_but_events_still_apply(step):
    app = make_app()
    app.handle_app_topic_messages({"a": 1})
    out = app.run_step(NOW_STR, time_step=step, max_wait_steps=0, horizon_steps=1000)
    assert "publish_due" not in app.writer.calls
    assert app.writer.calls == ["apply_events"]
    assert out["published"] == 0


def test_no_horizon_configured_still_publishes():
    app = make_app()
    app.run_step(NOW_STR, time_step=99999, max_wait_steps=0, horizon_steps=0)
    assert "publish_due" in app.writer.calls


def test_close_performs_a_final_drain():
    app = make_app()
    app.handle_app_topic_messages({"late": True})
    app.close(NOW_STR)
    assert app.writer.drained[-1] == [{"late": True}]
    assert app.is_exited is True
