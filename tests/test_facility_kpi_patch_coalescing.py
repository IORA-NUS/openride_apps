"""The facility REST KPI PATCH is coalesced to at most one per step.

Measured on a profiled 7-day `consortium_collab_7d` run: `facility.tick` spent
4407.9 s over 18,384 calls, of which 4314.0 s was `fac.http_patch` — 47,279
blocking HTTP PATCHes, 2.57 per tick, ~98% of the tick. The API answers in
1.4-3.5 ms; the rest is eventlet co-scheduling delay, so the fix is fewer
calls, not faster ones.

These tests pin the two halves of that fix:
  * the Kafka snapshot publish (live map, 0.15 ms) is UNCHANGED in count,
    order and force flags;
  * the PATCH happens once per `execute_step_actions`, carrying the LATEST
    stats, and once more on `close()` if anything is still pending.
"""

from __future__ import annotations

from datetime import datetime

from apps.container_logistics.facility.app import FacilityApp
from apps.container_logistics.statemachine import FacilityVisitType


class _FakePublisher:
    """Stands in for FacilitySnapshotPublisher.

    `kpi_stats()` is derived from the number of publishes so far, mirroring the
    real class, which recomputes from accumulated state on every call. That is
    what makes a coalesced last-write-wins PATCH lossless — and it lets a test
    tell the latest value apart from the first.
    """

    def __init__(self, publish_result: bool = True):
        self.publish_result = publish_result
        self.publish_calls: list[bool] = []  # force flag of each maybe_publish
        self.enqueues: list[tuple] = []
        self.kpi_stats_calls = 0

    def maybe_publish(self, manager, behavior, sim_clock_gmt, *, force=False):
        self.publish_calls.append(force)
        return self.publish_result

    def record_enqueue(self, truck_id, visit_type, sim_clock_gmt):
        self.enqueues.append((truck_id, visit_type, sim_clock_gmt))

    def kpi_stats(self):
        self.kpi_stats_calls += 1
        n = len(self.publish_calls)
        return {"avg_queue_wait_seconds": 10.0 * n, "peak_queue_length": n}


class _QueueController:
    def __init__(self):
        self.queue = []

    def active_truck_ids(self):
        return set()


class _FakeManager:
    def __init__(self):
        self.patches: list[dict] = []
        self.refresh_calls = 0
        self.enqueued: list[tuple] = []
        self.queue_controller = _QueueController()

    def refresh(self):
        self.refresh_calls += 1

    def enqueue_arrival(self, truck_id, *, visit_type):
        self.enqueued.append((truck_id, visit_type))

    def assign_available_gates(self):
        return {}

    def patch_kpi_stats(self, stats):
        self.patches.append(dict(stats))


class _StepDrivenFacilityApp(FacilityApp):
    """FacilityApp whose message drain replays scripted arrivals.

    In production `consume_messages()` routes an inbound FACILITY_WORKFLOW_EVENT
    through the interaction plugin, which lands on `enqueue_arrival`. Driving
    `enqueue_arrival` directly keeps the real publish/flush path under test
    without pulling in the plugin's routing table.
    """

    def consume_messages(self):
        arrivals, self._scripted_arrivals = self._scripted_arrivals, []
        for truck_id in arrivals:
            self.enqueue_arrival(truck_id, visit_type=FacilityVisitType.PICKUP)


def _make_app(publisher=None, arrivals=()):
    app = _StepDrivenFacilityApp.__new__(_StepDrivenFacilityApp)
    app.run_id = "run_test"
    app.behavior = {"profile": {"service_time": 0}}
    app.manager = _FakeManager()
    app.message_queue = []
    app.current_time = None
    app.current_time_str = None
    app.latest_sim_clock = None
    app._gate_service_ends = {}
    app._facility_stream = publisher if publisher is not None else _FakePublisher()
    app._facility_refresh_pending = True
    app._kpi_patch_pending = False
    app._scripted_arrivals = list(arrivals)
    return app


STEP_TIME = datetime(2020, 1, 1, 8, 0, 0)


