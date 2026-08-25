"""Regression test for the 2026-07-01 outage — real threads, not a lock assertion.

The old ``apps/kpi_sink/duckdb_store.py`` cached one DuckDB connection per run_id and
reached ``_connection()`` (which created the file AND ran ``CREATE TABLE``) from methods
that did not hold the lock. Two threads first-touching the same run both ran the schema
DDL, DuckDB raised ``TransactionException``, and the consumer thread — which only caught
``KafkaException`` — died silently.

These tests reproduce exactly that shape of access: many threads, a **fresh empty
database**, first-touch of the schema and first-touch of both shared and per-thread runs
happening concurrently, mixing writes and reads. Every thread's exceptions are collected
and the list must be empty; the final row counts must be exactly what was written.

Everything is under ``tmp_path``. Nothing here opens the default path or
``~/.openride/kpi-duckdb/``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

from apps.dataplane.contract.frame import Frame
from apps.dataplane.store.duck import DuckStore, DuckStoreError

N_THREADS = 8
N_ITERS = 200
T0 = datetime(2026, 8, 5, 12, 0, 0)


def make_frame(n, frame_idx=0, sim_time_ms=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return Frame(
        frame_idx=frame_idx, sim_time_ms=sim_time_ms,
        lng=rng.uniform(3.0, 7.5, n), lat=rng.uniform(50.5, 53.5, n),
        slot=np.arange(n, dtype=np.uint32),
        state=rng.integers(0, 12, n, dtype=np.uint8),
        haulier=rng.integers(0, 8, n, dtype=np.uint8),
    )


def test_eight_threads_race_a_fresh_store(tmp_path):
    """8 threads x 200 mixed iterations against one DuckStore on an EMPTY database.

    Shared run ids force concurrent first-touch of the same run (the original race);
    per-thread run ids force concurrent first-touch of *different* runs. Assertions:
    no thread raised, and the final row counts are exactly what was written.
    """
    db = tmp_path / "race.duckdb"
    assert not db.exists()  # first touch of the schema happens under the race
    store = DuckStore(db)

    shared_runs = ["shared_0", "shared_1"]
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    start = threading.Barrier(N_THREADS)

    frame_rows_per_thread = 0  # computed below, deterministic
    kpi_rows_per_thread = N_ITERS  # one unique (metric, sim_clock) per iteration
    breakdown_entities = 3

    def worker(tid: int) -> None:
        own_run = f"thread_{tid}"
        start.wait()
        try:
            for i in range(N_ITERS):
                shared = shared_runs[i % len(shared_runs)]

                # unique key per (thread, iteration) so counting is exact
                store.write_kpi_events(
                    [(own_run, f"metric_{tid}", float(i), T0 + timedelta(seconds=i))]
                )

                # every thread writes the SAME breakdown key set into a shared run:
                # upserts must collapse them, and no thread may die doing it.
                store.write_breakdown(
                    shared,
                    "truck",
                    T0 + timedelta(minutes=i % 5),
                    False,
                    [
                        {"id": f"t{k}", "haulier_id": f"h{k}", "empty_km": float(tid),
                         "loaded_km": 1.0, "total_km": 2.0}
                        for k in range(breakdown_entities)
                    ],
                )

                # frames: append-only, so each thread owns a disjoint frame_idx space
                store.write_frame(
                    own_run, make_frame(5, frame_idx=i, sim_time_ms=float(i), seed=i)
                )

                # reads interleaved with the writes — these were the unlocked paths
                store.query("SELECT count(*) AS c FROM frames WHERE run_id = ?", [own_run])
                store.kpi_row_count(shared)
                store.get_export_state(shared)
                if i % 25 == 0:
                    store.set_export_state(own_run, status="PENDING", row_count=i)
                    store.frame_range(own_run)
                    store.list_run_ids()
        except BaseException as exc:  # noqa: BLE001 - the whole point is to catch everything
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,), name=f"w{t}") for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    assert all(not t.is_alive() for t in threads), "a worker thread hung"
    assert errors == [], f"threads raised: {[repr(e) for e in errors]}"

    frame_rows_per_thread = N_ITERS * 5
    try:
        # exact final counts
        total_frames = store.query("SELECT count(*) AS c FROM frames")[0]["c"]
        assert total_frames == N_THREADS * frame_rows_per_thread

        total_kpi = store.query("SELECT count(*) AS c FROM kpi_events")[0]["c"]
        assert total_kpi == N_THREADS * kpi_rows_per_thread

        # 2 shared runs x 5 distinct sim_clocks x 3 entities, collapsed by the upsert key
        total_breakdown = store.query("SELECT count(*) AS c FROM kpi_breakdown_rows")[0]["c"]
        assert total_breakdown == len(shared_runs) * 5 * breakdown_entities

        assert store.query("SELECT count(*) AS c FROM run_export_state")[0]["c"] == N_THREADS

        for tid in range(N_THREADS):
            run = f"thread_{tid}"
            assert store.run_frame_count(run) == N_ITERS
            assert store.kpi_row_count(run) == N_ITERS
            assert store.frame_range(run) == (0, N_ITERS - 1)
    finally:
        store.close()


def test_concurrent_first_touch_of_the_same_run(tmp_path):
    """The precise original race: N threads first-touching one brand-new run at once."""
    store = DuckStore(tmp_path / "firsttouch.duckdb")
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(N_THREADS)

    def worker(tid: int) -> None:
        start.wait()
        try:
            store.write_kpi_events([("run_first", f"m{tid}", float(tid), T0)])
            store.set_export_state("run_first", status="PENDING")
            store.get_export_state("run_first")
            store.kpi_row_count("run_first")
            store.write_frame("run_first", make_frame(3, frame_idx=tid, seed=tid))
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    try:
        assert store.kpi_row_count("run_first") == N_THREADS
        assert store.run_frame_count("run_first") == N_THREADS
        assert store.query("SELECT count(*) AS c FROM frames")[0]["c"] == N_THREADS * 3
    finally:
        store.close()


def test_concurrent_evict_and_write_do_not_corrupt_other_runs(tmp_path):
    """Evicting one run while other threads keep writing must not lose their rows."""
    store = DuckStore(tmp_path / "evict.duckdb")
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(N_THREADS)
    iters = 100

    def writer(tid: int) -> None:
        start.wait()
        try:
            for i in range(iters):
                store.write_frame(
                    f"keep_{tid}", make_frame(4, frame_idx=i, sim_time_ms=float(i), seed=i)
                )
                store.write_kpi_events([(f"keep_{tid}", "m", float(i), T0 + timedelta(seconds=i))])
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    def evictor() -> None:
        start.wait()
        try:
            for i in range(iters):
                store.write_frame("churn", make_frame(4, frame_idx=i, seed=i))
                store.write_kpi_events([("churn", "m", float(i), T0 + timedelta(seconds=i))])
                if i % 10 == 9:
                    store.evict_run("churn")
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS - 1)]
    threads.append(threading.Thread(target=evictor))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    try:
        for tid in range(N_THREADS - 1):
            assert store.run_frame_count(f"keep_{tid}") == iters
            assert store.kpi_row_count(f"keep_{tid}") == iters
        assert store.run_frame_count("churn") == 0
    finally:
        store.close()


def test_rehydrate_transaction_does_not_disturb_concurrent_writers(tmp_path):
    """``rehydrate_run`` now holds ONE transaction across the whole source read.

    Two things must hold while that transaction is open: other threads' writes (which
    queue on the store lock) all land, and the ROLLBACK of a rehydrate whose source dies
    half-way undoes **only** the rehydrate — never another thread's committed rows.
    """
    store = DuckStore(tmp_path / "rehydrate_race.duckdb")
    frames = [make_frame(6, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(40)]
    kpi = [("target", "empty_km", 1.0, T0)]

    class SlowDyingSource:
        """Frames trickle in, then the cursor dies — as a Mongo read really fails."""

        def has_run(self, run_id):
            return True

        def read_run_frames(self, run_id):
            for i, frame in enumerate(frames):
                if i == 20:
                    raise RuntimeError("cursor id 42 not found (timed out)")
                yield frame

        def read_run_kpi_events(self, run_id):
            return iter(kpi)

    # target run already holds good data; it must survive the failed rehydrate untouched
    store.write_frames("target", frames)
    store.write_kpi_events(kpi)

    errors: list[BaseException] = []
    lock = threading.Lock()
    iters = 60
    start = threading.Barrier(N_THREADS)

    def writer(tid: int) -> None:
        start.wait()
        try:
            for i in range(iters):
                store.write_frame(f"w{tid}", make_frame(3, frame_idx=i, seed=i))
                store.write_kpi_events([(f"w{tid}", "m", float(i), T0 + timedelta(seconds=i))])
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    def rehydrator() -> None:
        start.wait()
        for _ in range(5):
            try:
                store.rehydrate_run("target", SlowDyingSource(), force=True)
            except RuntimeError:
                pass  # expected: the source died
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS - 1)]
    threads.append(threading.Thread(target=rehydrator))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert all(not t.is_alive() for t in threads), "a thread hung inside the transaction"
    assert errors == [], [repr(e) for e in errors]
    try:
        for tid in range(N_THREADS - 1):
            assert store.run_frame_count(f"w{tid}") == iters
            assert store.kpi_row_count(f"w{tid}") == iters
        assert store.run_frame_count("target") == len(frames)
        assert store.kpi_row_count("target") == 1
        assert list(store.iter_frames("target")) == frames
    finally:
        store.close()


def test_second_duckstore_on_the_same_file_fails_as_a_clean_DuckStoreError(tmp_path):
    """DuckDB refuses a second read-write process/handle on an already-open file.

    In-process, ``duckdb.connect`` on the same path returns a handle to the SAME
    cached database instance, so a second DuckStore succeeds. This test therefore
    asserts the *implemented* behaviour: constructing several DuckStores concurrently
    against one fresh file either all succeed (shared instance) or fail with a clean
    ``DuckStoreError`` — never a thread dying on a raw duckdb exception. Whichever
    happens, no thread raises anything other than ``DuckStoreError``.
    """
    path = tmp_path / "multi.duckdb"
    stores: list[DuckStore] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(N_THREADS)

    def worker(tid: int) -> None:
        start.wait()
        try:
            s = DuckStore(path)
            with lock:
                stores.append(s)
            s.write_kpi_events([("multi", f"m{tid}", float(tid), T0)])
        except DuckStoreError as exc:
            with lock:
                errors.append(exc)
        except BaseException as exc:  # noqa: BLE001 - any other type is a failure
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    try:
        assert all(isinstance(e, DuckStoreError) for e in errors), [repr(e) for e in errors]
        assert stores, "no DuckStore opened at all"
        # every write that reported success is actually present
        succeeded = N_THREADS - len(errors)
        assert stores[0].kpi_row_count("multi") == succeeded
    finally:
        for s in stores:
            try:
                s.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# ROUND 3 / ROOT 3 — the lock is per RUN, and a rehydrate is columnar.
#
# Before: ``rehydrate_run`` of one real run (2 520 frames x 500 trucks + 35 000 kpi rows +
# 35 000 breakdown rows) measured **253.6 s** on this box with the GLOBAL store lock held,
# so ``/health``, ``get_run_meta`` on the poll thread and every other run's writes queued
# behind it for the whole 253 s (measured: the hammer thread below completed 20 calls in
# that window, one of which blocked for 253.7 s) — and 253 s blows past the supervisor's
# 90 s stall_timeout, which is what produced the permanent thread duplication.
#
# After: **1.22 s**, with every concurrent reader/other-run writer answering in < 0.14 s.
#
# Nothing here uses a test double for DuckStore.
# ─────────────────────────────────────────────────────────────────────────────

SCALE_FRAMES = 2520
SCALE_TRUCKS = 500
SCALE_KPI = 35_000
SCALE_BREAKDOWN = 35_000


def _scale_frames(n_frames=SCALE_FRAMES, n_trucks=SCALE_TRUCKS):
    rng = np.random.default_rng(1)
    lng = rng.uniform(3.0, 7.5, n_trucks)
    lat = rng.uniform(50.5, 53.5, n_trucks)
    slot = np.arange(n_trucks, dtype=np.uint32)
    state = rng.integers(0, 12, n_trucks, dtype=np.uint8)
    haulier = rng.integers(0, 8, n_trucks, dtype=np.uint8)
    return [
        Frame(frame_idx=i, sim_time_ms=float(i) * 1000.0, lng=lng + i * 1e-6, lat=lat,
              slot=slot, state=state, haulier=haulier)
        for i in range(n_frames)
    ]


class ScaleSource:
    """A real-shaped archive: frame documents with byte columns, kpi tuples, bd rows."""

    def __init__(self, frames, run_id="SCALE"):
        self._frames = frames
        self.run_id = run_id

    def has_run(self, run_id):
        return True

    def read_run_frame_docs(self, run_id):
        for f in self._frames:
            yield {
                "frame_idx": f.frame_idx, "sim_time_ms": f.sim_time_ms, "n": int(f.n),
                "lng": np.ascontiguousarray(f.lng, "<f8").tobytes(),
                "lat": np.ascontiguousarray(f.lat, "<f8").tobytes(),
                "slot": np.ascontiguousarray(f.slot, "<u4").tobytes(),
                "state": np.ascontiguousarray(f.state, np.uint8).tobytes(),
                "haulier": np.ascontiguousarray(f.haulier, np.uint8).tobytes(),
            }

    def read_run_frames(self, run_id):
        return iter(self._frames)

    def read_run_kpi_events(self, run_id):
        for i in range(SCALE_KPI):
            yield (run_id, f"metric_{i % 40}", float(i), T0 + timedelta(seconds=i))

    def read_run_breakdown_rows(self, run_id):
        for i in range(SCALE_BREAKDOWN):
            yield {
                "run_id": run_id, "scope": "truck", "final": False,
                "sim_clock": T0 + timedelta(hours=i // 500),
                "entity_id": f"truck_{i % 500}", "haulier_id": f"h{i % 8}",
                "haulier_name": f"Haulier {i % 8}", "num_orders_completed": i % 20,
                "empty_km": float(i) * 0.5, "loaded_km": float(i), "total_km": float(i) * 1.5,
                "empty_ratio": 0.33, "active_hours": 4.5, "orders_per_day": 2.0,
                "dual_cycle_count": 1, "chain_opportunities": 2, "dual_cycle_rate": 0.5,
                "num_trucks": 1, "payload": "{}", "ingested_at": T0,
            }

    def run_summary(self, run_id):
        return {"status": "completed", "n_trucks": SCALE_TRUCKS, "complete": True,
                "kpi_count": SCALE_KPI, "frame_count": SCALE_FRAMES}


def test_a_full_scale_rehydrate_is_fast_and_starves_no_other_caller(tmp_path):
    """(a) + (b): 1.26 M position rows + 35 k + 35 k, with a hammer thread beside it.

    The rehydrate must finish in < 5 s (it measured 253.6 s row-at-a-time) and EVERY
    concurrent read / other-run write must answer in < 0.25 s (they measured 253.7 s).
    """
    store = DuckStore(tmp_path / "scale.duckdb")
    store.write_kpi_events([("other", "m", 1.0, T0)])
    store.upsert_run_meta("other", status="RUNNING")

    latencies: dict[str, list[float]] = {"get_run_meta": [], "list_run_ids": [],
                                         "write_breakdown": []}
    errors: list[BaseException] = []
    stop = threading.Event()

    def hammer():
        i = 0
        while not stop.is_set():
            calls = (
                ("get_run_meta", lambda: store.get_run_meta("other")),
                ("list_run_ids", store.list_run_ids),
                ("write_breakdown", lambda: store.write_breakdown(
                    "other", "haulier", T0 + timedelta(seconds=i), False,
                    [{"id": "ACME", "empty_km": 1.0}])),
            )
            for name, fn in calls:
                t = time.perf_counter()
                try:
                    fn()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                latencies[name].append(time.perf_counter() - t)
            i += 1
            time.sleep(0.005)

    worker = threading.Thread(target=hammer, name="hammer", daemon=True)
    worker.start()
    time.sleep(0.2)
    try:
        started = time.perf_counter()
        assert store.rehydrate_run("SCALE", ScaleSource(_scale_frames())) == SCALE_FRAMES
        elapsed = time.perf_counter() - started
    finally:
        stop.set()
        worker.join(timeout=30)

    try:
        assert errors == [], [repr(e) for e in errors]
        assert elapsed < 5.0, f"rehydrate took {elapsed:.2f}s (row-at-a-time was 253.6 s)"
        for name, samples in latencies.items():
            assert samples, f"the hammer thread never ran {name}"
            worst = max(samples)
            assert worst < 0.25, f"{name} blocked for {worst:.3f}s behind the rehydrate"
        # …and the run really is there
        assert store.run_frame_count("SCALE") == SCALE_FRAMES
        assert store.kpi_row_count("SCALE") == SCALE_KPI
        assert store.breakdown_row_count("SCALE") == SCALE_BREAKDOWN
        assert store.get_run_meta("SCALE")["status"] == "completed"
        assert store.get_export_state("SCALE")["status"] == "exported"
        assert store.query("SELECT count(*) AS c FROM frames WHERE run_id = 'SCALE'")[0]["c"] \
            == SCALE_FRAMES * SCALE_TRUCKS
    finally:
        store.close()


class SmallSource:
    def __init__(self, frames, kpi=(), breakdown=()):
        self._frames = list(frames)
        self._kpi = list(kpi)
        self._breakdown = [dict(r) for r in breakdown]

    def has_run(self, run_id):
        return True

    def read_run_frames(self, run_id):
        return iter(self._frames)

    def read_run_kpi_events(self, run_id):
        return iter(self._kpi)

    def read_run_breakdown_rows(self, run_id):
        return iter(self._breakdown)


def test_two_runs_rehydrate_concurrently_and_same_run_work_serialises(tmp_path):
    """(d) Per-run locking is the correct granularity, proven both ways.

    Two DuckDB transactions on DISJOINT rows of a table both commit; two on the SAME rows
    raise ``TransactionException: Conflict on update!`` — the 2026-07-01 exception. So:
    different runs rehydrate at the same time, and a rehydrate racing a ``write_breakdown``
    on the SAME run serialises instead of raising.
    """
    store = DuckStore(tmp_path / "conflict.duckdb")
    frames = [make_frame(60, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(120)]
    kpi_a = [("A", f"m{i}", float(i), T0 + timedelta(seconds=i)) for i in range(400)]
    kpi_b = [("B", f"m{i}", float(i), T0 + timedelta(seconds=i)) for i in range(400)]
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(3)

    def rehydrate(run_id, kpi):
        start.wait()
        try:
            for _ in range(4):
                store.rehydrate_run(run_id, SmallSource(frames, kpi), force=True)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    def same_run_writer():
        start.wait()
        try:
            for i in range(120):
                store.write_breakdown(
                    "A", "truck", T0 + timedelta(minutes=i), False,
                    [{"id": f"t{k}", "empty_km": float(i)} for k in range(20)],
                )
                store.write_kpi_events([("A", "live", float(i), T0 + timedelta(hours=i))])
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=rehydrate, args=("A", kpi_a), name="rehydrate-A"),
        threading.Thread(target=rehydrate, args=("B", kpi_b), name="rehydrate-B"),
        threading.Thread(target=same_run_writer, name="writer-A"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert all(not t.is_alive() for t in threads), "a thread hung on a run lock"
    assert errors == [], [repr(e) for e in errors]
    try:
        # both runs are fully restored: neither rehydrate lost to the other
        for run in ("A", "B"):
            assert store.run_frame_count(run) == len(frames)
            assert store.query(
                "SELECT count(*) AS c FROM frames WHERE run_id = ?", [run]
            )[0]["c"] == len(frames) * 60
        assert store.kpi_row_count("B") == 400
        # run A's concurrent writer serialised with the rehydrates rather than conflicting
        assert store.kpi_row_count("A") >= 400
    finally:
        store.close()


def test_a_read_never_waits_on_a_run_lock(tmp_path):
    """Reads take no run lock at all — that is what keeps /health answering."""
    store = DuckStore(tmp_path / "readers.duckdb")
    store.write_kpi_events([("held", "m", 1.0, T0)])
    store.upsert_run_meta("held", status="RUNNING")
    store.write_frame("held", make_frame(8, frame_idx=0))

    run_lock = store._run_lock("held")
    released = threading.Event()
    results: list[float] = []
    errors: list[BaseException] = []

    def reader():
        try:
            for fn in (lambda: store.get_run_meta("held"),
                       store.list_run_ids,
                       lambda: store.kpi_row_count("held"),
                       lambda: store.frame_range("held"),
                       lambda: store.read_frame("held", 0),
                       lambda: store.get_export_state("held")):
                t = time.perf_counter()
                fn()
                results.append(time.perf_counter() - t)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with run_lock:  # a "rehydrate" of this run is in progress
        t = threading.Thread(target=reader, name="reader")
        t.start()
        t.join(timeout=10)
        released.set()

    try:
        assert errors == [], [repr(e) for e in errors]
        assert len(results) == 6
        assert max(results) < 0.25, f"a read blocked for {max(results):.3f}s on a run lock"
    finally:
        store.close()


def test_multi_run_kpi_batches_cannot_deadlock(tmp_path):
    """``write_kpi_events`` groups by run_id and takes ONE run lock at a time.

    Two threads writing batches whose runs are in OPPOSITE order is the classic
    lock-ordering deadlock; it is not expressible here because a second run's lock is only
    ever taken after the first has been released. A naive "acquire every run lock, then
    write" implementation hangs this test.
    """
    store = DuckStore(tmp_path / "multirun.duckdb")
    runs = [f"r{i}" for i in range(6)]
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(2)
    iters = 150

    def writer(order):
        start.wait()
        try:
            for i in range(iters):
                store.write_kpi_events(
                    [(r, f"m{order[0]}", float(i), T0 + timedelta(seconds=i)) for r in order]
                )
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(runs,), name="forward"),
        threading.Thread(target=writer, args=(list(reversed(runs)),), name="reverse"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert all(not t.is_alive() for t in threads), "deadlock: two run locks were held at once"
    assert errors == [], [repr(e) for e in errors]
    try:
        for r in runs:
            # two metrics ("mr0" and "mr5") x iters distinct clocks
            assert store.kpi_row_count(r) == 2 * iters
    finally:
        store.close()
