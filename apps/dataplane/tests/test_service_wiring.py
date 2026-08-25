"""DataplaneService wiring tests with fake hot/duck/archive. No Kafka, no Mongo, no DuckDB.

Feeding a message runs the production write path to completion on the calling thread — route
it, then flush the poll batch, which is exactly what ``commit_safe`` does before it may move
an offset — so a wiring gap between handler and store cannot hide behind a queue, a writer
thread or a double.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from apps.config import kafka_config
from apps.dataplane.ingest.consumer import DataplaneConsumer
from apps.dataplane.service import DataplaneService
# The real error, not a look-alike: ``capture_run`` now catches this type and nothing else,
# so a fake raising ``RuntimeError("RunNotResident: …")`` would prove the opposite of what
# the tests using it claim.
from apps.dataplane.store.hot import RunNotResident

BROKER = kafka_config["topics"]
CLOCK = "Mon, 01 Jun 2026 08:00:00 GMT"


class FakeMessage:
    def __init__(self, topic, key=b"run_1", value=None):
        self._topic, self._key = topic, key
        self._value = None if value is None else json.dumps(value).encode("utf-8")

    def topic(self):
        return self._topic

    def key(self):
        return self._key

    def value(self):
        return self._value

    def error(self):
        return None


class FakeFrame:
    def __init__(self, n=3, frame_idx=0):
        self.n = n
        self.frame_idx = frame_idx


class FakeHot:
    """Behaves like HotStore where it matters: slots/codes per run, snapshot bumps frame_idx."""

    def __init__(self):
        self.lock = threading.Lock()
        self.positions = []
        self.runs = []
        self.snapshots = []
        self.evicted = []
        self._frame_idx = {}
        self._codes = {}
        self._slots = {}

    def update_position(self, run_id, agent_id, lng, lat, state, haulier_id, sim_time_ms):
        with self.lock:
            self.positions.append((run_id, agent_id, lng, lat, state, haulier_id, sim_time_ms))
            if run_id not in self.runs:
                self.runs.append(run_id)
            slots = self._slots.setdefault(run_id, {})
            slot = slots.setdefault(agent_id, len(slots))
            codes = self._codes.setdefault(run_id, {})
            if haulier_id and haulier_id not in codes:
                codes[haulier_id] = len(codes) + 1
        return slot

    def resident_runs(self):
        with self.lock:
            return list(self.runs)

    def snapshot(self, run_id, kind=0):
        with self.lock:
            if run_id not in self.runs:
                raise RunNotResident(run_id)
            idx = self._frame_idx.get(run_id, 0)
            self._frame_idx[run_id] = idx + 1
            self.snapshots.append(run_id)
            return FakeFrame(frame_idx=idx)

    def evict(self, run_id):
        with self.lock:
            if run_id in self.runs:
                self.runs.remove(run_id)
                self.evicted.append(run_id)
                return True
            return False

    def haulier_codes(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise RunNotResident(run_id)
            return dict(self._codes.get(run_id, {}))

    def slot_map(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise RunNotResident(run_id)
            return dict(self._slots.get(run_id, {}))

    def stats(self):
        with self.lock:
            return {"runs": len(self.runs), "trucks": len(self.positions), "frames": 1, "updates": 1}


class FakeDuck:
    db_path = "/tmp/fake.duckdb"

    def __init__(self):
        self.lock = threading.Lock()
        self.kpi_rows = []
        self.breakdowns = []
        self.frames = []
        self.meta = {}
        self.export_state = {}
        self.checkpoints = 0
        self.closed = False

    def write_kpi_events(self, rows):
        with self.lock:
            self.kpi_rows.extend(rows)
        return len(rows)

    def write_breakdown(self, run_id, scope, sim_clock, final, entities):
        with self.lock:
            self.breakdowns.append((run_id, scope, sim_clock, final, entities))
        return len(entities)

    def write_breakdown_batch(self, run_id, snapshots):
        written = 0
        for scope, sim_clock, final, entities in snapshots:
            written += self.write_breakdown(run_id, scope, sim_clock, final, entities)
        return written

    def write_frame(self, run_id, frame):
        with self.lock:
            self.frames.append((run_id, frame))
        return frame.n

    def upsert_run_meta(self, run_id, **fields):
        with self.lock:
            self.meta.setdefault(run_id, {}).update(fields)

    def get_run_meta(self, run_id):
        with self.lock:
            meta = self.meta.get(run_id)
            return dict(meta) if meta is not None else None

    def set_export_state(self, run_id, *, status, row_count=None, frame_count=None, error=None):
        with self.lock:
            self.export_state[run_id] = {
                "status": status,
                "row_count": row_count,
                "frame_count": frame_count,
                "error": error,
            }

    def get_export_state(self, run_id):
        with self.lock:
            state = self.export_state.get(run_id)
            return dict(state) if state is not None else None

    def frame_range(self, run_id):
        with self.lock:
            idx = [f.frame_idx for r, f in self.frames if r == run_id]
            return (min(idx), max(idx)) if idx else None

    def list_run_ids(self):
        with self.lock:
            return sorted(self.meta)

    def open_run_ids(self):
        with self.lock:
            return sorted(r for r, m in self.meta.items() if not m.get("status"))

    def checkpoint(self):
        self.checkpoints += 1

    def close(self):
        self.closed = True


class FakeRunsCollection:
    def __init__(self):
        self.updates = []

    def update_one(self, flt, update, upsert=False):
        self.updates.append((flt, update, upsert))


class FakeArchive:
    def __init__(self, fail=False):
        self.complete_flags = []
        self.fail = fail
        self.dumps = []
        self.reconciles = 0
        self.closed = False
        self.runs = FakeRunsCollection()
        self.block = None  # set to a threading.Event to simulate a hung mongod

    def ping(self):
        return not self.fail

    def ensure_indexes(self):
        pass

    def dump_run(self, store, run_id, reason="", complete=None):
        # `complete` mirrors MongoArchive.dump_run: the service passes complete=False when
        # ingest is blocked, so a run with stranded rows is not certified closed. A double
        # missing it turns a real contract change into a TypeError instead of a test.
        self.complete_flags.append(complete)
        if self.block is not None:
            self.block.wait(10.0)
        if self.fail:
            raise RuntimeError("mongo down")
        self.dumps.append((run_id, reason))
        return {"frames": 0, "kpi": 0}

    def reconcile(self, store, run_ids=None, reason="sweep"):
        self.reconciles += 1
        if self.fail:
            raise RuntimeError("mongo down")
        return {"checked": 0, "dumped": 0, "run_ids": [], "errors": {}}

    def archived_run_ids(self):
        return {run_id for run_id, _ in self.dumps}

    def run_is_pending(self, store, run_id, summary=None):
        return run_id not in {run_id_ for run_id_, _ in self.dumps}

    def close(self):
        self.closed = True


@pytest.fixture
def svc():
    service = DataplaneService(
        hot=FakeHot(),
        duck=FakeDuck(),
        archive=FakeArchive(),
        enable_http=False,
        reconcile_interval_s=0.05,
        frame_interval_s=0.02,
    )
    yield service
    service.stop(timeout=1.0)


def feed(service, msg):
    """One message through the production path: route it, then flush the poll batch.

    ``DataplaneConsumer.commit_safe`` is the only thing that flushes in production and it is
    the only thing that can move an offset, so "handled then flushed" is exactly the state in
    which a record's offset becomes committable. Feeding without the flush would test a state
    the process never commits from. The result is therefore ``handled AND made durable`` — the
    same predicate the offset gate uses.
    """
    ok = service.consumer.handle_message(msg)
    return service.consumer.flush_writes() and ok


class TestHandlerTable:
    def test_handler_table_covers_the_six_logical_topics(self, svc):
        assert set(svc.handlers) == set(DataplaneConsumer.LOGICAL_TOPICS)
        assert set(svc.consumer.topic_map().values()) == set(DataplaneConsumer.LOGICAL_TOPICS)

    def test_kpi_messages_are_written_by_the_handler_that_received_them(self, svc):
        """The handler writes, then returns; there is nothing between it and the store."""
        for i in range(3):
            assert feed(svc, FakeMessage(BROKER["kpi"], value={"metric": f"m{i}", "value": i, "sim_clock": CLOCK}))
        rows = svc._duck.kpi_rows
        assert len(rows) == 3
        assert rows[0][0] == "run_1" and rows[0][1] == "m0" and rows[0][2] == 0.0
        assert svc.counters["kpi"] == 3

    def test_kpi_without_metric_is_ignored(self, svc):
        feed(svc, FakeMessage(BROKER["kpi"], value={"value": 1, "sim_clock": CLOCK}))
        assert svc._duck.kpi_rows == []
        assert svc.counters["kpi_malformed"] == 1

    def test_every_scope_the_producer_emits_is_either_stored_or_named(self, svc):
        """``planner`` was falling into the same silent hole as ``lane``, unmentioned.

        ``kpi_breakdown_persist.persist_kpi_breakdown`` emits four scopes: truck, haulier,
        lane and planner. ``on_kpi_breakdown`` returned early for anything outside
        (truck, haulier), counting only ``kpi_breakdown_skipped`` — a counter no endpoint
        reads — and NOT calling ``_discard``, so the record's offset was committed and it was
        gone from Kafka too. Planner rows are the per-collaboration measurement unit, i.e. the
        project's primary research lens (CLAUDE.md §1), and the code comment justified only
        ``lane``. Now: the stored set is stored, the ignored set is named, and anything else
        is a discard the verdict can see.
        """
        for scope in ("truck", "haulier", "planner", "lane", "wormhole"):
            feed(
                svc,
                FakeMessage(
                    BROKER["kpi_breakdown"],
                    value={
                        "scope": scope,
                        "sim_clock": CLOCK,
                        "final": False,
                        "breakdown": {"entities": [{"id": "t1", "empty_km": 1.0}]},
                    },
                ),
            )
        scopes = [b[1] for b in svc._duck.breakdowns]
        # "lane" joined the stored set when the /lanes read endpoint was added: an endpoint
        # cannot serve a scope ingest throws away.
        assert scopes == ["truck", "haulier", "planner", "lane"]
        assert svc.counters.get("kpi_breakdown_skipped", 0) == 0
        assert svc.counters["kpi_breakdown_unknown_scope"] == 1    # wormhole: audited
        assert svc.consumer.stats()["discards"] == 1
        assert "ingest_discarding" in svc.health_payload()["degraded"]

    def test_an_empty_entities_list_is_legitimate_not_malformed(self, svc):
        """``save_breakdowns()`` publishes exactly this, twice, at the start of every run.

        ``apps/container_logistics/analytics/manager.py`` calls ``persist_kpi_breakdown`` for
        the truck and haulier scopes unconditionally, and that function has no empty-rows
        guard — so ``breakdown.entities == []`` is what the producer emits before any truck
        has completed a trip. Counting it as a discard put ``ingest_discarding`` in
        ``degraded`` and 503'd /health for the first five minutes of every simulation.
        """
        assert feed(
            svc,
            FakeMessage(
                BROKER["kpi_breakdown"],
                value={"scope": "truck", "sim_clock": CLOCK, "final": False,
                       "breakdown": {"entities": []}},
            ),
        ) is True
        assert svc.counters["kpi_breakdown_malformed"] == 0
        assert svc.consumer.stats()["discards"] == 0
        assert svc._duck.breakdowns == [("run_1", "truck", pytest.approx(
            svc._duck.breakdowns[0][2]), False, [])]
        assert svc.health_payload()["ok"] is True

    def test_a_breakdown_of_only_poison_entities_is_dropped_and_counted(self, svc):
        """Filtering is not the same as accepting: the elements that vanished are named."""
        assert feed(
            svc,
            FakeMessage(
                BROKER["kpi_breakdown"],
                value={"scope": "truck", "sim_clock": CLOCK,
                       "breakdown": {"entities": ["truck_7", None]}},
            ),
        ) is True
        assert svc.counters["kpi_breakdown_entities_dropped"] == 1
        assert svc.consumer.stats()["discards"] == 1

    @pytest.mark.parametrize(
        "logical,value,counter",
        [
            # The breakdown ENVELOPE, not its entities: ``(payload["breakdown"] or {}).get``
            # raised AttributeError on a list or a string.
            ("kpi_breakdown",
             {"scope": "truck", "sim_clock": CLOCK, "breakdown": [{"entities": []}]},
             "kpi_breakdown_malformed"),
            ("kpi_breakdown",
             {"scope": "truck", "sim_clock": CLOCK, "breakdown": "none"},
             "kpi_breakdown_malformed"),
            # ``haulier_id`` is a DICT KEY in the hot tier's code book: an object raised
            # TypeError: unhashable type straight out of the handler.
            ("trip_geo",
             {"type": "truck_loc", "truck_agent_id": "t1", "lon": 4.0, "lat": 51.0,
              "haul_state": "loaded", "haulier_id": {"id": "H1"}, "sim_clock": CLOCK},
             "truck_loc_malformed"),
        ],
    )
    def test_a_payload_the_store_cannot_represent_is_a_discard_never_an_exception(
        self, svc, logical, value, counter
    ):
        """The poison class must be unrepresentable, not merely rarer.

        A handler that raises does not advance its record's offset, so the broker replays the
        SAME bytes after every restart and the partition is wedged for ever — measured across
        a simulated restart: poison at kpi_breakdown_stream offset 5 of 12, pass 1 commits 5
        with handler_errors=1, the restart from the committed offset commits 5 again. Raising
        is reserved for "the store is down", which a redelivery can fix; a payload shape no
        redelivery can fix is a counted discard, which the ``ingest_discarding`` rate rule
        judges. These two shapes were the last of the class still raising.
        """
        # True = the handler returned, so the offset is committable and the bytes are gone.
        assert feed(svc, FakeMessage(BROKER[logical], value=value)) is True
        assert svc.counters[counter] == 1
        assert svc.consumer.stats()["handler_errors"] == 0   # nothing reached the offset gate
        assert svc.consumer.stats()["discards"] == 1

    def test_truck_loc_updates_the_hot_tier_with_lon_mapped_to_lng(self, svc):
        feed(
            svc,
            FakeMessage(
                BROKER["trip_geo"],
                value={
                    "type": "truck_loc",
                    "sim_clock": CLOCK,
                    "truck_agent_id": "truck_7",
                    "lon": 4.5,
                    "lat": 51.9,
                    "haul_state": "loaded_in_transit",
                    "haulier_id": "h1",
                },
            ),
        )
        assert len(svc._hot.positions) == 1
        run_id, agent, lng, lat, state, haulier, ms = svc._hot.positions[0]
        assert (run_id, agent, lng, lat) == ("run_1", "truck_7", 4.5, 51.9)
        assert state == "loaded_in_transit"
        assert haulier == "h1"
        assert ms == pytest.approx(1780300800000.0)  # 2026-06-01T08:00:00Z

    def test_trip_route_and_trip_end_are_counted_not_stored(self, svc):
        feed(svc, FakeMessage(BROKER["trip_geo"], value={"type": "trip_route", "trip_id": "x"}))
        feed(svc, FakeMessage(BROKER["trip_geo"], value={"type": "trip_end", "trip_id": "x"}))
        assert svc.counters["trip_route"] == 1
        assert svc.counters["trip_end"] == 1
        assert svc._hot.positions == []

    def test_facility_and_perf_are_counted_only(self, svc):
        feed(svc, FakeMessage(BROKER["facility_stream"], value={"type": "facility_snapshot", "facility_id": "f1"}))
        feed(svc, FakeMessage(BROKER["perf"], value={"metrics": {"steps": 1}}))
        assert svc.counters["facility_stream"] == 1
        assert svc.counters["perf"] == 1

    def test_terminal_run_status_marks_meta_and_queues_the_dump(self, svc):
        feed(svc, FakeMessage(BROKER["kpi"], value={"metric": "m", "value": 1, "sim_clock": CLOCK}))
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "COMPLETED", "run_id": "run_1", "simulation_active": False},
            ),
        )
        # The kpi row was written before the terminal arrived — order is kept by the topic.
        assert len(svc._duck.kpi_rows) == 1
        assert svc._duck.meta["run_1"]["status"] == "completed"
        assert svc.counters["run_terminal"] == 1
        # The dump is queued for the archive task, never run on the poll thread.
        assert svc._archive.dumps == []
        assert svc.pending_dumps() == ["run_1"]
        assert svc.drain_dumps() == 1
        assert svc._archive.dumps == [("run_1", "completed")]
        assert svc.pending_dumps() == []

    def test_running_status_does_not_dump(self, svc):
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "RUNNING", "run_id": "run_1", "simulation_active": True},
            ),
        )
        assert svc._archive.dumps == []
        assert svc.counters["run_status"] == 1

    def test_non_simulation_lifecycle_scope_is_ignored(self, svc):
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "COMPLETED", "simulation_active": False, "lifecycle_scope": "service"},
            ),
        )
        assert svc._archive.dumps == []


class TestFailureIsolation:
    def test_archive_failure_is_logged_not_raised(self, svc):
        svc._archive.fail = True
        assert feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "FAILED", "simulation_active": False},
            ),
        )
        assert svc.consumer.stats()["handler_errors"] == 0
        svc.drain_dumps()
        assert svc.health_payload()["archive"]["available"] is False

    def test_a_duck_write_failure_reaches_the_consumer_and_holds_the_offset(self, svc):
        """A failed write must not be swallowed: that is what leaves the offset uncommitted."""
        def boom(rows):
            raise RuntimeError("TransactionException")

        svc._duck.write_kpi_events = boom
        assert feed(svc, FakeMessage(BROKER["kpi"], value={"metric": "m", "value": 1, "sim_clock": CLOCK})) is False
        assert svc.consumer.stats()["handler_errors"] == 1
        assert svc._duck.kpi_rows == []

    def test_missing_archive_degrades_instead_of_failing(self):
        service = DataplaneService(
            hot=FakeHot(),
            duck=FakeDuck(),
            archive=None,
            archive_factory=lambda: (_ for _ in ()).throw(RuntimeError("mongo down")),
            enable_http=False,
        )
        assert service.dump_run("run_x", reason="completed") is False
        payload = service.health_payload()
        assert payload["archive"]["available"] is False
        assert payload["ok"] is True  # nothing pending and no tasks -> still healthy
        service.stop(timeout=1.0)


class TestArchiveProbeIsNotALatch:
    """Regression: one failed probe used to disable archiving for the life of the process.

    `_archive_tried` was set *before* the probe and never reset, so a mongod that was down for
    the 30 s around startup meant every completed run silently skipped the durable record —
    with /health still answering 200.
    """

    def _flaky_factory(self, calls, fail_times):
        def factory():
            calls.append(1)
            if len(calls) <= fail_times:
                raise RuntimeError("connection refused")
            return FakeArchive()

        return factory

    def test_probe_is_retried_and_recovers(self):
        calls = []
        service = DataplaneService(
            hot=FakeHot(),
            duck=FakeDuck(),
            archive=None,
            archive_factory=self._flaky_factory(calls, fail_times=2),
            enable_http=False,
        )
        try:
            assert service.archive is None          # probe 1 fails
            service._archive_probe_at = 0.0         # backoff elapses
            assert service.archive is None          # probe 2 fails
            service._archive_probe_at = 0.0
            assert service.archive is not None      # probe 3 succeeds — no permanent latch
            assert len(calls) == 3
            assert service.health_payload()["archive"]["available"] is True
        finally:
            service.stop(timeout=1.0)

    def test_probe_backs_off_instead_of_hammering_mongo(self):
        calls = []
        service = DataplaneService(
            hot=FakeHot(),
            duck=FakeDuck(),
            archive=None,
            archive_factory=self._flaky_factory(calls, fail_times=99),
            enable_http=False,
        )
        try:
            for _ in range(20):
                assert service.archive is None
            assert len(calls) == 1  # inside the backoff window: probed once
            assert service._archive_probe_at > time.monotonic()
        finally:
            service.stop(timeout=1.0)

    def test_queued_runs_plus_a_dead_archive_fail_the_health_verdict(self):
        service = DataplaneService(
            hot=FakeHot(),
            duck=FakeDuck(),
            archive=None,
            archive_factory=self._flaky_factory([], fail_times=99),
            enable_http=False,
        )
        try:
            assert service.health_payload()["ok"] is True
            service.queue_dump("run_1", "completed")
            service.drain_dumps()
            payload = service.health_payload()
            assert payload["archive"]["available"] is False
            assert payload["archive"]["pending_run_ids"] == ["run_1"]
            assert payload["ok"] is False  # <- the silent-archive-loss alarm
            assert "archive_unavailable_with_pending_runs" in payload["degraded"]
        finally:
            service.stop(timeout=1.0)

    def test_a_queued_dump_survives_an_outage_and_lands_when_mongo_returns(self):
        archive = FakeArchive(fail=True)
        service = DataplaneService(
            hot=FakeHot(), duck=FakeDuck(), archive=archive, enable_http=False
        )
        try:
            service.queue_dump("run_1", "completed")
            assert service.drain_dumps() == 0
            assert service.pending_dumps() == ["run_1"]  # a miss costs delay, never data
            archive.fail = False
            assert service.drain_dumps() == 1
            assert archive.dumps == [("run_1", "completed")]
        finally:
            service.stop(timeout=1.0)

    def test_mongo_client_kwargs_carry_timeouts(self):
        service = DataplaneService(hot=FakeHot(), duck=FakeDuck(), archive=FakeArchive(), enable_http=False)
        try:
            kwargs = service.mongo_client_kwargs()
            assert kwargs["serverSelectionTimeoutMS"] > 0
            assert kwargs["connectTimeoutMS"] > 0
            assert kwargs["socketTimeoutMS"] > 0
            assert "host" in kwargs
        finally:
            service.stop(timeout=1.0)


class TestIngestIsNeverBlockedByTheArchive:
    """Regression: dump_run ran inline on the poll thread; a hung mongod stopped all ingest."""

    def test_a_hung_mongo_does_not_stall_the_consumer_thread(self, svc):
        svc._archive.block = threading.Event()
        blocked = threading.Thread(target=svc.drain_dumps, daemon=True)

        started = time.monotonic()
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "COMPLETED", "simulation_active": False},
            ),
        )
        handled_in = time.monotonic() - started
        assert handled_in < 0.5, "terminal run_status blocked the poll thread"

        svc.queue_dump("run_1", "completed")
        blocked.start()
        time.sleep(0.1)  # the archive worker is now wedged inside dump_run

        # Ingest keeps going on every topic while the archive thread is stuck.
        for i in range(20):
            assert feed(
                svc, FakeMessage(BROKER["kpi"], value={"metric": f"m{i}", "value": i, "sim_clock": CLOCK})
            )
        assert feed(svc, FakeMessage(BROKER["perf"], value={"metrics": {}}))
        assert svc.counters["kpi"] == 20
        svc._archive.block.set()
        blocked.join(timeout=5)
        assert not blocked.is_alive()


def _truck_loc(run_id=b"run_1", agent="truck_7", lon=4.5, lat=51.9, haulier="h1"):
    return FakeMessage(
        BROKER["trip_geo"],
        key=run_id,
        value={
            "type": "truck_loc",
            "sim_clock": CLOCK,
            "truck_agent_id": agent,
            "lon": lon,
            "lat": lat,
            "haul_state": "loaded_in_transit",
            "haulier_id": haulier,
        },
    )


class TestNoGhostFrames:
    """Regression: a finished (or idle) run kept minting an identical frame every second.

    At 500 trucks / 1 Hz that is 500 rows/s — ~43M rows a day — of frozen positions, appended
    *after* the archive dump, so DuckDB and Mongo diverged permanently and any replay showed
    the run stuck at its last position forever.
    """

    def test_an_idle_run_is_not_snapshotted_twice(self, svc):
        feed(svc, _truck_loc())
        assert svc.capture_frames() == 1
        assert svc.capture_frames() == 0          # nothing moved
        assert svc.capture_frames() == 0
        assert len(svc._duck.frames) == 1
        assert svc.counters["frames_skipped_idle"] >= 2

    def test_a_run_that_moves_again_is_captured_again(self, svc):
        feed(svc, _truck_loc())
        assert svc.capture_frames() == 1
        feed(svc, _truck_loc(lon=4.6))
        assert svc.capture_frames() == 1
        assert len(svc._duck.frames) == 2

    def test_a_finished_run_is_evicted_and_produces_no_further_frames(self, svc):
        feed(svc, _truck_loc())
        svc.capture_frames()
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "COMPLETED", "simulation_active": False},
            ),
        )
        frames_at_terminal = len(svc._duck.frames)
        assert frames_at_terminal >= 1
        assert svc._hot.evicted == ["run_1"]
        assert svc._hot.resident_runs() == []

        for _ in range(20):                       # 20 more sweeps, i.e. 20 s at 1 Hz
            svc.capture_frames()
        assert len(svc._duck.frames) == frames_at_terminal

    def test_the_final_frame_is_captured_before_eviction(self, svc):
        feed(svc, _truck_loc())
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "COMPLETED", "simulation_active": False},
            ),
        )
        assert [r for r, _ in svc._duck.frames] == ["run_1"]


class TestCodeBooksArePersisted:
    """Regression: haulier codes and slot maps existed only in RAM.

    `haulier` is a uint8 and `slot` a uint32 assigned in first-sighting order. Without the two
    maps on disk, every archived frame is integers with no dictionary — no colouring by
    haulier, no join to a truck, i.e. the project's primary lens is unrecoverable.
    """

    def test_capture_writes_the_code_books_into_run_meta(self, svc):
        feed(svc, _truck_loc(agent="truck_7", haulier="h1"))
        feed(svc, _truck_loc(agent="truck_8", haulier="h2"))
        svc.capture_frames()
        meta = svc._duck.meta["run_1"]
        assert json.loads(meta["slot_map"]) == {"truck_7": 0, "truck_8": 1}
        assert json.loads(meta["haulier_codes"]) == {"h1": 1, "h2": 2}
        assert meta["n_trucks"] == 2
        assert meta["source"] == "live"
        assert meta["first_seen"] is not None and meta["last_seen"] is not None
        assert meta["max_frame_idx"] == 0

    def test_terminal_status_persists_the_maps_before_evicting(self, svc):
        feed(svc, _truck_loc(agent="truck_7", haulier="h1"))
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "COMPLETED", "simulation_active": False},
            ),
        )
        meta = svc._duck.meta["run_1"]
        assert meta["status"] == "completed"
        assert json.loads(meta["haulier_codes"]) == {"h1": 1}
        assert json.loads(meta["slot_map"]) == {"truck_7": 0}

    def test_meta_is_not_rewritten_when_nothing_changed(self, svc):
        feed(svc, _truck_loc())
        svc.capture_frames()
        writes = svc.counters["meta_writes"]
        feed(svc, _truck_loc(lon=4.7))   # same truck, same haulier
        svc.capture_frames()
        assert svc.counters["meta_writes"] == writes

    def test_codes_are_restored_from_run_meta_after_a_restart(self):
        """A restart mid-run must not make the same uint8 mean a different haulier.

        Deliberately run against the REAL HotStore: the previous version of this test used a
        fake that implemented ``seed_haulier_codes``, a method the real class did not have, so
        it passed while the wiring was completely dead.
        """
        from apps.dataplane.store.hot import HotStore

        duck = FakeDuck()
        duck.meta["run_1"] = {
            "haulier_codes": json.dumps({"h_alpha": 1, "h_beta": 2}),
            "slot_map": json.dumps({"truck_1": 0}),
            "first_seen": "2026-08-05T10:00:00",
            "max_frame_idx": 41,
        }
        service = DataplaneService(
            hot=HotStore(), duck=duck, archive=FakeArchive(), enable_http=False
        )
        try:
            feed(service, _truck_loc(agent="truck_9", haulier="h_beta"))
            # The pre-restart meaning of every code survives, and a haulier seen in a
            # different order after the restart keeps its old code.
            assert service._hot.haulier_codes("run_1") == {"h_alpha": 1, "h_beta": 2}
            # first_seen and max_frame_idx are carried forward, not reset to "now"/0.
            assert service._run_first_seen["run_1"] == "2026-08-05T10:00:00"
            assert service._run_max_frame_idx["run_1"] == 41
            # The frame counter resumes past the archived frames instead of at 0.
            assert service._hot.next_frame_idx("run_1") == 42
            # Slots are restored too; truck_1 keeps slot 0 and the newcomer gets a fresh one.
            assert service._hot.slot_map("run_1") == {"truck_1": 0, "truck_9": 1}
            # …but a re-seeded truck that has not reported is NOT emitted at (0, 0).
            frame = service._hot.snapshot("run_1")
            assert frame.n == 1
            assert list(frame.slot) == [1] and list(frame.lng) == [4.5]
        finally:
            service.stop(timeout=1.0)

    def test_restore_is_retried_until_a_lookup_actually_happens(self):
        """The one-shot latch must not be consumed by a call that looked nothing up."""

        class BlindDuck(FakeDuck):
            def __init__(self):
                super().__init__()
                self.calls = 0
                self.explode = True

            def get_run_meta(self, run_id):
                self.calls += 1
                if self.explode:
                    raise RuntimeError("duckdb not ready")
                return super().get_run_meta(run_id)

        from apps.dataplane.store.hot import HotStore

        duck = BlindDuck()
        duck.meta["run_1"] = {"haulier_codes": json.dumps({"h_alpha": 1, "h_beta": 2})}
        service = DataplaneService(
            hot=HotStore(), duck=duck, archive=FakeArchive(), enable_http=False
        )
        try:
            assert service.restore_run_codes("run_1") is False
            assert "run_1" not in service._run_restored  # not latched — nothing was read
            duck.explode = False
            assert service.restore_run_codes("run_1") is True
            assert service._hot.haulier_codes("run_1") == {"h_alpha": 1, "h_beta": 2}
            assert duck.calls == 2
            assert service.restore_run_codes("run_1") is False  # now latched
            assert duck.calls == 2
        finally:
            service.stop(timeout=1.0)

    def test_persist_run_meta_merges_and_never_blanks_the_book(self, svc):
        """A status-only write must not stamp {} over the only surviving dictionary."""
        svc._duck.meta["run_1"] = {
            "haulier_codes": json.dumps({"h_alpha": 1}),
            "slot_map": json.dumps({"truck_1": 0}),
        }
        # A run with no hot tier residency at all (headless): the maps survive untouched.
        assert svc.persist_run_meta("run_1", status="completed", force=True) is True
        meta = svc._duck.meta["run_1"]
        assert meta["status"] == "completed"
        assert json.loads(meta["haulier_codes"]) == {"h_alpha": 1}
        assert json.loads(meta["slot_map"]) == {"truck_1": 0}

    def test_persist_run_meta_keeps_the_persisted_value_on_a_conflict(self, svc):
        svc._duck.meta["run_1"] = {"haulier_codes": json.dumps({"h1": 7, "h_old": 2})}
        feed(svc, _truck_loc(agent="truck_7", haulier="h1"))   # hot tier mints h1 -> 1
        assert svc.persist_run_meta("run_1", force=True) is True
        merged = json.loads(svc._duck.meta["run_1"]["haulier_codes"])
        assert merged == {"h1": 7, "h_old": 2}     # persisted wins, nothing is lost
        assert svc.counters["code_book_conflicts"] == 1

    def test_an_unseedable_hot_tier_is_counted_and_warned_about_not_ignored(self, svc):
        svc._duck.meta["run_1"] = {"haulier_codes": json.dumps({"h1": 3})}
        feed(svc, _truck_loc())
        assert svc.counters["haulier_code_reseed_unavailable"] == 1

    def test_the_service_does_not_patch_the_mongo_run_summary_itself(self, svc):
        """Summary ownership belongs to MongoArchive.dump_run, and to nobody else.

        The service used to bolt the code books on afterwards from ONE of the two call
        sites, so the reconcile sweep — the very path that exists for missed triggers —
        wrote a summary without them, and its next replace_one wiped the ones the terminal
        dump had added.
        """
        feed(svc, _truck_loc())
        feed(
            svc,
            FakeMessage(
                BROKER["run_status"],
                value={"status": "COMPLETED", "simulation_active": False},
            ),
        )
        svc.drain_dumps()
        assert svc._archive.dumps == [("run_1", "completed")]
        assert svc._archive.runs.updates == []
        assert not hasattr(svc, "_augment_run_summary")
        # The code books the archive reads out of run_meta are there for it to find.
        assert json.loads(svc._duck.meta["run_1"]["haulier_codes"]) == {"h1": 1}
        assert json.loads(svc._duck.meta["run_1"]["slot_map"]) == {"truck_7": 0}

    def test_a_successful_dump_records_the_export_state(self, svc):
        svc.queue_dump("run_1", "completed")
        assert svc.drain_dumps() == 1
        state = svc._duck.get_export_state("run_1")
        assert state is not None and state["status"] == "exported"

    def test_a_failed_dump_records_the_error_in_the_export_state(self, svc):
        svc._archive.fail = True
        svc.queue_dump("run_1", "completed")
        assert svc.drain_dumps() == 0
        state = svc._duck.get_export_state("run_1")
        assert state is not None and state["status"] == "error"
        assert "mongo down" in state["error"]


class TestTheFakesCannotDriftFromTheRealClasses:
    """The exact defect that let a broken restart-restore ship with a green suite.

    ``test_codes_are_restored_from_run_meta_after_a_restart`` passed because a fake in this
    file implemented ``seed_haulier_codes`` while the real ``HotStore`` had no such method —
    the production call site was dead and no test could see it. A fake may implement FEWER
    methods than the real class; it may never implement one the real class lacks.
    """

    @staticmethod
    def _public_methods(cls):
        return {
            name
            for name in vars(cls)
            if not name.startswith("_") and callable(getattr(cls, name, None))
        }

    def test_every_fake_hot_method_exists_on_the_real_hot_store(self):
        from apps.dataplane.store.hot import HotStore

        missing = sorted(self._public_methods(FakeHot) - set(dir(HotStore)))
        assert missing == [], f"FakeHot invents methods HotStore does not have: {missing}"

    def test_every_fake_duck_method_exists_on_the_real_duck_store(self):
        from apps.dataplane.store.duck import DuckStore

        missing = sorted(self._public_methods(FakeDuck) - set(dir(DuckStore)))
        assert missing == [], f"FakeDuck invents methods DuckStore does not have: {missing}"

    def test_every_fake_archive_method_exists_on_the_real_archive(self):
        from apps.dataplane.archive.mongo import MongoArchive

        missing = sorted(self._public_methods(FakeArchive) - set(dir(MongoArchive)))
        assert missing == [], f"FakeArchive invents methods MongoArchive does not have: {missing}"

    def test_the_seams_the_service_probes_for_exist_on_the_real_classes(self):
        """Every getattr-probed method the service depends on, asserted to be real."""
        from apps.dataplane.store.duck import DuckStore
        from apps.dataplane.store.hot import HotStore

        for name in ("seed_run_identity", "seed_haulier_codes", "rollback_frame_idx",
                     "haulier_codes", "slot_map", "next_frame_idx", "snapshot", "evict"):
            assert callable(getattr(HotStore, name, None)), f"HotStore.{name} is missing"
        for name in ("get_run_meta", "upsert_run_meta", "set_export_state", "get_export_state",
                     "frame_range", "write_frame", "write_kpi_events", "write_breakdown"):
            assert callable(getattr(DuckStore, name, None)), f"DuckStore.{name} is missing"


class TestWriteFailuresAreNotSilent:
    """Regression: a failed DuckDB write dropped up to 500 rows with only a log line, while
    offsets kept auto-committing and /health stayed 200.

    The repair is structural, not a counter, and it is now one rule: the write happens inside
    the handler, so a failure propagates to ``handle_message``, which does not advance that
    record's offset. The row is still in Kafka and is replayed on the next start.
    """

    def test_a_failed_write_holds_its_offset_and_the_replay_lands_it(self, svc):
        calls = {"n": 0}
        real = svc._duck.write_kpi_events

        def flaky(rows):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("TransactionException: write-write conflict")
            return real(rows)

        svc._duck.write_kpi_events = flaky
        msg = FakeMessage(BROKER["kpi"], value={"metric": "m0", "value": 0, "sim_clock": CLOCK})
        assert feed(svc, msg) is False
        assert svc._duck.kpi_rows == []                       # the write failed
        assert svc.consumer.stats()["handler_errors"] == 1
        assert svc.consumer.stats()["discards"] == 1          # judged by ingest_discarding
        assert feed(svc, msg) is True                         # the broker replays it
        assert [row[1] for row in svc._duck.kpi_rows] == ["m0"]

    def test_a_breakdown_write_failure_keeps_the_rows_instead_of_shredding_them(self, svc):
        """The kpi path had a retry buffer and the breakdown path had none — ~35k rows/run."""

        def boom(*args, **kwargs):
            raise RuntimeError("TransactionException")

        svc._duck.write_breakdown_batch = boom
        assert feed(
            svc,
            FakeMessage(
                BROKER["kpi_breakdown"],
                value={
                    "scope": "truck",
                    "sim_clock": CLOCK,
                    "breakdown": {"entities": [{"id": "t1"}]},
                },
            ),
        ) is False
        assert svc._duck.breakdowns == []
        assert svc.consumer.stats()["handler_errors"] == 1

    def test_a_failing_store_fails_the_verdict_and_recovery_clears_it_on_its_own(self, svc):
        """No counter a later success has to clear: the rule is a rate over a window."""
        import apps.dataplane.health as health

        def boom(*args, **kwargs):
            raise RuntimeError("TransactionException")

        svc._duck.write_breakdown_batch = boom
        for hour in range(3):
            feed(
                svc,
                FakeMessage(
                    BROKER["kpi_breakdown"],
                    value={
                        "scope": "truck",
                        "sim_clock": CLOCK,
                        "breakdown": {"entities": [{"id": f"t{hour}"}]},
                    },
                ),
            )

        # kpi keeps flowing and writing perfectly happily; that must NOT hide the breakdown
        # rows that are not reaching the store.
        for i in range(5):
            feed(svc, FakeMessage(BROKER["kpi"], value={"metric": f"m{i}", "value": i, "sim_clock": CLOCK}))

        payload = svc.health_payload()
        assert payload["ok"] is False
        assert "ingest_discarding" in payload["degraded"]
        assert payload["consumer"]["handler_errors"] == 3

        # The store recovers and the broker replays the three records; the window closes on
        # its own, with nothing to reset.
        del svc._duck.write_breakdown_batch
        for hour in range(3):
            assert feed(
                svc,
                FakeMessage(
                    BROKER["kpi_breakdown"],
                    value={
                        "scope": "truck",
                        "sim_clock": CLOCK,
                        "breakdown": {"entities": [{"id": f"t{hour}"}]},
                    },
                ),
            ) is True
        assert len(svc._duck.breakdowns) == 3
        svc.consumer._last_discard_at -= health.DISCARD_WINDOW_S + 1
        assert svc.health_payload()["ok"] is True

    def test_a_frame_write_failure_is_counted_and_gives_the_frame_idx_back(self):
        """A swallowed write_frame left health green and a hole in frame_idx."""
        from apps.dataplane.store.hot import HotStore

        class BrokenFrameDuck(FakeDuck):
            def write_frame(self, run_id, frame):
                raise RuntimeError("disk full")

        service = DataplaneService(
            hot=HotStore(), duck=BrokenFrameDuck(), archive=FakeArchive(), enable_http=False
        )
        try:
            feed(service, _truck_loc())
            for i in range(4):
                feed(service, _truck_loc(lon=4.5 + i))
                assert service.capture_frames() == 0
            assert service.counters["frame_write_failures"] == 4
            assert service.counters["frames_written"] == 0
            # No 0..n hole: the index is handed back, so the first good frame is still 0.
            assert service._hot.next_frame_idx("run_1") == 0
        finally:
            service.stop(timeout=1.0)


class TestBackgroundWork:
    def test_capture_frames_snapshots_each_resident_run(self, svc):
        svc._hot.runs = ["run_a", "run_b"]
        assert svc.capture_frames() == 2
        assert [r for r, _ in svc._duck.frames] == ["run_a", "run_b"]
        assert svc.counters["frames_written"] == 2

    def test_capture_frames_survives_an_evicted_run(self, svc):
        svc._hot.runs = ["gone", "here"]

        def snapshot(run_id, kind=0):
            if run_id == "gone":
                raise RunNotResident("gone")
            return FakeFrame()

        svc._hot.snapshot = snapshot
        assert svc.capture_frames() == 1

    def test_reconcile_once_records_lag(self, svc):
        svc._duck.meta = {"run_1": {}, "run_2": {}}
        svc._archive.dumps = [("run_1", "completed")]
        svc.reconcile_once()
        payload = svc.health_payload()
        assert payload["archive"]["available"] is True
        assert payload["archive"]["lag_runs"] == 1
        assert payload["archive"]["pending_run_ids"] == ["run_2"]

    def test_supervised_tasks_run_and_report_healthy(self):
        service = DataplaneService(
            hot=FakeHot(),
            duck=FakeDuck(),
            archive=FakeArchive(),
            consumer_factory=lambda conf: _IdleConsumer(),
            enable_http=False,
            reconcile_interval_s=0.05,
            frame_interval_s=0.02,
        )
        service.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not service.supervisor.healthy():
                time.sleep(0.01)
            payload = service.health_payload()
            assert payload["ok"] is True, payload
            assert {t["name"] for t in payload["tasks"]} == {
                "consumer", "frames", "reconcile", "watchdog",
            }
            assert service._archive.reconciles >= 1
            assert service._duck.checkpoints == 0
        finally:
            started = time.monotonic()
            service.stop(timeout=2.0)
            assert time.monotonic() - started < 3.0
        assert service._duck.closed is True
        assert service._duck.checkpoints >= 1  # stop() checkpoints on the way out

    def test_health_payload_has_the_pinned_keys(self, svc):
        feed(svc, FakeMessage(BROKER["kpi"], value={"metric": "m", "value": 1, "sim_clock": CLOCK}))
        payload = svc.health_payload()
        assert set(payload) == {
            "ok", "service", "uptime_s", "degraded", "tasks", "consumer", "archive", "hot",
            "store",
        }
        assert set(payload["consumer"]) == {
            "topics", "messages", "handler_errors", "decode_errors", "last_message_age_s",
            "uncommitted", "commit_lag", "commit_stuck_s", "assigned_partitions",
            "broker_lag", "broker_lag_total", "broker_lag_unknown", "broker_lag_rising",
            "discards", "last_discard_age_s",
        }
        assert set(payload["archive"]) == {"available", "lag_runs", "last_dump_at", "pending_run_ids"}
        assert set(payload["hot"]) == {"resident_runs", "trucks", "frames"}
        assert set(payload["store"]) == {
            "db_path", "run_ids", "open_runs", "rows_dropped", "frame_write_failures",
        }
        assert payload["consumer"]["messages"] == 1
        assert len(payload["consumer"]["topics"]) == 6
        assert payload["store"]["db_path"] == "/tmp/fake.duckdb"
        assert payload["store"]["rows_dropped"] == 0


class _IdleConsumer:
    def subscribe(self, topics):
        self.topics = topics

    def poll(self, timeout):
        time.sleep(min(timeout, 0.02))
        return None

    def close(self):
        pass


class TestConcurrency:
    def test_concurrent_kpi_handlers_lose_no_rows(self, svc):
        """Race 8 threads through the buffer; the batch boundary must not drop a row."""
        n_threads, per_thread = 8, 50

        def push(t):
            for i in range(per_thread):
                svc.consumer.handle_message(
                    FakeMessage(
                        BROKER["kpi"],
                        value={"metric": f"m{t}_{i}", "value": i, "sim_clock": CLOCK},
                    )
                )

        threads = [threading.Thread(target=push, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        svc.consumer.flush_writes()
        assert len(svc._duck.kpi_rows) == n_threads * per_thread
        assert len({row[1] for row in svc._duck.kpi_rows}) == n_threads * per_thread
        assert svc.consumer.stats()["handler_errors"] == 0
