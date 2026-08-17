"""Verdict rules of ``apps/dataplane/health.py``, driven through the REAL object graph.

Every service here is a real :class:`DataplaneService` with a real :class:`HotStore`, a real
:class:`DuckStore` on a ``tmp_path`` file and the real supervised tasks; the only doubles are
the Kafka client (the package's rule is that no test constructs a ``confluent_kafka.Consumer``)
and an archive object that is reachable and fails, which is the shape under test.

The two rules these tests pin were wrong in **both** directions at once:

* ``assigned_partitions == 0`` disabled the only silence rule, so a consumer that never
  joined the group answered 200 forever — a real broker-down process measured green for
  25 s with 0 messages, 0 partitions and five healthy tasks;
* ``messages == 0`` (and any 300 s gap) failed the verdict, so an idle dataplane — which is
  what OpenRide is between runs, for hours — answered 503 permanently;
* the archive rule required ``available`` to be false, so an archive that answered ``ping()``
  and failed every ``dump_run`` left runs unarchived forever under a green ``ok``.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from apps.dataplane.health import (
    ARCHIVE_LAG_AFTER_S,
    INGEST_STALL_AFTER_S,
    NOT_CONSUMING_AFTER_S,
)
from apps.dataplane.ingest.consumer import DataplaneConsumer
from apps.dataplane.service import DataplaneService
from apps.dataplane.store.duck import DuckStore

CLOCK = "Mon, 01 Jun 2026 08:00:00 GMT"


class StubKafka:
    """A confluent-Consumer surface that delivers nothing. ``joined`` is the whole point:
    False is the shape of a failed subscribe / unreachable broker / fenced member."""

    def __init__(self, joined: bool = True, partitions: int = 6):
        self.joined = joined
        self.partitions = partitions
        self.subscribed: list = []
        # Broker head per topic, which is what ``get_watermark_offsets`` answers. A double
        # that cannot express where the broker's head is cannot express the one signal that
        # separates a dead ingest from an idle box — and a suite built on such a double
        # passes over both, which is how 244 tests once passed over 14 live defects.
        self.heads: dict = {}
        # An unreachable broker answers neither fetches nor watermarks — the failure is
        # CORRELATED, so the double must be able to stop answering both at once.
        self.watermark_error: Exception | None = None
        self.watermark_calls = 0

    def get_watermark_offsets(self, tp, timeout=None, cached=False):
        self.watermark_calls += 1
        if self.watermark_error is not None:
            raise self.watermark_error
        return (0, int(self.heads.get(tp.topic, 0)))

    def subscribe(self, topics):
        self.subscribed = list(topics)

    def assignment(self):
        return [(t, 0) for t in self.subscribed[: self.partitions]] if self.joined else []

    def poll(self, timeout=0.0):
        time.sleep(min(timeout, 0.01))
        return None

    def commit(self, offsets=None, asynchronous=False):
        return None

    def close(self):
        return None


class ReachableButFailingArchive:
    """Mongo is up (``ping`` ok) and every dump fails — a write-concern/validation error.

    ``reconcile`` swallows its per-run dump failures into ``errors`` exactly as
    ``MongoArchive.reconcile`` does, which is why ``_archive_state['available']`` stays True.
    """

    def __init__(self):
        self.dump_calls = 0

    def ping(self):
        return True

    def ensure_indexes(self):
        pass

    def close(self):
        pass

    def archived_run_ids(self):
        return set()

    def run_summary(self, run_id):
        return None

    def run_is_pending(self, store, run_id, summary=None):
        # Faithful to MongoArchive: no summary at all means the run still needs a dump.
        return (summary if summary is not None else self.run_summary(run_id)) is None

    def reconcile(self, store, reason=""):
        errors = {}
        for run_id in store.list_run_ids():
            try:
                self.dump_run(store, run_id, reason=reason)
            except Exception as exc:  # noqa: BLE001
                errors[run_id] = str(exc)
        return {"checked": len(errors), "dumped": 0, "run_ids": [], "errors": errors}

    def dump_run(self, store, run_id, reason="", complete=None):
        self.dump_calls += 1
        raise RuntimeError("bulk write failed: document validation")


def _service(tmp_path, **kw):
    duck = DuckStore(str(tmp_path / "verdicts.duckdb"))
    service = DataplaneService(duck=duck, enable_http=False, **kw)
    return service


def _await_broker_lag(service, ready, timeout: float = 5.0) -> dict:
    """Health payload once the RUNNING consumer task has refreshed its broker lag.

    Deliberately not a direct call to ``_refresh_broker_lag``: the wiring under test is
    poll loop -> cached watermark -> ``stats()`` -> verdict, and a test that reaches past
    the loop cannot fail when the loop stops calling it. ``ready`` is judged on the lag MAP,
    not on its total, so "not measured yet" ({}) can never be mistaken for "nothing waiting".
    """
    deadline = time.monotonic() + timeout
    while True:
        payload = service.health_payload()
        if ready(payload["consumer"]["broker_lag"]) or time.monotonic() > deadline:
            return payload
        time.sleep(0.1)


def _age_the_process(service, seconds: float) -> None:
    """Pretend the process has been up this long — the clock the rules are written against."""
    service._started_at = time.monotonic() - seconds


class TestNotConsuming:
    def test_a_consumer_that_never_joined_the_group_fails_the_verdict(self, tmp_path):
        """The dead-broker shape: five healthy tasks, zero partitions, nothing ingested."""
        kafka = StubKafka(joined=False)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.5)
            payload = service.health_payload()
            assert payload["consumer"]["assigned_partitions"] == 0
            # Inside the join grace it is not yet a fault: a starting process is not broken.
            assert payload["ok"] is True, payload["degraded"]

            _age_the_process(service, NOT_CONSUMING_AFTER_S + 5)
            payload = service.health_payload()
            assert payload["ok"] is False
            assert "ingest_not_consuming" in payload["degraded"]
            assert all(task["healthy"] for task in payload["tasks"])
        finally:
            service.stop(timeout=3.0)

    def test_a_joined_consumer_that_has_seen_nothing_is_healthy(self, tmp_path):
        """OpenRide is idle between runs. 'No first message yet' is not a fault."""
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(1.5)
            _age_the_process(service, 6 * 3600)
            payload = service.health_payload()
            assert payload["consumer"]["assigned_partitions"] == 6
            assert payload["consumer"]["messages"] == 0
            assert payload["ok"] is True, payload["degraded"]
        finally:
            service.stop(timeout=3.0)


class TestSilenceNeedsALiveRun:
    def test_a_long_gap_between_runs_is_healthy(self, tmp_path):
        """Six minutes of silence with no run resident and nothing uncommitted: 200."""
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            service.consumer._last_message_at = time.monotonic() - (INGEST_STALL_AFTER_S + 100)
            payload = service.health_payload()
            assert payload["hot"]["resident_runs"] == []
            assert payload["ok"] is True, payload["degraded"]
        finally:
            service.stop(timeout=3.0)

    def test_silence_over_records_that_never_reached_the_store_fails_the_verdict(self, tmp_path):
        """The rule is not deleted, it is aimed: consumed-and-unwritten, then silence."""
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            topic = service.consumer.broker_topics()[
                service.consumer.LOGICAL_TOPICS.index("kpi")
            ]
            service.consumer._note_offset((topic, 0, 7), False)   # the write raised
            service.consumer._last_message_at = time.monotonic() - (INGEST_STALL_AFTER_S + 1)
            payload = service.health_payload()
            assert payload["consumer"]["commit_lag"] == {f"{topic}:0": 1}
            assert payload["ok"] is False
            assert "ingest_stalled" in payload["degraded"]
        finally:
            service.stop(timeout=3.0)

    def test_a_run_left_resident_by_a_killed_sim_does_not_latch_the_verdict(self, tmp_path):
        """Residency is not liveness, and the old rule made that mistake permanently.

        A sim killed mid-run publishes no terminal ``run_status`` (CLAUDE.md §8), so its slab
        stays resident AND its run stays open in DuckDB for the life of the process — nothing
        in this package can close a run without a terminal. Keyed on either, ``ingest_stalled``
        answered 503 for ever with nothing able to clear it, and a supervisor healthcheck
        wired to the endpoint — the stated contract — restart-looped the box between runs,
        throwing away the hot tier each time.

        The run must have WRITTEN something for this to be that state. Earlier this test fed
        one ``truck_loc`` and nothing else: ``on_trip_geo`` writes only to the hot tier, so
        ``store.open_runs`` was 0 and the assertion passed over a store state no killed run
        ever has. Adding the one record every run publishes — a kpi — flipped it to 503 at
        +0 s, +1 h, +24 h and +30 d.
        """
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            service.on_kpi("kpi", "R1", {"metric": "empty_km", "value": 1.0, "sim_clock": CLOCK})
            service.flush_writes()
            service.on_trip_geo(
                "trip_geo", "R1",
                {"type": "truck_loc", "truck_agent_id": "t1", "lon": 107.0, "lat": -6.9,
                 "haul_state": "loaded", "haulier_id": "H1", "sim_clock": CLOCK},
            )
            topic = service.consumer.broker_topics()[
                service.consumer.LOGICAL_TOPICS.index("kpi")
            ]
            service.consumer._note_offset((topic, 0, 0), True)
            kafka.heads[topic] = 1   # the sim is dead: the broker has nothing left to give

            # The run is open, resident and unfinishable, and it stays that way for ever.
            assert service.duck.open_run_ids() == ["R1"]
            for silence in (INGEST_STALL_AFTER_S + 1, 3600, 86400, 30 * 86400):
                service.consumer._last_message_at = time.monotonic() - silence
                payload = _await_broker_lag(service, lambda lag: lag.get(f"{topic}:0", -1) == 0)
                assert payload["store"]["open_runs"] == 1
                assert payload["hot"]["resident_runs"] == ["R1"]
                # Every ASSIGNED partition is measured, not only the ones consumed from —
                # and all of them have nothing waiting, which is what idle looks like.
                assert set(payload["consumer"]["broker_lag"]) == {
                    f"{t}:0" for t in service.consumer.broker_topics()
                }
                assert payload["consumer"]["broker_lag_total"] == 0
                assert payload["ok"] is True, (silence, payload["degraded"])
        finally:
            service.stop(timeout=3.0)


class TestDiscardsAreJudged:
    def test_records_thrown_away_at_the_boundary_fail_the_verdict(self, tmp_path):
        """A whole topic could be discarded with every counter at zero and /health at 200.

        The four handler drop paths returned without counting or logging anything, and each
        discarded record's offset was committed anyway — so it was gone from Kafka too.
        Measured: 50 kpi + 20 breakdown + 30 undecodable records in, 0 rows in DuckDB,
        ``run_meta`` None, ``svc.counters`` entirely zero, ``ok=True degraded=[]``.
        """
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.consumer  # the boundary owns the clock, so it must exist first
            service.on_kpi("kpi", "R", {"name": "empty_km", "value": 1.0})   # renamed field
            service.on_kpi_breakdown(
                "kpi_breakdown", "R",
                {"scope": "truck", "breakdown": {"entities": [{"id": "t1"}]}},   # no clock
            )
            service.on_kpi_breakdown(
                "kpi_breakdown", "R",
                {"scope": "truck", "sim_clock": CLOCK,
                 "breakdown": {"entities": ["t1", None]}},                       # all poison
            )
            service.on_trip_geo(
                "trip_geo", "R", {"type": "truck_loc", "truck_agent_id": "t1", "lat": 51.0},
            )
            assert service.counters["kpi_malformed"] == 1
            assert service.counters["kpi_breakdown_malformed"] == 1   # the missing clock
            assert service.counters["kpi_breakdown_entities_dropped"] == 1
            assert service.counters["truck_loc_malformed"] == 1

            payload = service.health_payload()
            assert payload["consumer"]["discards"] == 4
            assert payload["ok"] is False
            assert "ingest_discarding" in payload["degraded"]
        finally:
            service.stop(timeout=3.0)

    def test_the_discard_verdict_is_a_rate_and_clears_itself(self, tmp_path, monkeypatch):
        """No counter here may need a later success to clear it — that was the whole repair."""
        import apps.dataplane.health as health_mod

        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.consumer
            service.on_kpi("kpi", "R", {"value": 1.0})
            assert "ingest_discarding" in service.health_payload()["degraded"]
            monkeypatch.setattr(health_mod, "DISCARD_WINDOW_S", -1.0)
            payload = service.health_payload()
            assert "ingest_discarding" not in payload["degraded"]
            assert payload["consumer"]["discards"] == 1, "the total stays for the postmortem"
        finally:
            service.stop(timeout=3.0)

    def test_a_breakdown_keeps_its_usable_entities_and_drops_only_the_poison(self, tmp_path):
        """The gate is a filter, not a reject: one bad element must not cost the snapshot."""
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.on_kpi_breakdown(
                "kpi_breakdown", "R",
                {"scope": "truck", "sim_clock": CLOCK, "breakdown": {
                    "entities": [{"id": "t1"}, "truck_7", None, {"id": "t2"}]}},
            )
            assert service.duck.breakdown_row_count("R") == 2
            assert service.counters["kpi_breakdown_entities_dropped"] == 1
        finally:
            service.stop(timeout=3.0)


class TestCollaboratorsThatCannotReport:
    def test_a_consumer_whose_stats_raise_is_a_fault_not_an_all_zero_block(self, tmp_path):
        """``{}`` read as "nothing to judge": both ingest rules silently became unevaluable.

        ``topics`` was empty so ``ingest_not_consuming`` could not fire, and
        ``last_message_age_s`` was None so ``ingest_stalled`` could not either — a broken
        consumer reported 200 with a plausible all-zero block and no degraded reason at all.
        """
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            def boom():
                raise RuntimeError("librdkafka is gone")

            service.consumer.stats = boom
            payload = service.health_payload()
            assert payload["ok"] is False
            assert "consumer_stats_unavailable" in payload["degraded"]

            service.consumer.stats = DataplaneConsumer.stats.__get__(service.consumer)
            service.on_trip_geo(
                "trip_geo", "R1",
                {"type": "truck_loc", "truck_agent_id": "t1", "lon": 107.0, "lat": -6.9,
                 "haul_state": "loaded", "haulier_id": "H1", "sim_clock": CLOCK},
            )
            service.hot.stats = boom
            payload = service.health_payload()
            assert payload["ok"] is False
            assert "hot_stats_unavailable" in payload["degraded"]
        finally:
            service.stop(timeout=3.0)


class TestArchiveLag:
    def test_a_reachable_archive_that_fails_every_dump_fails_the_verdict(self, tmp_path):
        """The 2026-07-01 shape: the durable record stops being written, everything green."""
        archive = ReachableButFailingArchive()
        service = _service(
            tmp_path, archive_factory=lambda: archive, reconcile_interval_s=0.2,
            consumer_factory=lambda conf: StubKafka(joined=True),
        )
        try:
            for run_id in ("R1", "R2", "R3"):
                service.duck.write_kpi_events([(run_id, "m", 1.0, dt.datetime(2026, 6, 1, 8))])
                service.duck.upsert_run_meta(run_id, status="completed")
            service.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not service.health_payload()["archive"]["lag_runs"]:
                time.sleep(0.1)
            payload = service.health_payload()
            assert archive.dump_calls > 0
            assert payload["archive"]["available"] is True   # ping() succeeds: reachable
            assert payload["archive"]["lag_runs"] == 3
            # A dump in flight must not flap the endpoint red.
            assert payload["ok"] is True, payload["degraded"]

            _age_the_process(service, ARCHIVE_LAG_AFTER_S + 10)
            payload = service.health_payload()
            assert payload["ok"] is False
            assert "archive_lagging" in payload["degraded"]

            # It is a function of PENDING DATA: a successful dump clears it.
            with service._archive_lock:
                service._archive_state["last_dump_at"] = time.time()
            assert service.health_payload()["ok"] is True
        finally:
            service.stop(timeout=3.0)

    def test_an_unreachable_archive_with_pending_runs_still_fails_immediately(self, tmp_path):
        service = _service(tmp_path, archive_factory=lambda: None, reconcile_interval_s=1e9)
        try:
            assert service.health_payload()["ok"] is True
            service.queue_dump("R9", "completed")
            service.drain_dumps()
            payload = service.health_payload()
            assert payload["archive"]["pending_run_ids"] == ["R9"]
            assert payload["ok"] is False
            assert "archive_unavailable_with_pending_runs" in payload["degraded"]
        finally:
            service.stop(timeout=3.0)


class TestSilenceDuringAHeadlessRun:
    """THE acid test: the OpenRide default is a run with no positions at all."""

    def test_silence_while_the_store_holds_an_unfinished_run_fails_the_verdict(self, tmp_path):
        """A headless run's ingest can die with every counter in this payload reading zero.

        ``ORSIM_HEADLESS`` sets ``stream_geo: false``, so a headless run publishes no
        ``truck_loc`` and never has a hot slab — residency is blind to it by construction.
        ``commit_lag`` is blind to it too: it counts records THIS PROCESS consumed and failed
        to write, and a cleanly dead fetcher produces none. Measured before this rule existed,
        with the fetcher dead and the sim still publishing 2 980 further records:
        ``ok=True degraded=[]`` at 60 s, 400 s, 3 600 s AND 86 400 s of silence, with
        ``commit_lag`` all zeros, ``uncommitted=0`` and ``resident_runs=[]``.

        What makes it visible is the broker's own head: the 2 980 records exist and are not
        being fetched. Nothing inside the process can say that, which is why the verdict is
        no longer keyed on ``store.open_runs`` — an abandoned run is open for ever, so that
        key 503'd an idle box (the same store state) until someone restarted it.
        """
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            service.on_kpi("kpi", "HEADLESS", {"metric": "empty_km", "value": 1.0, "sim_clock": CLOCK})
            service.flush_writes()
            assert service.duck.open_run_ids() == ["HEADLESS"]
            topic = service.consumer.broker_topics()[
                service.consumer.LOGICAL_TOPICS.index("kpi")
            ]
            service.consumer._note_offset((topic, 0, 0), True)   # that record was consumed

            # …and then the fetcher dies while the headless sim keeps publishing.
            kafka.heads[topic] = 3000
            service.consumer._last_message_at = time.monotonic() - (INGEST_STALL_AFTER_S + 1)
            payload = _await_broker_lag(service, lambda lag: lag.get(f"{topic}:0", 0) > 0)
            assert payload["hot"]["resident_runs"] == []      # headless: nothing to be resident
            assert payload["consumer"]["commit_lag"] == {f"{topic}:0": 0}  # nothing unwritten
            assert payload["consumer"]["broker_lag"][f"{topic}:0"] == 2999
            assert payload["consumer"]["broker_lag_total"] == 2999
            assert payload["ok"] is False
            assert "ingest_stalled" in payload["degraded"]

            # It is a function of what is WAITING, not a counter and not a store latch: the
            # verdict clears when the broker has nothing left, with the run still open — the
            # state a sim killed mid-flight leaves behind for the life of the process.
            kafka.heads[topic] = 1
            payload = _await_broker_lag(service, lambda lag: lag.get(f"{topic}:0", -1) == 0)
            assert service.duck.open_run_ids() == ["HEADLESS"]
            assert payload["ok"] is True, payload["degraded"]
        finally:
            service.stop(timeout=3.0)

    def test_a_process_that_ingested_nothing_still_measures_the_assignment(self, tmp_path):
        """Lag derived from partitions already consumed from is blind to the worst shape.

        The process starts, joins, is assigned all six partitions, and the fetch path is dead
        before the FIRST record — a broken ACL, an unavailable leader, a wedged fetcher. There
        is no position to subtract from, so the old refresh issued zero watermark calls and
        ``broker_lag`` stayed ``{}`` while 3 000 records waited: measured 200 ok=True at
        +301 s, +1 h, +24 h and +7 days, with ``assigned_partitions=6`` and ``messages=0``.
        ``ingest_not_consuming`` cannot fire (it needs assigned == 0) and the stall rule was
        gated on ``last_message_age_s is not None``, which is None here. This is the
        2026-07-01 shape, and the state after every restart during an idle period.
        """
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            topic = service.consumer.broker_topics()[
                service.consumer.LOGICAL_TOPICS.index("kpi")
            ]
            # Nothing has ever been consumed: no _note_offset, no handler call, no message.
            payload = _await_broker_lag(service, lambda lag: f"{topic}:0" in lag)
            assert payload["consumer"]["messages"] == 0
            assert payload["consumer"]["last_message_age_s"] is None
            assert payload["consumer"]["assigned_partitions"] == 6
            assert set(payload["consumer"]["broker_lag"]) == {
                f"{t}:0" for t in service.consumer.broker_topics()
            }
            _age_the_process(service, INGEST_STALL_AFTER_S + 1)
            assert service.health_payload()["ok"] is True   # nothing waiting: still idle

            # …and now the sim publishes 3 000 records this process will never fetch.
            kafka.heads[topic] = 3000
            payload = _await_broker_lag(service, lambda lag: lag.get(f"{topic}:0", 0) > 0)
            assert payload["consumer"]["broker_lag"][f"{topic}:0"] == 3000
            assert payload["consumer"]["messages"] == 0
            assert payload["ok"] is False, payload["consumer"]
            assert "ingest_stalled" in payload["degraded"]
        finally:
            service.stop(timeout=3.0)

    def test_a_watermark_lookup_that_raises_is_unknown_and_never_zero(self, tmp_path):
        """The signal must fail CLOSED: unknown is a fault, not "nothing waiting".

        This is the correlated failure — the broker that stopped answering fetches is the
        same one that stopped answering watermarks — so it is exactly the moment the only
        signal that can see a dead fetcher must not go quiet. Measured before this fix, with
        3 000 records waiting and every lookup raising: the ``except`` omitted the key, the
        lag map came back ``{}``, ``broker_lag_total`` summed to 0, and /health answered
        ``200 ok=True degraded=[]`` at +301 s and at +24 h.
        """
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            topic = service.consumer.broker_topics()[
                service.consumer.LOGICAL_TOPICS.index("kpi")
            ]
            service.consumer._note_offset((topic, 0, 0), True)
            payload = _await_broker_lag(service, lambda lag: f"{topic}:0" in lag)
            assert payload["consumer"]["broker_lag_total"] == 0
            assert payload["consumer"]["broker_lag_unknown"] == 0

            # …and now the broker goes away entirely, while records pile up behind it.
            kafka.heads[topic] = 3000
            kafka.watermark_error = RuntimeError("Local: Broker transport failure")
            service.consumer._last_message_at = time.monotonic() - (INGEST_STALL_AFTER_S + 1)
            payload = _await_broker_lag(
                service, lambda _lag: service.consumer.stats()["broker_lag_unknown"] > 0
            )
            consumer_out = payload["consumer"]
            assert consumer_out["broker_lag_unknown"] >= 1
            # The last number anyone actually measured survives; it is not silently deleted.
            assert consumer_out["broker_lag"][f"{topic}:0"] == 0
            assert consumer_out["broker_lag_total"] == 0
            assert payload["ok"] is False, consumer_out
            assert "ingest_lag_unknown" in payload["degraded"]

            # …and it clears itself the moment the broker can answer again.
            kafka.watermark_error = None
            payload = _await_broker_lag(service, lambda lag: lag.get(f"{topic}:0", 0) > 0)
            assert payload["consumer"]["broker_lag_unknown"] == 0
            assert "ingest_lag_unknown" not in payload["degraded"]
        finally:
            service.stop(timeout=3.0)

    def test_silence_with_no_unfinished_run_is_still_healthy(self, tmp_path):
        """The other half: OpenRide is idle for hours between runs and that is not a fault."""
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            service.on_kpi("kpi", "DONE", {"metric": "empty_km", "value": 1.0, "sim_clock": CLOCK})
            service.flush_writes()
            service.duck.upsert_run_meta("DONE", status="completed")
            service.consumer._last_message_at = time.monotonic() - (INGEST_STALL_AFTER_S + 5000)
            payload = service.health_payload()
            assert payload["store"]["open_runs"] == 0
            assert payload["ok"] is True, payload["degraded"]
        finally:
            service.stop(timeout=3.0)


class TestAWedgedPartitionIsAFaultOfItsOwn:
    def test_a_commit_point_that_stands_still_under_traffic_fails_the_verdict(self, tmp_path):
        """A partition frozen by a handler that raised was green for the rest of every run.

        ``uncommitted`` is projected into the payload and read by no rule at all, and
        ``commit_lag`` is read only together with 300 s of silence — which a live run never
        has. So one record that could not be written froze ``kpi_stream``'s commit point,
        every later record was written but could never be committed, every restart replayed
        the poison and re-froze, and /health answered 200 throughout.
        """
        kafka = StubKafka(joined=True)
        service = _service(
            tmp_path, consumer_factory=lambda conf: kafka,
            archive_factory=lambda: None, reconcile_interval_s=1e9,
        )
        try:
            service.start()
            time.sleep(0.3)
            topic = service.consumer.broker_topics()[
                service.consumer.LOGICAL_TOPICS.index("kpi")
            ]
            service.consumer._note_offset((topic, 0, 4), False)      # the write raised
            for offset in range(5, 11):                              # …and the run carries on
                service.consumer._note_offset((topic, 0, offset), True)
            service.consumer._last_message_at = time.monotonic()     # LIVE: no silence at all
            payload = service.health_payload()
            assert payload["consumer"]["commit_lag"][f"{topic}:0"] == 7
            assert "ingest_stalled" not in payload["degraded"]       # the silence rule is blind
            assert payload["ok"] is True, payload["degraded"]        # …inside the window

            # A standstill, not a mere quantity: judged once it has lasted a stall interval,
            # which is what keeps a one-poll-cycle batch from being a fault.
            service.consumer._commit_moved_at[(topic, 0)] = (
                time.monotonic() - (INGEST_STALL_AFTER_S + 1)
            )
            payload = service.health_payload()
            assert payload["consumer"]["commit_stuck_s"] > INGEST_STALL_AFTER_S
            assert payload["ok"] is False
            assert "ingest_wedged" in payload["degraded"]

            # And it clears the moment the point moves: it is not a counter awaiting a success.
            service.consumer._note_offset((topic, 0, 4), True)
            for offset in range(5, 11):
                service.consumer._note_offset((topic, 0, offset), True)
            payload = service.health_payload()
            assert payload["consumer"]["commit_stuck_s"] is None
            assert payload["ok"] is True, payload["degraded"]
        finally:
            service.stop(timeout=3.0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