def test_many_publishes_in_one_step_produce_exactly_one_patch():
    app = _make_app(arrivals=["truck_1", "truck_2", "truck_3"])

    app.execute_step_actions(STEP_TIME)

    # 3 arrivals + the end-of-workflow snapshot = 4 Kafka publishes ...
    assert len(app._facility_stream.publish_calls) == 4
    # ... but only ONE blocking REST PATCH.
    assert len(app.manager.patches) == 1


def test_the_single_patch_carries_the_latest_stats_not_the_first():
    app = _make_app(arrivals=["truck_1", "truck_2", "truck_3"])

    app.execute_step_actions(STEP_TIME)

    (patch,) = app.manager.patches
    # kpi_stats() is read at flush time, after all 4 publishes.
    assert patch == {"avg_queue_wait_seconds": 40.0, "peak_queue_length": 4}
    # The first publish would have produced 10.0 / 1.
    assert patch["peak_queue_length"] != 1


def test_kafka_publish_count_and_force_flags_are_unchanged():
    """Regression guard: coalescing must not touch the live-map stream."""
    app = _make_app(arrivals=["truck_1", "truck_2"])

    app.execute_step_actions(STEP_TIME)

    # enqueue_arrival forces; the workflow tail publish does not (no gates busy).
    assert app._facility_stream.publish_calls == [True, True, False]


def test_patch_repeats_per_step_not_per_publish():
    app = _make_app()

    for _ in range(3):
        app._scripted_arrivals = ["truck_a", "truck_b"]
        app.execute_step_actions(STEP_TIME)

    assert len(app._facility_stream.publish_calls) == 9  # 3 x (2 arrivals + tail)
    assert len(app.manager.patches) == 3  # one per step


def test_no_publish_in_a_step_means_no_patch():
    app = _make_app(publisher=_FakePublisher(publish_result=False))

    app.execute_step_actions(STEP_TIME)

    assert app._facility_stream.publish_calls == [False]
    assert app.manager.patches == []


def test_publish_between_ticks_flushes_on_the_next_step():
    """An MQTT arrival handled outside a tick must not lose its PATCH."""
    app = _make_app()
    app.current_time_str = "Wed, 01 Jan 2020 08:00:00 GMT"

    app.enqueue_arrival("truck_1", visit_type=FacilityVisitType.PICKUP)
    assert app.manager.patches == []  # deferred, not dropped
    assert app._kpi_patch_pending is True

    app._facility_stream.publish_result = False  # nothing new publishes this step
    app.execute_step_actions(STEP_TIME)

    assert len(app.manager.patches) == 1
    assert app._kpi_patch_pending is False


def test_close_flushes_a_pending_patch():
    app = _make_app()
    app.current_time_str = "Wed, 01 Jan 2020 08:00:00 GMT"

    app.enqueue_arrival("truck_1", visit_type=FacilityVisitType.PICKUP)
    assert app.manager.patches == []

    app._facility_stream.publish_result = False  # close's forced publish is a no-op
    app.close("Wed, 01 Jan 2020 09:00:00 GMT")

    assert len(app.manager.patches) == 1
    # 2 = the arrival's publish + close()'s own forced publish attempt; the flushed
    # value is read after both, i.e. it is the latest.
    assert app.manager.patches[0] == {"avg_queue_wait_seconds": 20.0, "peak_queue_length": 2}


def test_close_persists_the_final_forced_snapshot():
    app = _make_app()
    app.current_time_str = "Wed, 01 Jan 2020 08:00:00 GMT"

    app.close("Wed, 01 Jan 2020 09:00:00 GMT")

    # close() forces a publish, which arms the flag, which close() then flushes.
    assert app._facility_stream.publish_calls == [True]
    assert len(app.manager.patches) == 1
    assert app._kpi_patch_pending is False


def test_flush_is_idempotent():
    app = _make_app()
    app.current_time_str = "Wed, 01 Jan 2020 08:00:00 GMT"

    app.enqueue_arrival("truck_1", visit_type=FacilityVisitType.PICKUP)
    app._flush_kpi_stats()
    app._flush_kpi_stats()

    assert len(app.manager.patches) == 1
