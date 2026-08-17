"""Round-4 repairs in ``service.py``, each against the REAL collaborators.

Every test here drives a real :class:`DataplaneService` over a real :class:`DuckStore` and a
real ``HotStore`` (the service builds the hot tier itself). The only stub is a message object
with ``topic()/key()/value()/error()/partition()/offset()`` — there is no broker on this box —
and, where a store failure is the point, a ``DuckStore`` subclass that raises from exactly one
method. The seams these cover were all invisible to the existing suite because it exercises
them through a fake store that never fails one call while succeeding its neighbours.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from apps.dataplane import health
from apps.dataplane.service import DataplaneService
from apps.dataplane.store.duck import DuckStore, DuckStoreError

CLOCK = "Wed, 05 Aug 2026 10:00:00 GMT"


def _svc(tmp_path, duck=None, **kw):
    duck = duck if duck is not None else DuckStore(str(tmp_path / "dp.duckdb"))
    return DataplaneService(
        duck=duck, enable_http=False, archive_factory=lambda: None, **kw
    ), duck


def _loc(svc, run_id, agent="t1", lon=4.0, lat=51.0, haulier="ACME", clock=CLOCK):
    svc.on_trip_geo(
        "trip_geo",
        run_id,
        {
            "type": "truck_loc",
            "truck_agent_id": agent,
            "lon": lon,
            "lat": lat,
            "haul_state": "loaded_in_transit",
            "haulier_id": haulier,
            "sim_clock": clock,
        },
    )


def _frames(duck, run_id):
    return duck.query(
        "SELECT frame_idx, count(*) AS n, max(sim_time_ms) AS t FROM frames "
        "WHERE run_id = ? GROUP BY 1 ORDER BY 1",
        [run_id],
    )


# ---------------------------------------------------------------------------- run_meta


class _MetaBlip(DuckStore):
    """A store whose ``upsert_run_meta`` fails ``fail`` times. Everything else works."""

    fail = 0
    attempts = 0

    def upsert_run_meta(self, run_id, **fields):
        type(self).attempts += 1
        if type(self).fail > 0:
            type(self).fail -= 1
            raise DuckStoreError("TransactionException: Conflict on update!")
        return super().upsert_run_meta(run_id, **fields)


def test_a_failed_run_meta_write_is_retried_by_the_next_sweep(tmp_path):
    """The signature must be committed by the WRITE, not by the attempt.

    Recorded before the upsert, one failed write was never retried: the code book stops
    growing once every truck and haulier has been seen, so ``sig != _run_meta_sig[run_id]``
    — the sweep's only trigger — never fired again and the run's haulier code book and slot
    map never reached disk at all. A run that ends without a terminal (a crashed sim) then
    archives frames whose uint8 haulier and uint32 slot have no dictionary anywhere.
    """
    _MetaBlip.fail, _MetaBlip.attempts = 1, 0
    duck = _MetaBlip(str(tmp_path / "dp.duckdb"))
    svc, _ = _svc(tmp_path, duck=duck)
    try:
        for i in range(3):
            _loc(svc, "R", agent=f"t{i}", lon=4.0 + i, haulier=f"H{i % 2}")
        svc.capture_frames()  # swallows the meta failure, exactly as in production
        assert duck.get_run_meta("R") is None
        assert _MetaBlip.attempts == 1

        # the store is healthy again and the code book has stopped growing
        for sweep in range(5):
            for i in range(3):
                _loc(svc, "R", agent=f"t{i}", lon=4.0 + i + sweep, haulier=f"H{i % 2}")
            svc.capture_frames()

        meta = duck.get_run_meta("R")
        assert meta is not None, "the failed run_meta write was never retried"
        assert json.loads(meta["haulier_codes"]) == {"H0": 1, "H1": 2}
        assert json.loads(meta["slot_map"]) == {"t0": 0, "t1": 1, "t2": 2}
        assert svc.counters["meta_writes"] == 1

        # and the point of all that: a second process reads the same book back.
        svc2, _ = _svc(tmp_path, duck=duck)
        try:
            assert svc2.restore_run_codes("R") is True
            _loc(svc2, "R", agent="t9", haulier="H1")
            assert svc2.hot.haulier_codes("R")["H1"] == svc.hot.haulier_codes("R")["H1"]
        finally:
            svc2.stop(timeout=1.0)
    finally:
        svc.stop(timeout=1.0)


# ---------------------------------------------------------------------------- frames


def test_the_terminal_capture_does_not_duplicate_the_final_frame(tmp_path):
    """The normal end of every run: a terminal arrives with nothing new since the sweep.

    ``frames`` has no primary key, so this write is the one in the package that a replay
    cannot deduplicate: a second frame_idx for one instant breaks the frame_idx<->sim_time
    mapping the replay path reads, and Mongo stores it as a separate document.
    """
    svc, duck = _svc(tmp_path)
    try:
        for i in range(5):
            _loc(svc, "R", agent=f"t{i}", lon=4.0 + i)
        assert svc.capture_frames() == 1
        svc.finalize_run("R", "completed")

        rows = _frames(duck, "R")
        assert [r["frame_idx"] for r in rows] == [0]
        assert rows[0]["n"] == 5
        assert svc.counters["frames_written"] == 1
    finally:
        svc.stop(timeout=1.0)


def test_a_terminal_that_is_retried_writes_no_extra_frames(tmp_path):
    """N failed terminal attempts used to give N+1 identical copies of the final frame.

    ``finalize_run`` propagates the meta failure out of ``on_run_status``, so the terminal
    record's offset is not committed and the broker replays it — unboundedly, across
    restarts — and every replay used to mint another copy under a fresh frame_idx.
    """
    _MetaBlip.fail, _MetaBlip.attempts = 2, 0
    duck = _MetaBlip(str(tmp_path / "dp.duckdb"))
    svc, _ = _svc(tmp_path, duck=duck)
    try:
        _loc(svc, "R", lon=100.0)
        for _ in range(3):  # two failures, then the write that lands
            try:
                svc.finalize_run("R", "completed")
                break
            except DuckStoreError:
                continue

        rows = _frames(duck, "R")
        assert [r["frame_idx"] for r in rows] == [0]
        assert duck.query(
            "SELECT count(DISTINCT sim_time_ms) AS d, count(DISTINCT frame_idx) AS f "
            "FROM frames WHERE run_id = ?", ["R"],
        )[0] == {"d": 1, "f": 1}
        assert duck.get_run_meta("R")["max_frame_idx"] == 0
        assert svc.counters["runs_finalized"] == 1
    finally:
        svc.stop(timeout=1.0)


def test_a_finalized_run_is_never_re_admitted_to_the_hot_tier(tmp_path):
    """``trip_geo`` and ``run_status`` are different topics, so this ordering is real.

    Eviction alone did not prevent re-creation: ``update_position`` -> ``ensure_run`` rebuilt
    the slab from carryover and the sweep resumed writing frames into DuckDB *after* the
    archive dump had been queued, with ``run_meta.max_frame_idx`` frozen at the terminal's
    value. DuckDB and the archive then disagreed permanently under a complete summary.
    """
    svc, duck = _svc(tmp_path)
    try:
        for i in range(3):
            _loc(svc, "R", lon=4.0 + i)
            svc.capture_frames()
        svc.finalize_run("R", "completed")
        frames_at_terminal = len(_frames(duck, "R"))
        assert svc.hot.resident_runs() == []

        before = svc.counters["trip_geo_other"]
        for i in range(10):
            _loc(svc, "R", lon=9.0 + i)
            svc.capture_frames()

        assert svc.hot.resident_runs() == [], "the finished run came back to life"
        assert len(_frames(duck, "R")) == frames_at_terminal
        assert svc.counters["trip_geo_other"] - before == 10
        assert svc.counters["truck_loc"] == 3
        meta = duck.get_run_meta("R")
        assert meta["status"] == "completed"
        assert meta["max_frame_idx"] == frames_at_terminal - 1
    finally:
        svc.stop(timeout=1.0)


# ---------------------------------------------------------------------------- health


class _NoFrames(DuckStore):
    """Everything works except the frame path — the shape that was invisible."""

    def write_frame(self, run_id, frame):
        raise DuckStoreError("IO Error: frame write failed")

    def write_frames(self, run_id, frames):
        raise DuckStoreError("IO Error: frame write failed")


def test_the_hot_tier_is_not_built_with_a_bound_a_fifth_run_can_evict(tmp_path):
    """``HotStore``'s default ``max_runs`` is 4, and the service used to accept it.

    Measured: run RA's uncaptured positions were dropped as soon as RB..RE appeared on
    ``trip_geo`` — a restart replaying a backlog, or two overlapping sims, is enough — with
    ``frames_written=4``, ``frame_write_failures=0`` and ``runs_evicted=0``, i.e. entirely
    invisible. Those positions are unrecoverable: ``trip_geo`` offsets commit on receipt.
    """
    from apps.dataplane.service import HOT_MAX_RUNS

    assert HOT_MAX_RUNS >= 64
    svc, duck = _svc(tmp_path)
    try:
        for i in range(HOT_MAX_RUNS):
            _loc(svc, f"R{i}")
        assert len(svc.hot.resident_runs()) == HOT_MAX_RUNS
        assert svc.capture_frames() == HOT_MAX_RUNS
        assert svc.counters["positions_dropped"] == 0
    finally:
        svc.stop(timeout=1.0)


def test_an_lru_eviction_is_counted_as_the_row_loss_it_is(tmp_path):
    """The audit that proves the bound above is doing its job.

    Nothing but ``evict_run`` used to pop the run's bookkeeping, and the LRU path never calls
    it, so the loss had no counter at all — only an INFO line inside ``hot.py``.
    """
    from apps.dataplane.store.hot import HotStore

    svc, duck = _svc(tmp_path, hot=HotStore(max_runs=4))
    try:
        _loc(svc, "RA")                                  # never captured
        for run_id in ("RB", "RC", "RD", "RE"):
            _loc(svc, run_id)
        assert "RA" not in svc.hot.resident_runs()

        assert svc.capture_frames() == 4
        assert svc.counters["positions_dropped"] == 1
        assert svc.counters["runs_evicted"] == 0         # it did not go through that door

        payload = svc.health_payload()
        assert payload["store"]["rows_dropped"] == 1
        assert payload["ok"] is False
        assert "store_rows_dropped" in payload["degraded"]
        assert [r["run_id"] for r in _frames(duck, "RA")] == []
    finally:
        svc.stop(timeout=1.0)


def test_a_run_whose_first_position_lands_mid_sweep_is_not_reported_as_a_loss(tmp_path):
    """``resident_runs()`` cannot be read before the set of runs it is compared against.

    Read the other way round, a run whose very first ``truck_loc`` arrived between the two
    reads was absent from the older residency list and counted as destroyed positions —
    which, wired to a verdict, is a 503 on a perfectly healthy live run.
    """
    svc, _ = _svc(tmp_path)
    try:
        _loc(svc, "OLD")
        real_resident = svc.hot.resident_runs

        def slow_resident():
            runs = real_resident()
            _loc(svc, "NEW")          # a brand-new run appears inside the window
            return runs

        svc._hot.resident_runs = slow_resident
        svc.capture_frames()
        assert svc.counters["positions_dropped"] == 0
        assert "NEW" in svc._run_updates
    finally:
        svc.stop(timeout=1.0)


def test_a_capture_that_is_not_resident_is_counted_not_guessed(tmp_path):
    """``except Exception`` made "nothing to capture" and "the hot tier is broken" identical.

    The docstring said the two cases are deliberately not the same thing; the code could not
    tell them apart, so ``finalize_run`` went on to write the status, evict and archive a run
    whose position series had just failed to be read.
    """
    svc, _ = _svc(tmp_path)
    try:
        _loc(svc, "R")                                   # builds the real hot tier
        assert svc.capture_run("HEADLESS") == 0
        assert svc.counters["frames_not_resident"] == 1
        assert svc.counters["frame_write_failures"] == 0

        def broken(run_id, kind=0):
            raise MemoryError("the hot tier is broken")

        _loc(svc, "R2")
        svc._hot.snapshot = broken
        with pytest.raises(MemoryError):
            svc.capture_run("R2")                        # must stop finalize_run, not be eaten
        assert svc.counters["frames_not_resident"] == 1
    finally:
        svc.stop(timeout=1.0)


def test_the_body_separates_a_broken_frame_path_from_a_healthy_one(tmp_path):
    """An operator must be able to see WHICH of the two identical-looking states this is."""
    healthy, _ = _svc(tmp_path)
    broken, _ = _svc(tmp_path, duck=_NoFrames(str(tmp_path / "broken.duckdb")))
    try:
        _loc(healthy, "R")
        assert healthy.capture_frames() == 1
        _loc(broken, "R")
        broken.capture_frames()
        assert healthy.health_payload()["store"]["frame_write_failures"] == 0
        assert broken.health_payload()["store"]["frame_write_failures"] >= 1
    finally:
        healthy.stop(timeout=1.0)
        broken.stop(timeout=1.0)


def test_an_idle_resident_run_reports_no_fault(tmp_path):
    """A run that has simply stopped moving must not latch the verdict red."""
    svc, _ = _svc(tmp_path)
    try:
        _loc(svc, "R")
        assert svc.capture_frames() == 1
        assert svc.health_payload()["ok"] is True
        assert svc.capture_frames() == 0            # nothing moved
        assert svc.health_payload()["ok"] is True
    finally:
        svc.stop(timeout=1.0)


# ---------------------------------------------------------------------------- poll thread


class _Msg:
    """A record that can report its offset. The consumer and the store are both real."""

    def __init__(self, topic, key, value, partition=0, offset=0):
        self._t, self._k, self._v, self._p, self._o = topic, key, value, partition, offset

    def topic(self):
        return self._t

    def key(self):
        return self._k.encode()

    def value(self):
        return json.dumps(self._v).encode()

    def error(self):
        return None

    def partition(self):
        return self._p

    def offset(self):
        return self._o


def test_the_poll_thread_writes_and_commits_and_a_failure_holds_the_offset(tmp_path):
    """The whole write path, end to end, with no queue and nothing in between.

    What this replaces: a bounded ``WriteQueue`` whose rejected offers kept their offset
    marks, so one backpressure event pinned ``kpi_stream`` at offset 4 of 40 consumed records
    for the life of the process (36 leaked marks, surviving a clean drain) and latched
    /health red on a system where nothing was stuck and nothing could clear it. There is no
    queue to be full and no mark to leak.
    """
    duck = DuckStore(str(tmp_path / "dp.duckdb"))
    svc, _ = _svc(tmp_path, duck=duck)
    try:
        topic = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("kpi")]
        for i in range(3):
            assert svc.consumer.handle_message(
                _Msg(topic, "R", {"metric": f"m{i}", "value": 1.0, "sim_clock": CLOCK}, offset=i)
            ) is True
        assert svc.consumer.flush_writes() is True   # one statement for the whole poll batch
        assert duck.kpi_row_count("R") == 3
        assert dict(((t, p), o) for t, p, o in svc.consumer.safe_offsets())[(topic, 0)] == 3

        # Now the store refuses. The batch is not written, so no offset in it moves.
        def boom(rows):
            raise DuckStoreError("IO Error: disk full")

        duck.write_kpi_events = boom
        assert svc.consumer.handle_message(
            _Msg(topic, "R", {"metric": "m3", "value": 1.0, "sim_clock": CLOCK}, offset=3)
        ) is True                                    # accepted into the batch…
        assert svc.consumer.flush_writes() is False  # …and the batch could not be written
        assert duck.kpi_row_count("R") == 3
        assert dict(((t, p), o) for t, p, o in svc.consumer.safe_offsets())[(topic, 0)] == 3

        payload = svc.health_payload()
        assert payload["ok"] is False
        assert "ingest_discarding" in payload["degraded"]
        assert payload["consumer"]["commit_lag"][f"{topic}:0"] == 1
    finally:
        svc.stop(timeout=1.0)


# ── round-7 repairs ─────────────────────────────────────────────────────────


def test_an_lru_eviction_that_lost_nothing_is_not_counted_as_a_loss(tmp_path):
    """Leaving the hot tier is not a loss; leaving it with UNWRITTEN work is.

    ``capture_frames``' audit only asked whether the run was still resident, so a run whose
    every position was already on disk was counted as ``positions_dropped`` the moment the LRU
    reclaimed its slab. That counter is monotonic and feeds ``store_rows_dropped``, so /health
    went 503 over frames sitting in DuckDB and no later success could ever clear it — exactly
    the "counter only a later success can clear" shape this package deleted. Reachable in
    production because a sim killed without a terminal never finalizes, so slabs accumulate
    until ``HOT_MAX_RUNS`` fires.
    """
    from apps.dataplane.store.hot import HotStore

    svc, duck = _svc(tmp_path, hot=HotStore(max_runs=2))
    try:
        _loc(svc, "RA")
        _loc(svc, "RB")
        assert svc.capture_frames() == 2
        assert len(_frames(duck, "RA")) == 1          # RA is FULLY captured, on disk

        _loc(svc, "RC")                               # …and now the LRU reclaims its slab
        assert "RA" not in svc.hot.resident_runs()
        svc.capture_frames()

        assert svc.counters["positions_dropped"] == 0
        payload = svc.health_payload()
        assert payload["store"]["rows_dropped"] == 0
        assert "store_rows_dropped" not in payload["degraded"]
        for _ in range(3):                            # three further clean sweeps
            svc.capture_frames()
        assert "store_rows_dropped" not in svc.health_payload()["degraded"]
        assert len(_frames(duck, "RA")) == 1          # the frame really is still there
    finally:
        svc.stop(timeout=1.0)


def test_a_run_the_lru_evicted_before_its_terminal_is_counted_as_the_loss_it_is(tmp_path):
    """The mirror image: closed and archived with an empty frame series, under a green 200.

    ``capture_run`` catches ``RunNotResident`` and returns 0 — correct for headless, wrong
    here — and ``finalize_run`` went on to persist ``status='completed'``, evict and queue the
    dump. Measured: 0 frames in DuckDB, ``run_meta.status='completed'``, archived with
    ``frame_count=0``, ``positions_dropped=0`` and ``/health ok=True degraded=[]``.
    ``frames_not_resident`` counted it and no endpoint read that counter.
    """
    from apps.dataplane.store.hot import HotStore

    svc, duck = _svc(tmp_path, hot=HotStore(max_runs=2))
    try:
        _loc(svc, "RX")                               # ten positions, never captured
        for run_id in ("RY", "RZ"):
            _loc(svc, run_id)
        assert "RX" not in svc.hot.resident_runs()

        svc.finalize_run("RX", "completed")

        assert _frames(duck, "RX") == []
        assert duck.get_run_meta("RX")["status"] == "completed"
        assert svc.counters["frames_not_resident"] == 1
        assert svc.counters["positions_dropped"] == 1
        payload = svc.health_payload()
        assert payload["store"]["rows_dropped"] == 1
        assert "store_rows_dropped" in payload["degraded"]

        # …and a run whose positions ARE all captured finalizes clean: no false loss.
        svc.capture_frames()
        before = svc.counters["positions_dropped"]
        svc.finalize_run("RY", "completed")
        assert svc.counters["positions_dropped"] == before
        assert len(_frames(duck, "RY")) >= 1
    finally:
        svc.stop(timeout=1.0)


@pytest.mark.parametrize(
    "bad, why",
    [
        ({"metric": "m", "value": 1.0}, "sim_clock missing"),
        ({"metric": "m", "value": 1.0, "sim_clock": "2026-06-01 08:00 (sim tick 42)"},
         "sim_clock unparseable"),
        ({"metric": "m", "value": 1.0, "sim_clock": 1717228800}, "sim_clock numeric"),
        ({"metric": "m", "value": "n/a", "sim_clock": CLOCK}, "value non-numeric"),
    ],
)
def test_a_payload_the_store_cannot_represent_is_discarded_not_wedged(tmp_path, bad, why):
    """One producer message must not pin a partition for the life of the process.

    ``parse_sim_clock`` and ``coerce_metric_value`` were unguarded, so each of these four
    payload classes raised out of ``on_kpi``. Under the offset rule a raising handler freezes
    its partition at its own offset forever, and a restart replays the same byte-identical
    record and re-freezes: measured committed=5 of 30, uncommitted=25, with no quarantine, no
    drop and no operator exit short of hand-editing the group offset. A payload that can never
    be stored is a discard — counted, judged as a rate by ``ingest_discarding``, committed.
    """
    svc, duck = _svc(tmp_path)
    try:
        topic = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("kpi")]
        good = lambda i: {"metric": f"m{i}", "value": 1.0, "sim_clock": CLOCK}  # noqa: E731
        for i in range(5):
            svc.consumer.handle_message(_Msg(topic, "R", good(i), offset=i))
        svc.consumer.handle_message(_Msg(topic, "R", bad, offset=5))
        for i in range(6, 11):
            svc.consumer.handle_message(_Msg(topic, "R", good(i), offset=i))
        assert svc.consumer.flush_writes() is True

        assert duck.kpi_row_count("R") == 10, why
        offsets = dict(((t, p), o) for t, p, o in svc.consumer.safe_offsets())
        assert offsets[(topic, 0)] == 11, f"{why}: the partition is wedged"
        stats = svc.consumer.stats()
        assert stats["commit_lag"][f"{topic}:0"] == 0
        assert stats["handler_errors"] == 0          # a discard, not a store failure
        assert stats["discards"] == 1
        assert svc.counters["kpi_malformed"] == 1
        assert "ingest_discarding" in svc.health_payload()["degraded"]
    finally:
        svc.stop(timeout=1.0)


class _KpiBlip(DuckStore):
    """A real store whose kpi write refuses exactly one batch — a transient failure."""

    fail_kpi = 0

    def write_kpi_events(self, rows):
        if self.fail_kpi:
            self.fail_kpi -= 1
            raise DuckStoreError("TransactionException: injected kpi failure")
        return super().write_kpi_events(rows)


def test_a_run_whose_ingest_is_blocked_is_not_archived_with_a_hole(tmp_path):
    """One transient kpi write failure used to produce a durable record that is WRONG.

    The rollback is correctly scoped to ``kpi_stream``, so ``run_status`` keeps committing —
    and the terminal then captured, persisted, evicted and dumped the run while the rows the
    failed flush dropped were still sitting unconsumed on the broker. Measured: 100 of 200
    kpi rows on disk (m0..m99 a hole, m100..m199 durable), and Mongo holding ``kpi_count=100``
    with ``summary complete=True``. Nothing repairs that: ``reconcile_once`` dumps 0 because
    ``run_is_pending`` compares DuckDB's 100 against the archive's 100 and finds them equal.
    /health did go red — but it said ``ingest_wedged``, not "the archive you just wrote is
    wrong".

    The terminal path is the one place that can ask whether the run's ingest is whole, and it
    already calls ``flush_writes`` there. Refusing does not advance the terminal's offset, so
    the restart that replays the blocked partition also replays the terminal (measured after
    restart: 200/200 rows in DuckDB and in Mongo, 0 duplicates, MISSING=[]).
    """
    duck = _KpiBlip(str(tmp_path / "dp.duckdb"))
    svc, _ = _svc(tmp_path, duck=duck)
    try:
        kpi = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("kpi")]
        status = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("run_status")]
        row = lambda i: {"metric": f"m{i}", "value": float(i), "sim_clock": CLOCK}  # noqa: E731

        duck.fail_kpi = 1
        for i in range(100):
            svc.consumer.handle_message(_Msg(kpi, "R1", row(i), offset=i))
        assert svc.consumer.flush_writes() is False      # dropped, rolled back, blocked
        for i in range(100, 200):
            svc.consumer.handle_message(_Msg(kpi, "R1", row(i), offset=i))
        assert svc.consumer.flush_writes() is True       # the store is healthy again…
        assert duck.kpi_row_count("R1") == 100           # …and m0..m99 are a HOLE
        assert "kpi" in svc.consumer.blocked_topics()

        # …and now the run ends normally, on the topic that was never rolled back.
        svc.consumer.handle_message(_Msg(
            status, "R1",
            {"status": "COMPLETED", "lifecycle_scope": "simulation",
             "simulation_active": False},
            offset=0,
        ))
        assert svc.counters.get("runs_finalized", 0) == 0
        assert duck.open_run_ids() == ["R1"]     # not statused, not evicted, not queued
        # The terminal's offset does NOT advance, so a restart replays it — which is the
        # only thing that can repair the hole.
        offsets = dict(((t, p), o) for t, p, o in svc.consumer.safe_offsets())
        assert offsets.get((status, 0), 0) == 0
        assert svc.health_payload()["ok"] is False
    finally:
        svc.stop(timeout=1.0)


@pytest.mark.parametrize("second", [1.5, 7, True, None], ids=["float", "int", "bool", "null"])
def test_a_mixed_type_haulier_id_cannot_wedge_run_status(tmp_path, second):
    """Rejecting the unhashable types was not enough — the poison is a MIXED code book.

    ``on_trip_geo``'s guard turned away dict/list/set, so a JSON number or boolean sailed
    through into the hot tier's book and keyed it by the raw value: ``{"H1": 1, 1.5: 2}``.
    The terminal then reached ``json.dumps(codes, sort_keys=True)``, whose sort compares the
    keys against each other, and raised ``TypeError: '<' not supported between instances of
    'str' and 'float'`` out of ``persist_run_meta`` -> ``finalize_run`` -> ``on_run_status``.
    Measured: handler_errors=1, runs_finalized=0, the run_status commit point frozen at 0 with
    commit_lag 1 and commit_stuck_s climbing, open_run_ids=['RC'] — the run never finalized,
    never persisted with a status, never archived — and because the terminal's offset never
    advances, the byte-identical record replays and re-wedges on every restart. This is
    ``run_status``, the one topic whose lost consumer thread WAS the 2026-07-01 outage.

    Coerced at the boundary the way ``agent_id`` already is, the mixed book cannot be built.
    """
    svc, duck = _svc(tmp_path)
    try:
        geo = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("trip_geo")]
        status = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("run_status")]
        for i, haulier in enumerate(("H1", second)):
            svc.consumer.handle_message(_Msg(
                geo, "RC",
                {"type": "truck_loc", "truck_agent_id": f"t{i}", "lon": 4.0, "lat": 51.0,
                 "haul_state": "loaded_in_transit", "haulier_id": haulier,
                 "sim_clock": CLOCK},
                offset=i,
            ))
        svc.consumer.handle_message(_Msg(
            status, "RC",
            {"status": "COMPLETED", "lifecycle_scope": "simulation",
             "simulation_active": False},
            offset=0,
        ))

        stats = svc.consumer.stats()
        assert stats["handler_errors"] == 0
        assert stats["commit_lag"][f"{status}:0"] == 0
        assert svc.counters["runs_finalized"] == 1
        assert duck.open_run_ids() == []
        # The terminal is committable, so no restart replays it.
        offsets = dict(((t, p), o) for t, p, o in svc.consumer.safe_offsets())
        assert offsets[(status, 0)] == 1

        # …and the book that is persisted is a real dictionary, not integers with nothing to
        # read them by: every key a string, and the second truck's haulier present.
        meta = duck.get_run_meta("RC") or {}
        codes = json.loads(meta["haulier_codes"])
        assert all(isinstance(key, str) for key in codes)
        assert "H1" in codes
        if second is not None:
            assert str(second) in codes
    finally:
        svc.stop(timeout=1.0)


def test_a_poll_batch_is_written_in_one_statement(tmp_path):
    """One DuckDB transaction costs ~6.5 ms whatever it carries.

    Writing once per record therefore capped ingest at ~141 kpi msg/s against a measured live
    rate of 132 — 1.06x headroom — and a backlog that grew silently as soon as the rate rose
    (measured at 400 msg/s: 282 sustained, broker backlog 452 -> 2 384 in 20 s, /health green).
    Batched over one poll cycle the same rows cost 0.05 ms each. This pins the mechanism: the
    store is called once for the whole batch and not at all before the flush.
    """

    class CountingDuck(DuckStore):
        def __init__(self, path):
            super().__init__(path)
            self.calls = 0
            self.rows = 0

        def write_kpi_events(self, rows):
            self.calls += 1
            self.rows += len(rows)
            return super().write_kpi_events(rows)

    duck = CountingDuck(str(tmp_path / "dp.duckdb"))
    svc, _ = _svc(tmp_path, duck=duck)
    try:
        topic = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("kpi")]
        for i in range(200):
            assert svc.consumer.handle_message(
                _Msg(topic, "R", {"metric": f"m{i}", "value": 1.0, "sim_clock": CLOCK}, offset=i)
            ) is True
        assert duck.calls == 0, "a record was written before the batch was flushed"

        assert svc.consumer.flush_writes() is True
        assert duck.calls == 1                       # ONE statement for two hundred records
        assert duck.rows == 200
        assert duck.kpi_row_count("R") == 200
        offsets = dict(((t, p), o) for t, p, o in svc.consumer.safe_offsets())
        assert offsets[(topic, 0)] == 200            # …and every one of them is committable
    finally:
        svc.stop(timeout=1.0)


def test_a_failed_batch_rolls_its_offsets_back_and_keeps_nothing_in_memory(tmp_path):
    """Batching must not become a retry queue that grows for as long as the store is down.

    The durability contract is unchanged from the per-record write: a flush that raises leaves
    the commit points where the last flush that RETURNED left them, so the whole batch is
    replayed by the broker on the next start, and the rows themselves are dropped rather than
    accumulated. Committing past them would be the silent loss this package exists to remove.
    """
    duck = DuckStore(str(tmp_path / "dp.duckdb"))
    svc, _ = _svc(tmp_path, duck=duck)
    try:
        topic = svc.consumer.broker_topics()[svc.consumer.LOGICAL_TOPICS.index("kpi")]
        for i in range(3):
            svc.consumer.handle_message(
                _Msg(topic, "R", {"metric": f"m{i}", "value": 1.0, "sim_clock": CLOCK}, offset=i)
            )
        assert svc.consumer.flush_writes() is True
        assert duck.kpi_row_count("R") == 3

        def boom(rows):
            raise DuckStoreError("IO Error: disk full")

        duck.write_kpi_events = boom
        for i in range(3, 9):
            svc.consumer.handle_message(
                _Msg(topic, "R", {"metric": f"m{i}", "value": 1.0, "sim_clock": CLOCK}, offset=i)
            )
        assert svc.consumer.flush_writes() is False
        assert svc._kpi_buffer == [], "rows retained: that is a queue, not a batch"
        offsets = dict(((t, p), o) for t, p, o in svc.consumer.safe_offsets())
        assert offsets[(topic, 0)] == 3, "committed past rows nothing wrote"
        assert svc.consumer.stats()["commit_lag"][f"{topic}:0"] == 6
        assert "ingest_discarding" in svc.health_payload()["degraded"]
    finally:
        svc.stop(timeout=1.0)
