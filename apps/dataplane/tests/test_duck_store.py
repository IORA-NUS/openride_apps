"""Functional tests for apps.dataplane.store.duck.DuckStore.

Every test opens the database at a ``tmp_path``-derived location. Nothing here ever
opens the default path or anything under ``~/.openride/kpi-duckdb/``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from apps.dataplane.contract.frame import KIND_KEYFRAME, Frame
from apps.dataplane.store.duck import DuckStore, DuckStoreError, RunNotInStore


def make_frame(n, frame_idx=0, sim_time_ms=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return Frame(
        frame_idx=frame_idx, sim_time_ms=sim_time_ms,
        lng=rng.uniform(3.0, 7.5, n), lat=rng.uniform(50.5, 53.5, n),
        slot=np.arange(n, dtype=np.uint32),
        state=rng.integers(0, 12, n, dtype=np.uint8),
        haulier=rng.integers(0, 8, n, dtype=np.uint8),
    )


@pytest.fixture()
def store(tmp_path):
    s = DuckStore(tmp_path / "dataplane.duckdb")
    try:
        yield s
    finally:
        s.close()


T0 = datetime(2026, 8, 5, 12, 0, 0)


def _entities(n=3, empty_km=1.0):
    return [
        {
            "id": f"truck_{i}", "haulier_id": f"h{i % 2}", "haulier_name": f"Haulier {i % 2}",
            "num_orders_completed": i, "empty_km": empty_km + i, "loaded_km": 10.0 + i,
            "total_km": 11.0 + 2 * i, "empty_ratio": 0.25, "active_hours": 4.5,
            "orders_per_day": 2.0, "dual_cycle_count": 1, "chain_opportunities": 2,
            "dual_cycle_rate": 0.5, "num_trucks": 1,
        }
        for i in range(n)
    ]


# ── schema ───────────────────────────────────────────────────────────────────


def test_all_tables_created(store):
    names = {
        r["table_name"]
        for r in store.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        )
    }
    assert {
        "kpi_events",
        "kpi_breakdown_rows",
        "frames",
        "run_meta",
        "run_export_state",
    } <= names


def test_frames_has_no_primary_key_and_no_index(store):
    cons = store.query(
        "SELECT constraint_type FROM duckdb_constraints() WHERE table_name = 'frames'"
    )
    assert all(c["constraint_type"] != "PRIMARY KEY" for c in cons)
    assert store.query("SELECT * FROM duckdb_indexes() WHERE table_name = 'frames'") == []


def test_schema_ddl_runs_only_once_even_when_reopened(tmp_path):
    p = tmp_path / "reopen.duckdb"
    s1 = DuckStore(p)
    s1.write_kpi_events([("r", "m", 1.0, T0)])
    s1.close()
    s2 = DuckStore(p)
    try:
        assert s2.kpi_row_count("r") == 1
    finally:
        s2.close()


# ── kpi_events ───────────────────────────────────────────────────────────────


def test_kpi_round_trip(store):
    rows = [("runA", "empty_km", 12.5, T0), ("runA", "loaded_km", 30.0, T0 + timedelta(minutes=1))]
    assert store.write_kpi_events(rows) == 2
    got = store.read_kpi_events("runA")
    assert got == [
        ("runA", "empty_km", 12.5, T0),
        ("runA", "loaded_km", 30.0, T0 + timedelta(minutes=1)),
    ]
    assert store.kpi_row_count("runA") == 2


def test_kpi_upsert_is_idempotent_and_last_write_wins(store):
    store.write_kpi_events([("runA", "empty_km", 1.0, T0)])
    store.write_kpi_events([("runA", "empty_km", 1.0, T0)])
    assert store.kpi_row_count("runA") == 1
    store.write_kpi_events([("runA", "empty_km", 99.0, T0)])
    assert store.kpi_row_count("runA") == 1
    assert store.read_kpi_events("runA")[0][2] == 99.0


def test_write_kpi_events_empty(store):
    assert store.write_kpi_events([]) == 0


# ── kpi_breakdown_rows ───────────────────────────────────────────────────────


def test_breakdown_round_trip(store):
    n = store.write_breakdown("runA", "truck", T0, False, _entities(3))
    assert n == 3
    rows = store.query("SELECT * FROM kpi_breakdown_rows WHERE run_id = ? ORDER BY entity_id", ["runA"])
    assert [r["entity_id"] for r in rows] == ["truck_0", "truck_1", "truck_2"]
    assert rows[0]["haulier_name"] == "Haulier 0"
    assert rows[1]["empty_km"] == 2.0
    assert json.loads(rows[0]["payload"])["id"] == "truck_0"
    assert rows[0]["final"] is False


def test_breakdown_upsert_is_idempotent_and_last_write_wins(store):
    store.write_breakdown("runA", "truck", T0, False, _entities(3, empty_km=1.0))
    store.write_breakdown("runA", "truck", T0, False, _entities(3, empty_km=1.0))
    assert len(store.query("SELECT * FROM kpi_breakdown_rows WHERE run_id = ?", ["runA"])) == 3
    store.write_breakdown("runA", "truck", T0, True, _entities(3, empty_km=100.0))
    rows = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? ORDER BY entity_id", ["runA"]
    )
    assert len(rows) == 3
    assert rows[0]["empty_km"] == 100.0
    assert rows[0]["final"] is True


def test_breakdown_rejects_unknown_scope_and_empty(store):
    # "lane" became a STORED scope when /lanes was added; "wormhole" stands for the
    # genuinely unknown scope this test is about.
    assert store.write_breakdown("runA", "wormhole", T0, False, _entities(2)) == 0
    assert store.write_breakdown("runA", "truck", T0, False, []) == 0
    # entities without an id are skipped
    assert store.write_breakdown("runA", "truck", T0, False, [{"empty_km": 1}]) == 0


def test_the_planner_scope_is_stored_alongside_truck_and_haulier(store):
    """One row per cooperation component — the project's primary research lens.

    ``scope`` is already part of the primary key, so holding this scope costs nothing, and
    the producer emits planner rows in the haulier row shape whenever a cooperation structure
    is active. They used to be dropped with their offsets committed and no health signal.
    """
    entities = [{"id": "planner:ACME+BOLT", "empty_km": 12.5, "num_trucks": 7}]
    assert store.write_breakdown("runP", "planner", T0, False, entities) == 1
    rows = store.query("SELECT * FROM kpi_breakdown_rows WHERE run_id = ?", ["runP"])
    assert [r["scope"] for r in rows] == ["planner"]
    assert rows[0]["entity_id"] == "planner:ACME+BOLT"
    assert rows[0]["empty_km"] == 12.5
    assert rows[0]["num_trucks"] == 7


def test_breakdown_coerces_non_numeric(store):
    ents = [{"id": "t1", "empty_km": "not-a-number", "loaded_km": None, "total_km": "3.5"}]
    assert store.write_breakdown("runA", "haulier", T0, False, ents) == 1
    row = store.query("SELECT * FROM kpi_breakdown_rows WHERE run_id = ?", ["runA"])[0]
    assert row["empty_km"] is None
    assert row["loaded_km"] is None
    assert row["total_km"] == 3.5


# ── frames ───────────────────────────────────────────────────────────────────


def test_write_frame_and_read_frame_round_trip(store):
    f = make_frame(37, frame_idx=5, sim_time_ms=1234.5, seed=7)
    assert store.write_frame("runA", f) == 37
    back = store.read_frame("runA", 5)
    assert back is not None
    assert back == f  # Frame.__eq__ compares all five arrays + header fields


def test_read_frame_missing_returns_none(store):
    store.write_frame("runA", make_frame(4, frame_idx=0))
    assert store.read_frame("runA", 99) is None
    assert store.read_frame("nope", 0) is None


def test_write_frames_and_frame_range_and_count(store):
    frames = [make_frame(10, frame_idx=i, sim_time_ms=float(i) * 100, seed=i) for i in range(6)]
    assert store.write_frames("runA", frames) == 60
    assert store.frame_range("runA") == (0, 5)
    assert store.run_frame_count("runA") == 6
    assert store.frame_range("missing") is None
    assert store.run_frame_count("missing") == 0


def test_read_frames_window_is_inclusive_and_returns_numpy(store):
    frames = [make_frame(8, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(5)]
    store.write_frames("runA", frames)
    cols = store.read_frames("runA", 1, 3)
    assert set(cols) == {"frame_idx", "sim_time_ms", "slot", "lng", "lat", "state", "haulier"}
    assert isinstance(cols["lng"], np.ndarray)
    assert cols["lng"].shape == (24,)
    assert sorted(set(np.asarray(cols["frame_idx"]).tolist())) == [1, 2, 3]


def test_zero_length_frame_writes_nothing(store):
    assert store.write_frame("runA", make_frame(0, frame_idx=0)) == 0
    assert store.run_frame_count("runA") == 0


def test_iter_frames_is_ordered_and_complete(store):
    frames = [make_frame(6, frame_idx=i, sim_time_ms=float(i) * 10, seed=i) for i in range(12)]
    store.write_frames("runA", frames)
    store.write_frame("runB", make_frame(3, frame_idx=0, seed=99))
    out = list(store.iter_frames("runA"))
    assert [f.frame_idx for f in out] == list(range(12))
    assert out == frames
    assert list(store.iter_frames("missing")) == []


def test_iter_frames_across_multiple_windows(store, monkeypatch):
    import apps.dataplane.store.duck as duck_mod

    monkeypatch.setattr(duck_mod, "_ITER_WINDOW", 3)
    frames = [make_frame(4, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(11)]
    store.write_frames("runA", frames)
    assert list(store.iter_frames("runA")) == frames


def test_iter_frames_does_not_hold_the_lock_while_yielding(store):
    """A consumer of iter_frames must be able to write to the store mid-iteration."""
    store.write_frames("runA", [make_frame(4, frame_idx=i, seed=i) for i in range(4)])
    seen = 0
    for _frame in store.iter_frames("runA"):
        # If iter_frames held the lock across the yield this would deadlock in the
        # threaded case; here it at least proves reentrant usage is safe.
        store.write_kpi_events([("runB", f"m{seen}", float(seen), T0)])
        seen += 1
    assert seen == 4
    assert store.kpi_row_count("runB") == 4


def test_frames_with_gaps_in_frame_idx(store):
    frames = [make_frame(5, frame_idx=i, sim_time_ms=float(i), seed=i) for i in (0, 7, 40)]
    store.write_frames("runA", frames)
    assert [f.frame_idx for f in store.iter_frames("runA")] == [0, 7, 40]
    assert store.frame_range("runA") == (0, 40)


# ── run_meta / export state ──────────────────────────────────────────────────


def test_run_meta_upsert(store):
    store.upsert_run_meta("runA", status="RUNNING", n_trucks=500, source="live")
    meta = store.get_run_meta("runA")
    assert meta["status"] == "RUNNING"
    assert meta["n_trucks"] == 500
    assert meta["source"] == "live"
    store.upsert_run_meta(
        "runA", status="COMPLETED", haulier_codes=json.dumps({"h1": 1}), slot_map=json.dumps({"t": 0})
    )
    meta = store.get_run_meta("runA")
    assert meta["status"] == "COMPLETED"
    assert meta["n_trucks"] == 500  # untouched field survives
    assert json.loads(meta["haulier_codes"]) == {"h1": 1}
    assert store.get_run_meta("nope") is None


def test_run_meta_rejects_unknown_column(store):
    with pytest.raises(DuckStoreError):
        store.upsert_run_meta("runA", bogus_column=1)


def test_export_state_round_trip(store):
    assert store.get_export_state("runA") is None
    store.set_export_state("runA", status="PENDING")
    st = store.get_export_state("runA")
    assert st["status"] == "PENDING"
    assert st["row_count"] is None
    store.set_export_state("runA", status="EXPORTED", row_count=935, frame_count=2520)
    st = store.get_export_state("runA")
    assert (st["status"], st["row_count"], st["frame_count"]) == ("EXPORTED", 935, 2520)
    store.set_export_state("runA", status="FAILED", error="boom")
    assert store.get_export_state("runA")["error"] == "boom"
    assert len(store.query("SELECT * FROM run_export_state")) == 1


def test_list_run_ids(store):
    assert store.list_run_ids() == []
    store.write_kpi_events([("runK", "m", 1.0, T0)])
    store.write_frame("runF", make_frame(2))
    store.write_breakdown("runB", "truck", T0, False, _entities(1))
    store.upsert_run_meta("runM", status="X")
    assert store.list_run_ids() == ["runB", "runF", "runK", "runM"]


def test_query_rejects_bad_sql_cleanly(store):
    with pytest.raises(DuckStoreError):
        store.query("SELECT * FROM table_that_does_not_exist")


# ── rehydrate / evict ────────────────────────────────────────────────────────


class FakeFrameSource:
    """Hand-written stand-in for MongoArchive (the FrameSource protocol)."""

    def __init__(self, frames_by_run, kpi_by_run=None):
        self._frames = frames_by_run
        self._kpi = kpi_by_run or {}
        self.frame_reads = 0

    def has_run(self, run_id):
        return run_id in self._frames

    def read_run_frames(self, run_id):
        self.frame_reads += 1
        return iter(self._frames.get(run_id, []))

    def read_run_kpi_events(self, run_id):
        return iter(self._kpi.get(run_id, []))


def test_rehydrate_run_populates_and_is_a_noop_second_time(store):
    frames = [make_frame(9, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(5)]
    kpi = [("runA", "empty_km", 4.0, T0)]
    src = FakeFrameSource({"runA": frames}, {"runA": kpi})

    assert store.rehydrate_run("runA", src) == 5
    assert store.run_frame_count("runA") == 5
    assert store.kpi_row_count("runA") == 1
    assert store.get_run_meta("runA")["source"] == "rehydrated"
    assert store.get_run_meta("runA")["max_frame_idx"] == 4
    assert list(store.iter_frames("runA")) == frames

    assert store.rehydrate_run("runA", src) == 0  # no-op
    assert src.frame_reads == 1
    assert store.run_frame_count("runA") == 5


def test_rehydrate_run_force_repopulates_without_duplicating(store):
    frames = [make_frame(9, frame_idx=i, seed=i) for i in range(5)]
    src = FakeFrameSource({"runA": frames}, {"runA": [("runA", "m", 1.0, T0)]})
    store.rehydrate_run("runA", src)

    assert store.rehydrate_run("runA", src, force=True) == 5
    assert store.run_frame_count("runA") == 5
    assert len(store.query("SELECT * FROM frames WHERE run_id = 'runA'")) == 45
    assert store.kpi_row_count("runA") == 1
    assert src.frame_reads == 2


def test_rehydrate_unknown_run_raises(store):
    src = FakeFrameSource({})
    with pytest.raises(RunNotInStore):
        store.rehydrate_run("ghost", src)


def test_evict_run_removes_only_the_target(store):
    store.write_frames("runA", [make_frame(4, frame_idx=i, seed=i) for i in range(3)])
    store.write_kpi_events([("runA", "m", 1.0, T0), ("runA", "m2", 2.0, T0)])
    store.write_breakdown("runA", "truck", T0, False, _entities(2))
    store.upsert_run_meta("runA", status="COMPLETED")

    store.write_frames("runB", [make_frame(4, frame_idx=i, seed=i) for i in range(2)])
    store.write_kpi_events([("runB", "m", 1.0, T0)])
    store.upsert_run_meta("runB", status="COMPLETED")

    deleted = store.evict_run("runA")
    assert deleted == 12 + 2 + 2 + 1

    assert store.run_frame_count("runA") == 0
    assert store.kpi_row_count("runA") == 0
    assert store.get_run_meta("runA") is None
    assert store.query("SELECT * FROM kpi_breakdown_rows WHERE run_id = 'runA'") == []

    assert store.run_frame_count("runB") == 2
    assert store.kpi_row_count("runB") == 1
    assert store.get_run_meta("runB")["status"] == "COMPLETED"

    assert store.evict_run("runA") == 0


def test_evict_then_rehydrate(store):
    frames = [make_frame(6, frame_idx=i, seed=i) for i in range(4)]
    src = FakeFrameSource({"runA": frames}, {"runA": [("runA", "m", 5.0, T0)]})
    store.rehydrate_run("runA", src)
    store.evict_run("runA")
    assert store.run_frame_count("runA") == 0
    assert store.rehydrate_run("runA", src) == 4
    assert list(store.iter_frames("runA")) == frames


def test_checkpoint_is_safe(store):
    store.write_frames("runA", [make_frame(50, frame_idx=i, seed=i) for i in range(20)])
    store.checkpoint()
    assert store.run_frame_count("runA") == 20


def test_close_is_idempotent(tmp_path):
    s = DuckStore(tmp_path / "x.duckdb")
    s.close()
    s.close()
    with pytest.raises(DuckStoreError):
        s.kpi_row_count("runA")


def test_large_frame_round_trip(store):
    f = make_frame(500, frame_idx=2519, sim_time_ms=1.7e12, seed=3)
    store.write_frame("runA", f)
    assert store.read_frame("runA", 2519) == f
    assert store.read_frame("runA", 2519).kind == KIND_KEYFRAME


# ── rehydrate: atomicity, headless runs, breakdown rows ──────────────────────
#
# These cover the round-2 findings against store/duck.py:
#   * the DELETE used to COMMIT before the source was read, so a source that died
#     half-way destroyed the run and the `existing and not force` guard then refused
#     every repair attempt;
#   * a frameless (headless — the OpenRide default) run has kpi rows but no frames, and
#     was therefore treated as absent and silently cleared;
#   * `kpi_breakdown_rows` — ~35 k rows per real run, the Companies views — were never
#     restored at all, and a force rehydrate mixed stale rows with fresh frames.


class RichFrameSource:
    """A source with the optional breakdown reader and run summary (like MongoArchive)."""

    def __init__(self, frames=(), kpi=(), breakdown=(), summary=None):
        self._frames = list(frames)
        self._kpi = list(kpi)
        self._breakdown = [dict(r) for r in breakdown]
        self._summary = summary
        self.frame_reads = 0

    def has_run(self, run_id):
        return True

    def read_run_frames(self, run_id):
        self.frame_reads += 1
        return iter(self._frames)

    def read_run_kpi_events(self, run_id):
        return iter(self._kpi)

    def read_run_breakdown_rows(self, run_id):
        return iter(self._breakdown)

    def run_summary(self, run_id):
        return self._summary


class DyingFrameSource(RichFrameSource):
    """Its frame cursor raises part-way through — a real Mongo cursor timeout."""

    def __init__(self, *args, fail_at=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_at = fail_at

    def read_run_frames(self, run_id):
        self.frame_reads += 1
        for i, frame in enumerate(self._frames):
            if i >= self.fail_at:
                raise RuntimeError("cursor id 42 not found (timed out)")
            yield frame


class BreakdownlessSource:
    """A source that cannot supply breakdown rows: no ``read_run_breakdown_rows`` at all."""

    def __init__(self, frames=(), kpi=()):
        self._frames = list(frames)
        self._kpi = list(kpi)

    def has_run(self, run_id):
        return True

    def read_run_frames(self, run_id):
        return iter(self._frames)

    def read_run_kpi_events(self, run_id):
        return iter(self._kpi)


def _snapshot(store, run_id):
    return {
        "frames": list(store.iter_frames(run_id)),
        "kpi": store.read_kpi_events(run_id),
        "breakdown": store.query(
            "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? ORDER BY entity_id", [run_id]
        ),
        "meta": store.get_run_meta(run_id),
    }


def test_a_source_that_dies_mid_read_leaves_the_run_byte_identical(store):
    frames = [make_frame(20, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(12)]
    store.write_frames("R", frames)
    store.write_kpi_events([("R", "empty_km", 4.0, T0), ("R", "loaded_km", 9.0, T0)])
    store.write_breakdown("R", "truck", T0, False, _entities(3))
    store.upsert_run_meta("R", status="COMPLETED", source="live")
    before = _snapshot(store, "R")

    dying = DyingFrameSource(frames, [("R", "empty_km", 4.0, T0)], fail_at=9)
    with pytest.raises(RuntimeError):
        store.rehydrate_run("R", dying, force=True)

    assert _snapshot(store, "R") == before
    # and the store is still usable afterwards — the transaction really was rolled back
    store.write_kpi_events([("R", "later", 1.0, T0)])
    assert store.kpi_row_count("R") == 3


def test_a_failed_rehydrate_is_repairable_by_a_plain_retry(store, monkeypatch):
    """The partial-commit case: >1 write batch, so the old code left the run half-full."""
    import apps.dataplane.store.duck as duck_mod

    monkeypatch.setattr(duck_mod, "_WRITE_BATCH_ROWS", 100)  # stands in for the 250 k flush
    frames = [make_frame(20, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(60)]
    kpi = [("R", "empty_km", 4.0, T0)]

    with pytest.raises(RuntimeError):
        store.rehydrate_run("R", DyingFrameSource(frames, kpi, fail_at=30))
    assert store.run_frame_count("R") == 0, "a half-read must commit nothing"
    assert store.kpi_row_count("R") == 0
    assert store.get_run_meta("R") is None

    # A PLAIN retry (force defaults to False) must fully restore the run. Before the fix
    # this returned 0 because 30 frames had already been committed by the dead read.
    assert store.rehydrate_run("R", RichFrameSource(frames, kpi)) == 60
    assert list(store.iter_frames("R")) == frames
    assert store.kpi_row_count("R") == 1


def test_a_frameless_kpi_only_run_is_a_noop_not_a_wipe(store):
    """Headless runs (the OpenRide default) publish no truck_loc: kpi rows, zero frames."""
    store.write_kpi_events([("H", f"m{i}", float(i), T0 + timedelta(seconds=i)) for i in range(60)])
    store.write_breakdown("H", "haulier", T0, True, _entities(3))
    store.upsert_run_meta("H", status="COMPLETED", source="live")
    before = _snapshot(store, "H")

    empty = RichFrameSource()  # has_run() True on the summary alone, nothing to give back
    assert store.rehydrate_run("H", empty) == 0
    assert _snapshot(store, "H") == before
    assert store.get_run_meta("H")["source"] == "live"
    assert empty.frame_reads == 0

    # breakdown rows alone are also enough to make it a no-op
    store2_run = "B"
    store.write_breakdown(store2_run, "truck", T0, False, _entities(2))
    assert store.rehydrate_run(store2_run, empty) == 0
    assert store.breakdown_row_count(store2_run) == 2


def test_rehydrate_restores_breakdown_rows_and_the_code_books(store):
    frames = [make_frame(5, frame_idx=i, seed=i) for i in range(3)]
    store.write_frames("RB", frames)
    store.write_kpi_events([("RB", "empty_km", 4.0, T0)])
    store.write_breakdown("RB", "truck", T0, False, _entities(20))
    archived_rows = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? ORDER BY entity_id", ["RB"]
    )
    src = RichFrameSource(
        frames,
        [("RB", "empty_km", 4.0, T0)],
        breakdown=archived_rows,
        summary={"haulier_codes": json.dumps({"ACME": 1}), "slot_map": json.dumps({"t1": 0}),
                 "n_trucks": 7},
    )

    store.evict_run("RB")
    assert store.breakdown_row_count("RB") == 0
    assert store.rehydrate_run("RB", src) == 3

    assert store.breakdown_row_count("RB") == 20
    back = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? ORDER BY entity_id", ["RB"]
    )
    assert {r["entity_id"] for r in back} == {f"truck_{i}" for i in range(20)}
    assert back == archived_rows
    assert back[0]["haulier_name"] == "Haulier 0"
    assert back[0]["sim_clock"] == T0
    meta = store.get_run_meta("RB")
    assert json.loads(meta["haulier_codes"]) == {"ACME": 1}
    assert json.loads(meta["slot_map"]) == {"t1": 0}
    assert meta["n_trucks"] == 7
    assert meta["source"] == "rehydrated"


def test_force_rehydrate_from_a_breakdownless_source_preserves_breakdown_rows(store):
    frames = [make_frame(5, frame_idx=i, seed=i) for i in range(3)]
    store.write_frames("RB", frames)
    store.write_breakdown("RB", "truck", T0, True, _entities(20))
    before = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? ORDER BY entity_id", ["RB"]
    )

    assert store.rehydrate_run("RB", BreakdownlessSource(frames), force=True) == 3
    after = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? ORDER BY entity_id", ["RB"]
    )
    assert after == before, "a source that cannot replace these rows must not delete them"


def test_rehydrate_replaces_stale_breakdown_rows_when_the_source_has_them(store):
    """The other half: a source that CAN supply rows replaces, never mixes."""
    frames = [make_frame(5, frame_idx=0, seed=0)]
    store.write_frames("RB", frames)
    store.write_breakdown("RB", "truck", T0, False, _entities(20))
    fresh = [
        dict(r) for r in store.query(
            "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? AND entity_id IN "
            "('truck_0','truck_1','truck_2')", ["RB"]
        )
    ]
    assert store.rehydrate_run("RB", RichFrameSource(frames, breakdown=fresh), force=True) == 1
    assert store.breakdown_row_count("RB") == 3


def test_a_store_side_write_failure_mid_rehydrate_rolls_the_whole_run_back(tmp_path):
    """Failure injected by SUBCLASSING the real store, not by a duck-typed double."""

    class FailingKpiStore(DuckStore):
        def _insert_kpi_columnar(self, cur, run_id, columns):
            raise DuckStoreError("disk full")

    s = FailingKpiStore(tmp_path / "failwrite.duckdb")
    try:
        frames = [make_frame(6, frame_idx=i, seed=i) for i in range(8)]
        src = RichFrameSource(frames, [("R", "m", 1.0, T0)])
        with pytest.raises(DuckStoreError):
            s.rehydrate_run("R", src)
        assert s.run_frame_count("R") == 0, "frames written before the failure must be undone"
        assert s.get_run_meta("R") is None
        # the connection is still usable — the transaction was properly closed
        s.write_frame("other", make_frame(3, frame_idx=0))
        assert s.run_frame_count("other") == 1
    finally:
        s.close()


def test_rehydrate_summary_probe_failure_does_not_cost_the_run(store):
    class ExplodingSummary(RichFrameSource):
        def run_summary(self, run_id):
            raise RuntimeError("mongod went away")

    frames = [make_frame(4, frame_idx=i, seed=i) for i in range(3)]
    src = ExplodingSummary(frames, [("R", "m", 1.0, T0)])
    assert store.rehydrate_run("R", src) == 3
    assert list(store.iter_frames("R")) == frames


# ── the same contract, proven against the REAL MongoArchive ──────────────────


def _mongo_client():
    from pymongo import MongoClient

    from apps.config import kpi_sink_settings

    uri = kpi_sink_settings.get("mongo_uri")
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=1000)
    return MongoClient(
        kpi_sink_settings["mongo_host"],
        int(kpi_sink_settings["mongo_port"]),
        serverSelectionTimeoutMS=1000,
    )


@pytest.fixture(scope="module")
def mongo_client():
    pytest.importorskip("pymongo")
    try:
        client = _mongo_client()
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Mongo unreachable ({type(exc).__name__}: {exc})")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def archive(mongo_client):
    import uuid

    from apps.dataplane.archive.mongo import MongoArchive

    db_name = f"dataplane_test_{uuid.uuid4().hex}"
    arc = MongoArchive(client=mongo_client, db_name=db_name)
    try:
        arc.ensure_indexes()
        yield arc
    finally:
        try:
            mongo_client.drop_database(db_name)
        finally:
            pass


def _populate(store, run_id="RM"):
    frames = [make_frame(8, frame_idx=i, sim_time_ms=float(i) * 10, seed=i) for i in range(6)]
    store.write_frames(run_id, frames)
    store.write_kpi_events([
        (run_id, "empty_km", 12.5, T0),
        (run_id, "loaded_km", 30.0, T0 + timedelta(minutes=1)),
    ])
    store.write_breakdown(run_id, "truck", T0, True, _entities(20))
    store.upsert_run_meta(
        run_id,
        status="COMPLETED",
        n_trucks=8,
        haulier_codes=json.dumps({"ACME": 1, "BOLT": 2}),
        slot_map=json.dumps({"t1": 0, "t2": 1}),
        source="live",
    )
    return frames


def test_dump_evict_rehydrate_round_trip_against_a_real_mongo_archive(store, archive):
    """The protocol match is proven against the REAL archive, not a hand-written fake."""
    frames = _populate(store)
    archive.dump_run(store, "RM", reason="test", complete=True)
    # The code books live in the run summary (whoever wrote it — the dump or the service).
    archive.runs.update_one(
        {"_id": "RM"},
        {"$set": {"haulier_codes": json.dumps({"ACME": 1, "BOLT": 2}),
                  "slot_map": json.dumps({"t1": 0, "t2": 1}), "n_trucks": 8}},
        upsert=True,
    )

    store.evict_run("RM")
    assert store.run_frame_count("RM") == 0
    assert store.breakdown_row_count("RM") == 0

    assert store.rehydrate_run("RM", archive) == 6
    assert list(store.iter_frames("RM")) == frames
    assert store.read_kpi_events("RM") == [
        ("RM", "empty_km", 12.5, T0),
        ("RM", "loaded_km", 30.0, T0 + timedelta(minutes=1)),
    ]
    assert store.breakdown_row_count("RM") == 20, "the Companies rows must survive an eviction"
    rows = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = 'RM' ORDER BY entity_id"
    )
    assert rows[0]["entity_id"] == "truck_0"
    assert rows[0]["haulier_name"] == "Haulier 0"
    assert rows[0]["final"] is True
    assert rows[0]["sim_clock"] == T0
    assert rows[1]["empty_km"] == 2.0
    meta = store.get_run_meta("RM")
    assert json.loads(meta["haulier_codes"]) == {"ACME": 1, "BOLT": 2}
    assert json.loads(meta["slot_map"]) == {"t1": 0, "t2": 1}
    assert meta["source"] == "rehydrated"
    # The terminal marker must come back too: without it run_is_terminal() answers False,
    # reconcile() flips the durable complete=True back to False, and /health counts a fully
    # archived run as pending for the life of the process.
    assert meta["status"] == "COMPLETED"
    assert isinstance(meta["first_seen"], datetime) or meta["first_seen"] is None
    assert store.get_export_state("RM") is not None
    assert store.get_export_state("RM")["status"] == "exported"


def test_a_real_archive_that_dies_mid_read_leaves_the_run_intact(store, archive, mongo_client):
    """Subclass the REAL MongoArchive so the failure is injected into the real read path."""
    from apps.dataplane.archive.mongo import MongoArchive

    class FlakyArchive(MongoArchive):
        """The real archive, with its frame cursor dying part-way.

        ``rehydrate_run`` prefers the columnar ``read_run_frame_docs`` seam and falls back
        to ``read_run_frames``; both are broken here so the failure lands wherever the
        store actually reads.
        """

        def read_run_frame_docs(self, run_id):
            for i, doc in enumerate(super().read_run_frame_docs(run_id)):
                if i >= 3:
                    raise RuntimeError("cursor id 42 not found (timed out)")
                yield doc

        def read_run_frames(self, run_id):
            for i, frame in enumerate(super().read_run_frames(run_id)):
                if i >= 3:
                    raise RuntimeError("cursor id 42 not found (timed out)")
                yield frame

    _populate(store)
    archive.dump_run(store, "RM", reason="test", complete=True)
    before = _snapshot(store, "RM")

    flaky = FlakyArchive(client=mongo_client, db_name=archive.db_name)
    assert flaky.has_run("RM")
    with pytest.raises(RuntimeError):
        store.rehydrate_run("RM", flaky, force=True)
    assert _snapshot(store, "RM") == before


# ─────────────────────────────────────────────────────────────────────────────
# ROUND 3 / ROOT 3 + the two duck-side ROOT 4 items.
#
# * every row write is columnar (``executemany`` over ON CONFLICT measured 349 rows/s on
#   this box: 35 000 breakdown rows = 100+ s, held the global store lock, and ran on the
#   Kafka poll thread);
# * ``rehydrate_run`` reads the whole source BEFORE it opens a transaction;
# * ``run_meta.status`` / ``first_seen`` / ``run_export_state`` come back, so a recovered
#   finished run does not re-open as live;
# * ``evict_run`` clears ``run_export_state`` too.
#
# The parity reference below is the pre-columnar SQL, verbatim. Nothing here uses a test
# double for DuckStore — the real class writes into a real database at ``tmp_path``.
# ─────────────────────────────────────────────────────────────────────────────

_OLD_KPI_UPSERT = """
INSERT INTO kpi_events (run_id, metric, value, sim_clock, ingested_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (run_id, metric, sim_clock) DO UPDATE SET
    value = excluded.value,
    ingested_at = excluded.ingested_at
"""

_OLD_BREAKDOWN_UPSERT = """
INSERT INTO kpi_breakdown_rows (
    run_id, scope, sim_clock, final, entity_id, haulier_id, haulier_name,
    num_orders_completed, empty_km, loaded_km, total_km, empty_ratio, active_hours,
    orders_per_day, dual_cycle_count, chain_opportunities, dual_cycle_rate, num_trucks,
    payload, ingested_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id, scope, sim_clock, entity_id) DO UPDATE SET
    final = excluded.final,
    haulier_id = excluded.haulier_id,
    haulier_name = excluded.haulier_name,
    num_orders_completed = excluded.num_orders_completed,
    empty_km = excluded.empty_km,
    loaded_km = excluded.loaded_km,
    total_km = excluded.total_km,
    empty_ratio = excluded.empty_ratio,
    active_hours = excluded.active_hours,
    orders_per_day = excluded.orders_per_day,
    dual_cycle_count = excluded.dual_cycle_count,
    chain_opportunities = excluded.chain_opportunities,
    dual_cycle_rate = excluded.dual_cycle_rate,
    num_trucks = excluded.num_trucks,
    payload = excluded.payload,
    ingested_at = excluded.ingested_at
"""


def _old_write_breakdown(store, run_id, scope, sim_clock, final, entities, now):
    """The replaced row-at-a-time implementation, verbatim, as the parity reference."""
    from apps.dataplane.store.duck import _num

    if scope not in ("truck", "haulier") or not entities:
        return 0
    params = []
    for e in entities:
        eid = e.get("id")
        if eid is None:
            continue
        params.append((
            run_id, scope, sim_clock, bool(final), str(eid),
            (str(e["haulier_id"]) if e.get("haulier_id") is not None else None),
            (str(e["haulier_name"]) if e.get("haulier_name") is not None else None),
            _num(e.get("num_orders_completed")), _num(e.get("empty_km")),
            _num(e.get("loaded_km")), _num(e.get("total_km")), _num(e.get("empty_ratio")),
            _num(e.get("active_hours")), _num(e.get("orders_per_day")),
            _num(e.get("dual_cycle_count")), _num(e.get("chain_opportunities")),
            _num(e.get("dual_cycle_rate")), _num(e.get("num_trucks")),
            json.dumps(e), now,
        ))
    if not params:
        return 0
    store._cursor().executemany(_OLD_BREAKDOWN_UPSERT, params)
    return len(params)


def _old_write_kpi_events(store, rows, now):
    params = [(r, m, float(v), sc, now) for r, m, v, sc in rows]
    if not params:
        return 0
    store._cursor().executemany(_OLD_KPI_UPSERT, params)
    return len(params)


#: Everything a semantics-parity fixture has to contain: NULLs, a non-integral float bound
#: into a BIGINT column, a value that does not parse as a number at all, duplicate keys
#: inside ONE batch (the property successive executemany upserts gave for free), and an
#: entity with nothing but an id.
_PARITY_ENTITIES = [
    {"id": "e1", "haulier_id": "h1", "haulier_name": "Haulier 1",
     "num_orders_completed": 3.7, "empty_km": None, "loaded_km": "3.5",
     "total_km": "not-a-number", "empty_ratio": 0.0, "active_hours": -2.5,
     "orders_per_day": 1e18, "dual_cycle_count": 2.5, "chain_opportunities": None,
     "dual_cycle_rate": 0.5, "num_trucks": 1},
    {"id": "e2"},
    {"id": "e3", "haulier_id": None, "haulier_name": None, "empty_km": 0.0},
    # duplicate key inside the same batch: the LAST occurrence must win
    {"id": "e1", "haulier_id": "h9", "haulier_name": "Haulier 9",
     "num_orders_completed": 12, "empty_km": 42.0, "loaded_km": 1.0, "total_km": 43.0,
     "empty_ratio": 0.97, "active_hours": 1.0, "orders_per_day": 3.0,
     "dual_cycle_count": 0, "chain_opportunities": 0, "dual_cycle_rate": 0.0,
     "num_trucks": 2},
]

_COMPARED_BD_COLUMNS = (
    "scope, sim_clock, final, entity_id, haulier_id, haulier_name, num_orders_completed, "
    "empty_km, loaded_km, total_km, empty_ratio, active_hours, orders_per_day, "
    "dual_cycle_count, chain_opportunities, dual_cycle_rate, num_trucks, payload"
)


def test_columnar_writes_are_semantically_identical_to_the_executemany_path(store):
    """(e) Byte-for-byte parity of the two write paths over the nasty fixture."""
    now = datetime(2026, 8, 5, 9, 30, 0)

    assert store.write_breakdown("NEW", "truck", T0, True, _PARITY_ENTITIES) == 4
    assert _old_write_breakdown(store, "OLD", "truck", T0, True, _PARITY_ENTITIES, now) == 4

    new_rows = store.query(
        f"SELECT {_COMPARED_BD_COLUMNS} FROM kpi_breakdown_rows WHERE run_id = 'NEW' "
        "ORDER BY entity_id"
    )
    old_rows = store.query(
        f"SELECT {_COMPARED_BD_COLUMNS} FROM kpi_breakdown_rows WHERE run_id = 'OLD' "
        "ORDER BY entity_id"
    )
    assert new_rows == old_rows
    # …and the specific coercions the fixture exists to pin down
    e1 = {r["entity_id"]: r for r in new_rows}["e1"]
    assert e1["num_orders_completed"] == 12       # last occurrence in the batch won
    assert e1["empty_km"] == 42.0
    e3 = {r["entity_id"]: r for r in new_rows}["e3"]
    assert e3["haulier_id"] is None and e3["haulier_name"] is None
    e2 = {r["entity_id"]: r for r in new_rows}["e2"]
    assert e2["total_km"] is None                 # unparseable -> NULL, both paths
    assert all(r["ingested_at"] is not None for r in
               store.query("SELECT ingested_at FROM kpi_breakdown_rows WHERE run_id='NEW'"))

    # 3.7 into a BIGINT column: identical rounding under both paths
    store.write_breakdown("NEW2", "haulier", T0, False, [{"id": "x", "num_orders_completed": 3.7}])
    _old_write_breakdown(store, "OLD2", "haulier", T0, False,
                         [{"id": "x", "num_orders_completed": 3.7}], now)
    assert (store.query("SELECT num_orders_completed AS v FROM kpi_breakdown_rows "
                        "WHERE run_id='NEW2'")[0]["v"]
            == store.query("SELECT num_orders_completed AS v FROM kpi_breakdown_rows "
                           "WHERE run_id='OLD2'")[0]["v"] == 4)

    # kpi: same fixture shape, including a duplicate key inside one batch
    kpi = [("KNEW", "m", 1.0, T0), ("KNEW", "m2", 2.5, T0), ("KNEW", "m", 9.0, T0)]
    assert store.write_kpi_events(kpi) == 3
    assert _old_write_kpi_events(store, [(r[0].replace("KNEW", "KOLD"), *r[1:]) for r in kpi],
                                 now) == 3
    assert ([(r[1], r[2], r[3]) for r in store.read_kpi_events("KNEW")]
            == [(r[1], r[2], r[3]) for r in store.read_kpi_events("KOLD")])
    assert dict((r[1], r[2]) for r in store.read_kpi_events("KNEW")) == {"m": 9.0, "m2": 2.5}

    # an empty batch is a no-op under both
    assert store.write_breakdown("NEW", "truck", T0, False, []) == 0
    assert store.write_kpi_events([]) == 0
    assert store.write_breakdown_batch("NEW", []) == 0


def test_a_tz_aware_sim_clock_lands_at_the_utc_instant(store):
    """The one deliberate divergence: a tz-aware clock is normalised, not shifted.

    ``np.array([tz_aware], dtype='datetime64[us]')`` also emits a numpy DeprecationWarning
    that is documented to become an error, so ``_naive_utc`` runs BEFORE array construction.
    """
    import warnings

    aware = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        store.write_breakdown("TZ", "truck", aware, False, [{"id": "t1", "empty_km": 1.0}])
        store.write_kpi_events([("TZ", "m", 1.0, aware)])

    expected = datetime(2026, 8, 5, 10, 0, 0)  # 12:00+02:00 == 10:00Z
    assert store.query("SELECT sim_clock FROM kpi_breakdown_rows WHERE run_id='TZ'")[0][
        "sim_clock"] == expected
    assert store.read_kpi_events("TZ")[0][3] == expected


def test_write_breakdown_batch_coalesces_many_snapshots_in_one_statement(store):
    """What the writer task calls: ~196 snapshots of a run become ONE INSERT."""
    snapshots = [
        ("truck", T0 + timedelta(hours=h), h == 5, _entities(4, empty_km=float(h)))
        for h in range(6)
    ]
    assert store.write_breakdown_batch("BATCH", snapshots) == 24
    assert store.breakdown_row_count("BATCH") == 24
    rows = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id='BATCH' AND entity_id='truck_0' "
        "ORDER BY sim_clock"
    )
    assert [r["empty_km"] for r in rows] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert [r["final"] for r in rows] == [False] * 5 + [True]
    # unknown scopes and id-less entities are dropped exactly as write_breakdown does
    assert store.write_breakdown_batch(
        "BATCH", [("wormhole", T0, False, _entities(2)), ("truck", T0, False, []),
                  ("truck", T0, False, [{"empty_km": 1}])]
    ) == 0
    # a run's rows are replaced, not duplicated, when the same batch is replayed
    assert store.write_breakdown_batch("BATCH", snapshots) == 24
    assert store.breakdown_row_count("BATCH") == 24


def test_rehydrate_restores_the_terminal_marker_and_the_export_state(store):
    """ROOT 4: a recovered finished run must not re-open as live."""
    frames = [make_frame(4, frame_idx=i, seed=i) for i in range(3)]
    first_seen = datetime(2026, 8, 4, 6, 0, 0)
    src = RichFrameSource(
        frames,
        [("FIN", "empty_km", 4.0, T0)],
        summary={"status": "completed", "first_seen": first_seen, "n_trucks": 500,
                 "haulier_codes": {"ACME": 1}, "slot_map": {"t1": 0},
                 "complete": True, "kpi_count": 1, "frame_count": 3},
    )
    assert store.rehydrate_run("FIN", src) == 3
    meta = store.get_run_meta("FIN")
    assert meta["status"] == "completed"
    assert meta["first_seen"] == first_seen
    assert meta["n_trucks"] == 500
    assert json.loads(meta["haulier_codes"]) == {"ACME": 1}
    state = store.get_export_state("FIN")
    assert state is not None
    assert (state["status"], state["row_count"], state["frame_count"]) == ("exported", 1, 3)


def test_summary_values_are_coerced_per_column_type_not_stringified(store):
    """A blanket ``str(value)`` wrote a stringified datetime into a TIMESTAMP column."""
    frames = [make_frame(4, frame_idx=0, seed=0)]
    src = RichFrameSource(
        frames,
        summary={"first_seen": "2026-08-04T06:00:00", "n_trucks": "not-a-number",
                 "status": 7, "haulier_codes": {"A": 1}},
    )
    assert store.rehydrate_run("S", src) == 1
    meta = store.get_run_meta("S")
    assert meta["first_seen"] is None, "a non-datetime first_seen is skipped, not coerced"
    assert meta["n_trucks"] is None
    assert meta["status"] == "7"
    assert json.loads(meta["haulier_codes"]) == {"A": 1}
    # an incomplete summary must not invent an export state
    assert store.get_export_state("S") is None


def test_evict_run_also_clears_the_export_state(store):
    """A stale 'exported' row must not outlive the run and mask a missing status."""
    store.write_kpi_events([("E", "m", 1.0, T0)])
    store.set_export_state("E", status="exported", row_count=1, frame_count=0)
    assert store.get_export_state("E") is not None
    store.evict_run("E")
    assert store.get_export_state("E") is None
    assert store.query("SELECT * FROM run_export_state WHERE run_id = 'E'") == []


# ── the columnar frame seam ──────────────────────────────────────────────────


class ColumnarFrameSource(RichFrameSource):
    """A source that speaks the fast seam: raw frame documents, columns as bytes."""

    def __init__(self, *args, fail_at=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_at = fail_at
        self.doc_reads = 0

    def read_run_frame_docs(self, run_id):
        self.doc_reads += 1
        for i, f in enumerate(self._frames):
            if self.fail_at is not None and i >= self.fail_at:
                raise RuntimeError("cursor id 42 not found (timed out)")
            yield {
                "frame_idx": f.frame_idx, "sim_time_ms": f.sim_time_ms, "n": int(f.n),
                "lng": np.ascontiguousarray(f.lng, "<f8").tobytes(),
                "lat": np.ascontiguousarray(f.lat, "<f8").tobytes(),
                "slot": np.ascontiguousarray(f.slot, "<u4").tobytes(),
                "state": np.ascontiguousarray(f.state, np.uint8).tobytes(),
                "haulier": np.ascontiguousarray(f.haulier, np.uint8).tobytes(),
            }

    def read_run_frames(self, run_id):
        raise AssertionError("the columnar seam must be preferred when it exists")


def test_the_columnar_frame_seam_is_preferred_and_round_trips_exactly(store):
    frames = [make_frame(37, frame_idx=i, sim_time_ms=float(i) * 3, seed=i) for i in range(9)]
    src = ColumnarFrameSource(frames, [("C", "m", 1.0, T0)])
    assert store.rehydrate_run("C", src) == 9
    assert src.doc_reads == 1
    assert list(store.iter_frames("C")) == frames
    assert store.get_run_meta("C")["max_frame_idx"] == 8


def test_a_source_that_dies_on_the_30th_frame_doc_changes_nothing(store):
    """(c) The DELETE is unreachable until the whole source has been read."""
    frames = [make_frame(20, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(60)]
    store.write_frames("R", frames[:12])
    store.write_kpi_events([("R", "empty_km", 4.0, T0)])
    store.write_breakdown("R", "truck", T0, False, _entities(3))
    store.upsert_run_meta("R", status="COMPLETED", source="live")
    store.set_export_state("R", status="exported", row_count=1)
    before = _snapshot(store, "R")
    before_counts = (store.run_frame_count("R"), store.kpi_row_count("R"),
                     store.breakdown_row_count("R"), store.get_export_state("R"))

    dying = ColumnarFrameSource(frames, [("R", "empty_km", 4.0, T0)], fail_at=30)
    with pytest.raises(RuntimeError):
        store.rehydrate_run("R", dying, force=True)

    assert _snapshot(store, "R") == before
    assert (store.run_frame_count("R"), store.kpi_row_count("R"),
            store.breakdown_row_count("R"), store.get_export_state("R")) == before_counts

    # a PLAIN retry against a healthy source then succeeds — no force=True needed
    healthy = ColumnarFrameSource(frames, [("R", "empty_km", 4.0, T0)])
    store.evict_run("R")
    assert store.rehydrate_run("R", healthy) == 60
    assert list(store.iter_frames("R")) == frames


def test_rehydrate_refuses_an_implausibly_large_run_instead_of_ooming(store, monkeypatch):
    import apps.dataplane.store.duck as duck_mod

    monkeypatch.setattr(duck_mod, "REHYDRATE_MAX_POSITION_ROWS", 100)
    frames = [make_frame(40, frame_idx=i, seed=i) for i in range(10)]
    with pytest.raises(DuckStoreError):
        store.rehydrate_run("BIG", ColumnarFrameSource(frames))
    assert store.run_frame_count("BIG") == 0
    assert store.get_run_meta("BIG") is None
    # the Frame-object fallback path is guarded too
    with pytest.raises(DuckStoreError):
        store.rehydrate_run("BIG", RichFrameSource(frames))
    assert store.list_run_ids() == []


def test_a_breakdown_reader_that_answers_none_leaves_the_rows_alone(store):
    """``None`` means "unknown", never "there were none" — the sentinel from mongo.py."""

    class UnknownBreakdown(RichFrameSource):
        def read_run_breakdown_columns(self, run_id):
            return None

        def read_run_breakdown_rows(self, run_id):
            raise AssertionError("must not fall back after an explicit None")

    frames = [make_frame(5, frame_idx=0, seed=0)]
    store.write_frames("U", frames)
    store.write_breakdown("U", "truck", T0, True, _entities(6))
    before = store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = 'U' ORDER BY entity_id"
    )
    assert store.rehydrate_run("U", UnknownBreakdown(frames), force=True) == 1
    assert store.query(
        "SELECT * FROM kpi_breakdown_rows WHERE run_id = 'U' ORDER BY entity_id"
    ) == before


def test_a_breakdown_reader_that_answers_empty_columns_clears_the_table(store):
    """The other half of the three-way distinction: empty-but-present means empty."""

    class EmptyBreakdown(RichFrameSource):
        def read_run_breakdown_columns(self, run_id):
            from apps.dataplane.store.duck import _BD_COLUMN_SPEC

            return {col: [] for col, _kind in _BD_COLUMN_SPEC}

    frames = [make_frame(5, frame_idx=0, seed=0)]
    store.write_frames("EC", frames)
    store.write_breakdown("EC", "truck", T0, True, _entities(6))
    assert store.rehydrate_run("EC", EmptyBreakdown(frames), force=True) == 1
    assert store.breakdown_row_count("EC") == 0


def test_the_connection_is_read_in_exactly_one_place(store):
    """The invariant that replaced 'every caller must remember to hold the lock'.

    ``self._conn`` is created in ``__init__``, closed in ``close()`` and read ONLY by
    ``_cursor()`` — which takes ``_conn_lock`` itself, so an unlocked acquisition is no
    longer expressible. This is the 2026-07-01 root cause, checked structurally.
    """
    import inspect

    import apps.dataplane.store.duck as duck_mod

    code = [
        line.strip()
        for line in inspect.getsource(duck_mod).splitlines()
        if "self._conn." in line or "self._conn =" in line
    ]
    code = [line for line in code if not line.startswith("#") and "``" not in line]
    # exactly: the duckdb.connect assignment, the one-time schema DDL, close(), _cursor()
    assert code == [
        "self._conn = duckdb.connect(self.db_path)",
        "self._conn.execute(_SCHEMA)",
        "cur = self._conn.cursor()",
        "self._conn.close()",
    ], code
    assert "with self._conn_lock:" in inspect.getsource(duck_mod.DuckStore._cursor)
    assert "with self._conn_lock:" in inspect.getsource(duck_mod.DuckStore._run_lock)
    # …and a run lock is never returned while the conn lock is still held
    lock = store._run_lock("x")
    assert lock.acquire(blocking=False)
    lock.release()
