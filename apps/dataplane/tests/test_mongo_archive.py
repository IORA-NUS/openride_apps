"""Tests for apps.dataplane.archive.mongo.

Skips cleanly when Mongo is unreachable. When it is reachable every test runs
against a throwaway ``dataplane_test_<uuid4hex>`` database that is dropped in the
fixture teardown, even on failure.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List

import numpy as np
import pytest

from apps.dataplane.archive.mongo import (
    FRAMES_COLLECTION,
    KPI_COLLECTION,
    RUNS_COLLECTION,
    ArchiveIncomplete,
    ArchiveUnavailable,
    BreakdownReadFailed,
    MongoArchive,
    doc_to_frame,
    frame_to_doc,
)
from apps.dataplane.contract.frame import Frame

pymongo = pytest.importorskip("pymongo")


def make_frame(n, frame_idx=0, sim_time_ms=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return Frame(
        frame_idx=frame_idx,
        sim_time_ms=sim_time_ms,
        lng=rng.uniform(3.0, 7.5, n),
        lat=rng.uniform(50.5, 53.5, n),
        slot=np.arange(n, dtype=np.uint32),
        state=rng.integers(0, 12, n, dtype=np.uint8),
        haulier=rng.integers(0, 8, n, dtype=np.uint8),
    )


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
    try:
        client = _mongo_client()
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Mongo unreachable ({type(exc).__name__}: {exc}) — skipping archive tests")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def archive(mongo_client):
    db_name = f"dataplane_test_{uuid.uuid4().hex}"
    arc = MongoArchive(client=mongo_client, db_name=db_name)
    try:
        arc.ensure_indexes()
        yield arc
    finally:
        try:
            mongo_client.drop_database(db_name)
        finally:
            # the client is module-scoped and shared; do not close it here
            pass


# ── a hand-written DuckReader fake (never import the real store) ────────────


class FakeReader:
    def __init__(self, runs: Dict[str, dict]):
        # runs: {run_id: {"frames": [Frame], "kpi": [tuple], "raise": bool}}
        self.runs = runs

    def list_run_ids(self) -> List[str]:
        return sorted(self.runs)

    def iter_frames(self, run_id: str) -> Iterator[Frame]:
        spec = self.runs[run_id]
        if spec.get("raise"):
            raise RuntimeError(f"boom for {run_id}")
        return iter(spec.get("frames", []))

    def read_kpi_events(self, run_id: str):
        return list(self.runs[run_id].get("kpi", []))


def kpi_rows(run_id: str, count: int) -> List[tuple]:
    base = datetime(2026, 8, 5, 12, 0, 0)
    return [
        (run_id, f"metric_{i % 3}", float(i), base + timedelta(minutes=i))
        for i in range(count)
    ]


def breakdown_rows(run_id: str, count: int, scope: str = "truck") -> List[dict]:
    """Row dicts shaped exactly like a ``kpi_breakdown_rows`` row."""
    base = datetime(2026, 8, 5, 12, 0, 0)
    return [
        {
            "run_id": run_id,
            "scope": scope,
            "sim_clock": base + timedelta(minutes=i // 5),
            "final": False,
            "entity_id": f"{scope}_{i}",
            "haulier_id": "h1",
            "haulier_name": "Haulier One",
            "num_orders_completed": i,
            "empty_km": float(i),
            "loaded_km": 2.0 * i,
            "total_km": 3.0 * i,
            "empty_ratio": 0.33,
            "active_hours": 1.5,
            "orders_per_day": 2.0,
            "dual_cycle_count": 0,
            "chain_opportunities": 0,
            "dual_cycle_rate": 0.0,
            "num_trucks": 1,
            "payload": "{}",
        }
        for i in range(count)
    ]


class FakeDuckLike:
    """A fake shaped like the real DuckStore: it knows run status and progress.

    ``FakeReader`` above is the minimal DuckReader; this one additionally exposes the
    probes the archive uses to tell a live run from a finished one.
    """

    def __init__(self) -> None:
        self.frames: Dict[str, list] = {}
        self.kpi: Dict[str, list] = {}
        self.breakdown: Dict[str, list] = {}
        self.status: Dict[str, str] = {}

    def list_run_ids(self) -> List[str]:
        return sorted(set(self.frames) | set(self.kpi) | set(self.breakdown))

    def iter_frames(self, run_id: str) -> Iterator[Frame]:
        return iter(list(self.frames.get(run_id, [])))

    def read_kpi_events(self, run_id: str):
        return list(self.kpi.get(run_id, []))

    def read_breakdown_rows(self, run_id: str):
        return [dict(r) for r in self.breakdown.get(run_id, [])]

    # ── probes the real DuckStore also has ──
    def get_run_meta(self, run_id: str):
        return {"run_id": run_id, "status": self.status.get(run_id)}

    def frame_range(self, run_id: str):
        idxs = [int(f.frame_idx) for f in self.frames.get(run_id, [])]
        return (min(idxs), max(idxs)) if idxs else None

    def kpi_row_count(self, run_id: str) -> int:
        return len(self.kpi.get(run_id, []))

    def query(self, sql: str, params=None):
        """Only the cheap count probes, like the real store answers them."""
        run_id = (params or [""])[0]
        if "kpi_breakdown_rows" in sql and "count(" in sql.lower():
            return [{"c": len(self.breakdown.get(run_id, []))}]
        return []


# ── document shape ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [0, 1, 500])
def test_doc_roundtrip_is_pure(n):
    frame = make_frame(n, frame_idx=3, sim_time_ms=1234.5, seed=n)
    doc = frame_to_doc("r1", frame)
    assert doc["_id"] == "r1:3"
    assert doc["n"] == n
    assert doc_to_frame(doc) == frame


def test_one_document_per_frame_not_per_position():
    frame = make_frame(500)
    doc = frame_to_doc("r1", frame)
    assert len(doc["lng"]) == 8 * 500
    assert len(doc["state"]) == 500


# ── frame round trip ────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [0, 1, 500])
def test_frame_roundtrip_through_mongo(archive, n):
    run_id = f"run_{n}"
    frames = [make_frame(n, frame_idx=i, sim_time_ms=float(i * 100), seed=i) for i in range(3)]
    assert archive.write_frames(run_id, frames) == 3
    read_back = list(archive.read_run_frames(run_id))
    assert read_back == frames


def test_frames_are_read_back_in_frame_idx_order(archive):
    run_id = "ordered"
    frames = [make_frame(4, frame_idx=i, seed=i) for i in (5, 1, 9, 0, 3)]
    archive.write_frames(run_id, frames, batch_size=2)
    idxs = [f.frame_idx for f in archive.read_run_frames(run_id)]
    assert idxs == [0, 1, 3, 5, 9]


def test_rewriting_the_same_run_inserts_nothing_new(archive):
    run_id = "idem"
    frames = [make_frame(20, frame_idx=i, seed=i) for i in range(10)]
    assert archive.write_frames(run_id, frames, batch_size=3) == 10
    before = archive.frames.count_documents({"run_id": run_id})
    assert archive.write_frames(run_id, frames, batch_size=3) == 0
    assert archive.frames.count_documents({"run_id": run_id}) == before == 10


def test_frames_own_their_memory(archive):
    frames = [make_frame(8, frame_idx=0)]
    archive.write_frames("owning", frames)
    got = next(iter(archive.read_run_frames("owning")))
    got.lng[0] = 999.0  # would raise if the array were a read-only frombuffer view
    assert got.lng.flags.writeable


# ── kpi round trip ──────────────────────────────────────────────────────────


def test_kpi_roundtrip(archive):
    run_id = "kpi_run"
    rows = kpi_rows(run_id, 25)
    assert archive.write_kpi_events(run_id, rows) == 25
    got = list(archive.read_run_kpi_events(run_id))
    assert len(got) == 25
    assert {(r[1], r[2]) for r in got} == {(r[1], r[2]) for r in rows}
    assert archive.write_kpi_events(run_id, rows) == 0
    assert archive.kpi.count_documents({"run_id": run_id}) == 25


def test_kpi_dedupes_within_one_batch(archive):
    rows = kpi_rows("dupes", 4) * 3
    assert archive.write_kpi_events("dupes", rows) == 4


# ── dump_run ────────────────────────────────────────────────────────────────


def test_dump_run_against_fake_reader(archive):
    run_id = "dumped"
    frames = [make_frame(12, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(4)]
    reader = FakeReader({run_id: {"frames": frames, "kpi": kpi_rows(run_id, 7)}})

    result = archive.dump_run(reader, run_id, reason="unit")
    assert result["frames"] == 4
    assert result["frames_inserted"] == 4
    assert result["kpi_rows"] == 7
    assert result["frame_min"] == 0 and result["frame_max"] == 3

    summary = archive.run_summary(run_id)
    assert summary["frame_count"] == 4
    assert summary["kpi_count"] == 7
    assert summary["reason"] == "unit"
    assert summary["frame_max"] == 3
    assert isinstance(summary["dumped_at"], datetime)

    assert list(archive.read_run_frames(run_id)) == frames
    assert archive.has_run(run_id) is True
    assert archive.has_run("never_seen") is False

    # second dump is idempotent
    again = archive.dump_run(reader, run_id, reason="unit")
    assert again["frames_inserted"] == 0
    assert again["kpi_inserted"] == 0
    assert archive.frames.count_documents({"run_id": run_id}) == 4


def test_dump_run_with_no_data(archive):
    reader = FakeReader({"empty": {"frames": [], "kpi": []}})
    result = archive.dump_run(reader, "empty")
    assert result["frames"] == 0 and result["frame_min"] is None
    assert archive.run_summary("empty")["frame_count"] == 0


# ── reconcile ───────────────────────────────────────────────────────────────


def test_reconcile_dumps_only_missing_runs_and_is_a_noop_second_time(archive):
    reader = FakeReader(
        {
            "a": {"frames": [make_frame(5, frame_idx=0)], "kpi": kpi_rows("a", 2)},
            "b": {"frames": [make_frame(5, frame_idx=0)], "kpi": kpi_rows("b", 2)},
            "c": {"frames": [], "kpi": []},
        }
    )
    archive.dump_run(reader, "b", reason="pre")

    first = archive.reconcile(reader, reason="sweep")
    assert first["checked"] == 3
    assert first["dumped"] == 2
    assert sorted(first["run_ids"]) == ["a", "c"]
    assert first["errors"] == {}

    second = archive.reconcile(reader, reason="sweep")
    assert second["checked"] == 3
    assert second["dumped"] == 0
    assert second["run_ids"] == []


def test_reconcile_redumps_a_run_that_vanished_from_mongo(archive):
    frames = [make_frame(6, frame_idx=i, seed=i) for i in range(3)]
    reader = FakeReader({"gone": {"frames": frames, "kpi": kpi_rows("gone", 3)}})
    archive.reconcile(reader)
    assert archive.frames.count_documents({"run_id": "gone"}) == 3

    # simulate the archive losing the run (test-local cleanup, the archive never deletes)
    archive.runs.delete_one({"_id": "gone"})
    archive.frames.delete_many({"run_id": "gone"})

    result = archive.reconcile(reader)
    assert result["dumped"] == 1
    assert result["run_ids"] == ["gone"]
    assert list(archive.read_run_frames("gone")) == frames


def test_reconcile_records_one_bad_run_and_still_dumps_the_others(archive):
    reader = FakeReader(
        {
            "ok1": {"frames": [make_frame(3, frame_idx=0)], "kpi": kpi_rows("ok1", 1)},
            "bad": {"raise": True},
            "ok2": {"frames": [make_frame(3, frame_idx=0)], "kpi": kpi_rows("ok2", 1)},
        }
    )
    result = archive.reconcile(reader)
    assert result["checked"] == 3
    assert result["dumped"] == 2
    assert sorted(result["run_ids"]) == ["ok1", "ok2"]
    assert set(result["errors"]) == {"bad"}
    assert "boom for bad" in result["errors"]["bad"]
    assert archive.run_summary("bad") is None
    # the sweep retries the failed run next time
    assert result["pending_run_ids"] == ["bad"]


def test_reconcile_accepts_an_explicit_run_id_list(archive):
    reader = FakeReader(
        {
            "x": {"frames": [make_frame(2, frame_idx=0)], "kpi": []},
            "y": {"frames": [make_frame(2, frame_idx=0)], "kpi": []},
        }
    )
    result = archive.reconcile(reader, run_ids=["y"])
    assert result["checked"] == 1
    assert result["run_ids"] == ["y"]
    assert archive.run_summary("x") is None


def test_reconcile_never_deletes(archive):
    """A sweep must not remove documents that are already there."""
    reader = FakeReader({"keep": {"frames": [make_frame(4, frame_idx=0)], "kpi": kpi_rows("keep", 2)}})
    archive.dump_run(reader, "keep")
    counts_before = (
        archive.frames.count_documents({}),
        archive.kpi.count_documents({}),
        archive.runs.count_documents({}),
    )
    archive.reconcile(reader)
    archive.reconcile(FakeReader({}))
    counts_after = (
        archive.frames.count_documents({}),
        archive.kpi.count_documents({}),
        archive.runs.count_documents({}),
    )
    assert counts_before == counts_after


def test_source_module_calls_no_delete_api():
    """Static guard: the archive module must contain no delete/drop call."""
    import pathlib

    import apps.dataplane.archive.mongo as mod

    text = pathlib.Path(mod.__file__).read_text()
    for forbidden in (
        "delete_one",
        "delete_many",
        "drop(",
        "drop_database",
        "remove(",
        "find_one_and_delete",
    ):
        assert forbidden not in text, f"archive must never call {forbidden}"


# ── failure handling ────────────────────────────────────────────────────────


def test_driver_errors_are_wrapped(archive, monkeypatch):
    from pymongo.errors import OperationFailure

    def boom(*a, **kw):
        raise OperationFailure("nope")

    monkeypatch.setattr(archive.frames, "insert_many", boom)
    with pytest.raises(ArchiveUnavailable):
        archive.write_frames("wrapped", [make_frame(3, frame_idx=0)])


def test_ping_and_stats(archive):
    assert archive.ping() is True
    stats = archive.stats()
    assert stats["available"] is True
    assert stats["db"] == archive.db_name


def test_collection_names_are_new(archive):
    assert FRAMES_COLLECTION == "dataplane_frames"
    assert KPI_COLLECTION == "dataplane_kpi_events"
    assert RUNS_COLLECTION == "dataplane_runs"


def test_partial_dump_is_not_marked_archived(archive, monkeypatch):
    """The summary is written last, so a failed dump is retried by the next sweep."""
    reader = FakeReader({"half": {"frames": [make_frame(3, frame_idx=0)], "kpi": kpi_rows("half", 1)}})
    real_write = archive.write_kpi_events

    def boom(*a, **kw):
        raise ArchiveUnavailable("kpi write died")

    monkeypatch.setattr(archive, "write_kpi_events", boom)
    result = archive.reconcile(reader)
    assert result["dumped"] == 0 and "half" in result["errors"]
    assert archive.run_summary("half") is None

    monkeypatch.setattr(archive, "write_kpi_events", real_write)
    assert archive.reconcile(reader)["dumped"] == 1


# ── regressions: the sweep must not close a run that is still running ───────


def test_sweep_of_a_live_run_keeps_catching_up_until_the_run_is_terminal(archive):
    """A sweep that fires mid-run must NOT mark the run archived and stop.

    Regression: the reconciler used to dump the live run's prefix, write its summary,
    and skip it forever — so a missed terminal run_status left only the prefix durable.
    """
    duck = FakeDuckLike()
    run_id = "live"
    duck.frames[run_id] = [make_frame(10, frame_idx=i, seed=i) for i in range(3)]
    duck.kpi[run_id] = kpi_rows(run_id, 3)

    first = archive.reconcile(duck, reason="sweep")
    assert first["dumped"] == 1
    assert archive.frames.count_documents({"run_id": run_id}) == 3
    assert archive.run_summary(run_id)["complete"] is False

    # the run keeps producing frames after the sweep
    duck.frames[run_id] = [make_frame(10, frame_idx=i, seed=i) for i in range(20)]
    duck.kpi[run_id] = kpi_rows(run_id, 12)

    second = archive.reconcile(duck, reason="sweep")
    assert second["dumped"] == 1, "a still-running run must stay in the sweep"
    assert archive.frames.count_documents({"run_id": run_id}) == 20
    assert archive.kpi.count_documents({"run_id": run_id}) == 12
    assert archive.run_summary(run_id)["frame_count"] == 20
    assert archive.run_summary(run_id)["complete"] is False

    # run ends: run_meta.status is set, the next dump closes the run
    duck.status[run_id] = "COMPLETED"
    third = archive.reconcile(duck, reason="sweep")
    assert third["dumped"] == 1
    summary = archive.run_summary(run_id)
    assert summary["complete"] is True
    assert summary["frame_count"] == 20 and summary["frame_max"] == 19

    fourth = archive.reconcile(duck, reason="sweep")
    assert fourth["dumped"] == 0
    assert fourth["pending_run_ids"] == []


def test_a_quiet_live_run_is_not_re_dumped_but_stays_pending(archive):
    duck = FakeDuckLike()
    duck.frames["quiet"] = [make_frame(4, frame_idx=0)]
    first = archive.reconcile(duck)
    assert first["dumped"] == 1
    second = archive.reconcile(duck)
    assert second["dumped"] == 0, "no new rows: nothing to re-send"
    assert second["pending_run_ids"] == ["quiet"], "but the run is NOT archived yet"
    assert archive.archived_run_ids() == set()


def test_archived_run_ids_excludes_a_run_that_is_still_running(archive):
    duck = FakeDuckLike()
    duck.frames["running"] = [make_frame(4, frame_idx=0)]
    duck.frames["done"] = [make_frame(4, frame_idx=0)]
    duck.status["done"] = "COMPLETED"

    archive.reconcile(duck)
    assert archive.archived_run_ids() == {"done"}


def test_a_terminal_dump_is_not_reopened_by_later_sweeps(archive):
    duck = FakeDuckLike()
    duck.frames["r"] = [make_frame(4, frame_idx=i, seed=i) for i in range(2)]
    duck.status["r"] = "COMPLETED"
    archive.dump_run(duck, "r", reason="COMPLETED")
    assert archive.reconcile(duck)["dumped"] == 0


def test_a_complete_summary_that_falls_behind_duckdb_is_re_swept(archive):
    """Watermark, not a boolean: more frames in DuckDB than in Mongo means pending."""
    duck = FakeDuckLike()
    duck.frames["w"] = [make_frame(4, frame_idx=i, seed=i) for i in range(2)]
    duck.status["w"] = "COMPLETED"
    archive.dump_run(duck, "w", reason="COMPLETED")
    assert archive.reconcile(duck)["dumped"] == 0

    duck.frames["w"].append(make_frame(4, frame_idx=2, seed=2))
    result = archive.reconcile(duck)
    assert result["dumped"] == 1
    assert archive.frames.count_documents({"run_id": "w"}) == 3


def test_explicit_complete_flag_overrides_the_probe(archive):
    duck = FakeDuckLike()
    duck.frames["x"] = [make_frame(3, frame_idx=0)]
    archive.dump_run(duck, "x", reason="manual", complete=True)
    assert archive.run_summary("x")["complete"] is True
    assert archive.archived_run_ids() == {"x"}


def test_summary_counts_describe_the_archive_not_one_dump(archive):
    duck = FakeDuckLike()
    duck.frames["s"] = [make_frame(3, frame_idx=i, seed=i) for i in range(2)]
    duck.kpi["s"] = kpi_rows("s", 4)
    archive.dump_run(duck, "s", reason="sweep")
    duck.frames["s"] = [make_frame(3, frame_idx=i, seed=i) for i in range(5)]
    duck.kpi["s"] = kpi_rows("s", 9)
    archive.dump_run(duck, "s", reason="sweep")
    summary = archive.run_summary("s")
    assert summary["frame_count"] == 5
    assert summary["kpi_count"] == 9
    assert summary["frame_min"] == 0 and summary["frame_max"] == 4


# ── regressions: kpi_breakdown_rows must be durable ─────────────────────────


def test_dump_run_archives_breakdown_rows(archive):
    duck = FakeDuckLike()
    run_id = "bd"
    duck.frames[run_id] = [make_frame(4, frame_idx=0)]
    duck.kpi[run_id] = kpi_rows(run_id, 2)
    duck.breakdown[run_id] = breakdown_rows(run_id, 30) + breakdown_rows(run_id, 5, scope="haulier")
    duck.status[run_id] = "COMPLETED"

    result = archive.dump_run(duck, run_id, reason="COMPLETED")
    assert result["breakdown_rows"] == 35
    assert result["breakdown_inserted"] == 35
    assert archive.breakdown.count_documents({"run_id": run_id}) == 35
    assert archive.run_summary(run_id)["breakdown_count"] == 35

    read_back = list(archive.read_run_breakdown_rows(run_id))
    assert len(read_back) == 35
    one = [r for r in read_back if r["entity_id"] == "truck_7"][0]
    assert one["scope"] == "truck"
    assert one["empty_km"] == 7.0
    assert one["haulier_name"] == "Haulier One"
    assert one["num_orders_completed"] == 7

    # idempotent
    again = archive.dump_run(duck, run_id, reason="COMPLETED")
    assert again["breakdown_inserted"] == 0
    assert archive.breakdown.count_documents({"run_id": run_id}) == 35


def test_breakdown_only_run_is_swept_and_found(archive):
    duck = FakeDuckLike()
    duck.breakdown["bdonly"] = breakdown_rows("bdonly", 6)
    duck.status["bdonly"] = "COMPLETED"
    assert archive.reconcile(duck)["dumped"] == 1
    assert archive.has_run("bdonly") is True
    assert len(list(archive.read_run_breakdown_rows("bdonly"))) == 6


def test_new_breakdown_rows_make_a_closed_run_pending_again(archive):
    duck = FakeDuckLike()
    duck.breakdown["grow"] = breakdown_rows("grow", 3)
    duck.status["grow"] = "COMPLETED"
    archive.reconcile(duck)
    assert archive.reconcile(duck)["dumped"] == 0
    duck.breakdown["grow"] = breakdown_rows("grow", 8)
    assert archive.reconcile(duck)["dumped"] == 1
    assert archive.breakdown.count_documents({"run_id": "grow"}) == 8


def test_breakdown_rows_are_read_from_a_reader_that_only_has_query(archive):
    """The real DuckStore exposes ``query``, not ``read_breakdown_rows``."""

    class QueryOnly:
        def list_run_ids(self):
            return ["q"]

        def iter_frames(self, run_id):
            return iter(())

        def read_kpi_events(self, run_id):
            return []

        def query(self, sql, params=None):
            if "count(*)" in sql.lower():
                return [{"c": len(breakdown_rows("q", 4))}]
            return breakdown_rows("q", 4)

    result = archive.dump_run(QueryOnly(), "q", reason="unit")
    assert result["breakdown_rows"] == 4
    assert archive.breakdown.count_documents({"run_id": "q"}) == 4


def test_a_second_dump_does_not_re_read_tables_that_are_already_whole(archive):
    duck = FakeDuckLike()
    duck.kpi["cheap"] = kpi_rows("cheap", 5)
    duck.breakdown["cheap"] = breakdown_rows("cheap", 6)
    reads = {"kpi": 0, "bd": 0}
    real_kpi, real_bd = duck.read_kpi_events, duck.read_breakdown_rows
    duck.read_kpi_events = lambda r: (reads.__setitem__("kpi", reads["kpi"] + 1), real_kpi(r))[1]
    duck.read_breakdown_rows = lambda r: (reads.__setitem__("bd", reads["bd"] + 1), real_bd(r))[1]

    archive.dump_run(duck, "cheap", reason="sweep")
    assert reads == {"kpi": 1, "bd": 1}
    archive.dump_run(duck, "cheap", reason="sweep")
    assert reads == {"kpi": 1, "bd": 1}, "unchanged tables must not be re-read every sweep"

    duck.kpi["cheap"] = kpi_rows("cheap", 9)
    archive.dump_run(duck, "cheap", reason="sweep")
    assert reads["kpi"] == 2
    assert archive.kpi.count_documents({"run_id": "cheap"}) == 9


# ── regressions: the durable record must carry the AUTHORITATIVE value ──────
#
# These run against the REAL DuckStore, not a fake. The round-1 lesson is that a fake
# reader cannot prove anything about the archive's interaction with the store that
# actually revises rows: `AnalyticsManager.recompute_breakdowns_full` rewrites the whole
# per-haulier series at the same sim_clocks at finalize, and only the real store's
# `_BREAKDOWN_UPSERT` / `ingested_at` behaviour exercises that path.


@pytest.fixture()
def duck(tmp_path):
    from apps.dataplane.store.duck import DuckStore

    store = DuckStore(str(tmp_path / "dp.duckdb"))
    try:
        yield store
    finally:
        store.close()


def _entity(eid, empty_km, orders):
    return {
        "id": eid,
        "haulier_id": "h1",
        "haulier_name": "ACME",
        "num_orders_completed": orders,
        "empty_km": empty_km,
        "loaded_km": 2.0,
        "total_km": 3.0,
        "empty_ratio": 0.3,
        "active_hours": 1.0,
        "orders_per_day": 1.0,
        "dual_cycle_count": 0,
        "chain_opportunities": 0,
        "dual_cycle_rate": 0.0,
        "num_trucks": 1,
    }


T = datetime(2026, 8, 5, 12, 0, 0)


def test_a_revised_breakdown_row_replaces_the_archived_one(archive, duck):
    """The finalize recompute rewrites the row IN PLACE: same key, same count.

    Regression: the archive was insert-only, so the mid-run snapshot (empty_km=10.0,
    final=False) stayed in Mongo forever while DuckDB held the authoritative 873.4 —
    and an evict+rehydrate then put the stale value BACK into DuckDB.
    """
    run_id = "revised_bd"
    duck.write_breakdown(run_id, "haulier", T, False, [_entity("ACME", 10.0, 5)])
    duck.upsert_run_meta(run_id, status="running")
    archive.dump_run(duck, run_id, reason="sweep")

    doc = archive.breakdown.find_one({"run_id": run_id, "doc_type": "row"})
    assert (doc["empty_km"], doc["num_orders_completed"], doc["final"]) == (10.0, 5, False)

    # authoritative recompute at the SAME sim_clock: the row COUNT does not move
    duck.write_breakdown(run_id, "haulier", T, True, [_entity("ACME", 873.4, 412)])
    duck.upsert_run_meta(run_id, status="COMPLETED")
    assert archive.breakdown.count_documents({"run_id": run_id, "doc_type": "row"}) == 1

    archive.dump_run(duck, run_id, reason="COMPLETED")

    doc = archive.breakdown.find_one({"run_id": run_id, "doc_type": "row"})
    assert (doc["empty_km"], doc["num_orders_completed"], doc["final"]) == (873.4, 412, True)
    assert archive.breakdown.count_documents({"run_id": run_id, "doc_type": "row"}) == 1
    # and it survives the read path the rehydrate uses
    row = next(iter(archive.read_run_breakdown_rows(run_id)))
    assert row["empty_km"] == 873.4 and row["final"] is True


def test_a_revised_kpi_value_replaces_the_archived_one(archive, duck):
    run_id = "revised_kpi"
    duck.write_kpi_events([(run_id, "empty_km", 1.0, T)])
    archive.dump_run(duck, run_id, reason="sweep")
    assert archive.kpi.find_one({"run_id": run_id})["value"] == 1.0

    duck.write_kpi_events([(run_id, "empty_km", 999.0, T)])
    assert duck.kpi_row_count(run_id) == 1  # unchanged count: this is the short-circuit
    archive.dump_run(duck, run_id, reason="COMPLETED")

    assert archive.kpi.find_one({"run_id": run_id})["value"] == 999.0
    assert archive.kpi.count_documents({"run_id": run_id}) == 1
    assert [r[2] for r in archive.read_run_kpi_events(run_id)] == [999.0]


def test_the_count_short_circuit_still_skips_a_run_that_really_is_unchanged(archive, duck):
    """The content watermark must not turn every sweep into a full re-read."""
    run_id = "quiet_real"
    duck.write_kpi_events([(run_id, "empty_km", 1.0, T)])
    duck.write_breakdown(run_id, "haulier", T, False, [_entity("ACME", 10.0, 5)])
    duck.upsert_run_meta(run_id, status="COMPLETED")

    first = archive.dump_run(duck, run_id, reason="COMPLETED")
    assert first["kpi_rows"] == 1 and first["breakdown_rows"] == 1
    summary = archive.run_summary(run_id)
    assert summary["kpi_max_ingested_at"] and summary["breakdown_max_ingested_at"]

    second = archive.dump_run(duck, run_id, reason="COMPLETED")
    assert second["kpi_rows"] == 0, "unchanged table must not be re-read"
    assert second["breakdown_rows"] == 0
    assert archive.reconcile(duck, reason="sweep")["dumped"] == 0


def test_a_revision_makes_a_closed_run_pending_for_the_sweep_again(archive, duck):
    """A revision moves no counter forward, so only the content watermark can see it."""
    run_id = "revised_sweep"
    duck.write_breakdown(run_id, "haulier", T, False, [_entity("ACME", 10.0, 5)])
    duck.upsert_run_meta(run_id, status="COMPLETED")
    assert archive.reconcile(duck, reason="sweep")["dumped"] == 1
    assert archive.reconcile(duck, reason="sweep")["dumped"] == 0

    duck.write_breakdown(run_id, "haulier", T, True, [_entity("ACME", 873.4, 412)])
    assert archive.reconcile(duck, reason="sweep")["dumped"] == 1
    doc = archive.breakdown.find_one({"run_id": run_id, "doc_type": "row"})
    assert (doc["empty_km"], doc["final"]) == (873.4, True)


def test_frames_stay_insert_only(archive, duck):
    """Frames are immutable and high-volume: replaces would be a throughput regression."""
    calls = {"insert_many": 0, "bulk_write": 0}
    real_insert = archive.frames.insert_many
    real_bulk = archive.frames.bulk_write
    archive.frames.insert_many = lambda *a, **kw: (
        calls.__setitem__("insert_many", calls["insert_many"] + 1),
        real_insert(*a, **kw),
    )[1]
    archive.frames.bulk_write = lambda *a, **kw: (
        calls.__setitem__("bulk_write", calls["bulk_write"] + 1),
        real_bulk(*a, **kw),
    )[1]
    try:
        duck.write_frames("fr", [make_frame(4, frame_idx=i, seed=i) for i in range(3)])
        archive.dump_run(duck, "fr", reason="unit")
    finally:
        archive.frames.insert_many = real_insert
        archive.frames.bulk_write = real_bulk
    assert calls["insert_many"] >= 1
    assert calls["bulk_write"] == 0, "frames must not be turned into replaces"
    assert archive.frames.count_documents({"run_id": "fr"}) == 3


# ── regressions: the summary owns the code books ────────────────────────────


def test_dump_run_itself_writes_the_code_books_from_run_meta(archive, duck):
    """No service-side augmentation: dump_run probes run_meta and folds it in.

    Regression: the code books were bolted on by DataplaneService.dump_run, which the
    reconcile sweep never reaches — so the sweep's replace_one blanked them, worst
    precisely in the missed-trigger case the reconciler exists for.
    """
    run_id = "codes"
    duck.write_frames(run_id, [make_frame(2, frame_idx=0)])
    duck.write_kpi_events([(run_id, "empty_km", 1.0, T)])
    duck.upsert_run_meta(
        run_id,
        status="COMPLETED",
        n_trucks=2,
        max_frame_idx=0,
        haulier_codes='{"ACME": 1}',
        slot_map='{"t1": 0, "t2": 1}',
    )

    archive.dump_run(duck, run_id, reason="COMPLETED")
    summary = archive.run_summary(run_id)
    assert summary["haulier_codes"] == '{"ACME": 1}'
    assert summary["slot_map"] == '{"t1": 0, "t2": 1}'
    assert summary["n_trucks"] == 2
    assert summary["status"] == "COMPLETED"


def test_a_later_sweep_never_blanks_a_summary_key_it_did_not_compute(archive, duck):
    run_id = "codes_kept"
    duck.write_frames(run_id, [make_frame(2, frame_idx=0)])
    duck.write_kpi_events([(run_id, "empty_km", 1.0, T)])
    duck.upsert_run_meta(
        run_id,
        status="COMPLETED",
        n_trucks=2,
        haulier_codes='{"ACME": 1}',
        slot_map='{"t1": 0, "t2": 1}',
    )
    archive.dump_run(duck, run_id, reason="COMPLETED")
    # a key written by some other writer must also survive the sweep
    archive.runs.update_one({"_id": run_id}, {"$set": {"foreign_key": "keep me"}})

    # one late redelivered kpi row for the finished run -> the sweep re-dumps it
    duck.write_kpi_events([(run_id, "late", 2.0, T + timedelta(minutes=1))])
    assert archive.reconcile(duck, reason="sweep")["dumped"] == 1

    summary = archive.run_summary(run_id)
    assert summary["haulier_codes"] == '{"ACME": 1}'
    assert summary["slot_map"] == '{"t1": 0, "t2": 1}'
    assert summary["n_trucks"] == 2
    assert summary["foreign_key"] == "keep me"
    assert summary["kpi_count"] == 2


def test_a_reader_without_run_meta_still_dumps(archive):
    """The code-book probe is optional: FakeReader has no get_run_meta."""
    reader = FakeReader({"nometa": {"frames": [make_frame(3, frame_idx=0)], "kpi": []}})
    archive.dump_run(reader, "nometa", reason="unit")
    summary = archive.run_summary("nometa")
    assert summary["frame_count"] == 1
    assert "haulier_codes" not in summary


# ── regressions: has_run must mean "the archive holds data" ─────────────────


def test_has_run_is_false_when_only_a_summary_document_exists(archive, duck):
    """rehydrate_run uses has_run to decide it may DELETE. A summary is not data.

    Regression: a kpi-only headless run was cleared against a source holding nothing,
    because dump_run writes a summary for every swept run, even an empty one.
    """
    duck.upsert_run_meta("summary_only", status="COMPLETED")
    archive.dump_run(duck, "summary_only", reason="COMPLETED")
    assert archive.run_summary("summary_only") is not None
    assert archive.frames.count_documents({"run_id": "summary_only"}) == 0
    assert archive.has_run("summary_only") is False


def test_has_run_is_true_for_a_kpi_only_headless_run(archive, duck):
    duck.write_kpi_events([("headless", "empty_km", 1.0, T)])
    duck.upsert_run_meta("headless", status="COMPLETED")
    archive.dump_run(duck, "headless", reason="COMPLETED")
    assert archive.has_run("headless") is True


# ── regressions: a FAILED breakdown read must never look like an EMPTY one ──
#
# ROUND-3 ROOT 4. ``_read_breakdown_rows`` swallowed every exception and returned None,
# and ``dump_run`` read None as "there were none": it wrote complete=True plus a
# ``breakdown_max_ingested_at`` watermark taken from DuckDB, so 34 689 rows that were
# never archived were recorded as durable and ``archived_run_ids()`` reported the run
# CLOSED. These run against the REAL DuckStore — the failure only exists in the
# interaction between the store's reader and the dump's watermark logic.


def _exploding_store(path):
    """A REAL DuckStore whose ``SELECT *`` on kpi_breakdown_rows raises.

    ``count(*)`` and ``max(ingested_at)`` keep working — a DuckDB transient, an OOM
    materialising 34 689 rows, or schema drift all look exactly like this. The store has
    no ``read_breakdown_rows``, so the archive reaches the table through ``query``.
    """
    from apps.dataplane.store.duck import DuckStore

    class BreakdownReadExplodes(DuckStore):
        broken = True

        def query(self, sql, params=None):
            if self.broken and "kpi_breakdown_rows" in sql and sql.strip().upper().startswith("SELECT *"):
                raise RuntimeError("OOM materialising kpi_breakdown_rows")
            return super().query(sql, params)

    return BreakdownReadExplodes(str(path))


def _fill_breakdown(store, run_id, count=200, status="COMPLETED"):
    store.write_breakdown(
        run_id, "truck", T, True, [_entity(f"truck_{i}", float(i), i) for i in range(count)]
    )
    store.write_kpi_events([(run_id, "empty_km", 1.0, T)])
    store.upsert_run_meta(run_id, status=status)


def test_a_failed_breakdown_read_leaves_the_run_open_and_tells_the_caller(archive, tmp_path):
    store = _exploding_store(tmp_path / "boom.duckdb")
    run_id = "bd_lost"
    try:
        _fill_breakdown(store, run_id, 200)
        assert store.breakdown_row_count(run_id) == 200

        with pytest.raises(ArchiveIncomplete) as excinfo:
            archive.dump_run(store, run_id, reason="COMPLETED")

        # (iii) the result is still available, and says what went wrong
        assert excinfo.value.result["breakdown_read_failed"] is True
        assert excinfo.value.result["complete"] is False

        summary = archive.run_summary(run_id)
        # (iv) the summary IS written — frames and kpi really were archived ...
        assert summary is not None
        assert summary["kpi_count"] == 1
        # (i) ... but OPEN, so the next sweep comes back for it
        assert summary["complete"] is False
        assert summary["breakdown_read_failed"] is True
        # (ii) and no content watermark claims the unread rows are durable
        assert "breakdown_max_ingested_at" not in summary
        # nothing may report this run as archived
        assert archive.archived_run_ids() == set()
        assert archive.breakdown.count_documents({"run_id": run_id}) == 0
        assert store.breakdown_row_count(run_id) == 200
    finally:
        store.close()


def test_an_explicit_complete_flag_cannot_close_a_run_whose_breakdown_read_failed(archive, tmp_path):
    """Even ``complete=True`` from the caller loses to an unknown table."""
    store = _exploding_store(tmp_path / "forced.duckdb")
    try:
        _fill_breakdown(store, "forced", 20)
        with pytest.raises(ArchiveIncomplete):
            archive.dump_run(store, "forced", reason="manual", complete=True)
        assert archive.run_summary("forced")["complete"] is False
        assert archive.archived_run_ids() == set()
    finally:
        store.close()


def test_reconcile_counts_a_failed_breakdown_read_and_retries_it_until_it_succeeds(archive, tmp_path):
    store = _exploding_store(tmp_path / "sweep.duckdb")
    run_id = "heals"
    try:
        _fill_breakdown(store, run_id, 200)

        first = archive.reconcile(store, reason="sweep")
        assert first["dumped"] == 0
        assert run_id in first["errors"] and "ArchiveIncomplete" in first["errors"][run_id]
        assert first["pending_run_ids"] == [run_id]
        assert archive.archived_run_ids() == set()

        # the transient clears; the OPEN summary is what brings the sweep back
        store.broken = False
        second = archive.reconcile(store, reason="sweep")
        assert second["dumped"] == 1
        assert second["errors"] == {}
        assert archive.breakdown.count_documents({"run_id": run_id, "doc_type": "row"}) == 200
        summary = archive.run_summary(run_id)
        assert summary["complete"] is True
        assert summary["breakdown_read_failed"] is False, "a success must clear the flag"
        assert summary["breakdown_max_ingested_at"]
        assert archive.archived_run_ids() == {run_id}
        assert archive.reconcile(store, reason="sweep")["dumped"] == 0
    finally:
        store.close()


def test_a_reader_that_answers_zero_breakdown_rows_still_closes_the_run(archive, duck):
    """``[]`` is a real answer. Only a RAISE is unknown — do not collapse the two."""
    duck.write_kpi_events([("no_bd", "empty_km", 1.0, T)])
    duck.upsert_run_meta("no_bd", status="COMPLETED")
    result = archive.dump_run(duck, "no_bd", reason="COMPLETED")
    assert result["breakdown_rows"] == 0
    assert result["breakdown_read_failed"] is False
    assert result["complete"] is True
    assert archive.run_summary("no_bd")["complete"] is True
    assert archive.archived_run_ids() == {"no_bd"}


def test_a_reader_without_the_breakdown_capability_still_closes_the_run(archive):
    """``None`` (no capability) is harmless and must keep behaving as it always did."""
    reader = FakeReader({"nocap": {"frames": [make_frame(3, frame_idx=0)], "kpi": kpi_rows("nocap", 1)}})
    result = archive.dump_run(reader, "nocap", reason="unit")
    assert result["breakdown_rows"] == 0
    assert result["breakdown_read_failed"] is False
    assert result["complete"] is True
    assert archive.archived_run_ids() == {"nocap"}


def test_read_breakdown_rows_distinguishes_all_three_outcomes():
    """The probe itself: None / [] / raise. No Mongo needed."""

    class NoCapability:
        pass

    class Empty:
        def read_breakdown_rows(self, run_id):
            return []

    class Boom:
        def read_breakdown_rows(self, run_id):
            raise RuntimeError("transient")

    assert MongoArchive._read_breakdown_rows(NoCapability(), "r") is None
    assert MongoArchive._read_breakdown_rows(Empty(), "r") == []
    with pytest.raises(BreakdownReadFailed):
        MongoArchive._read_breakdown_rows(Boom(), "r")


def test_summary_carries_status_and_first_seen_for_the_rehydrate(archive, duck):
    """``rehydrate_run`` restores the terminal marker FROM the summary, so it must be there.

    Without ``status`` a recovered finished run re-opens as live: ``run_is_terminal`` says
    False, the next sweep flips the durable ``complete=True`` back to False, and the run
    can never be re-closed.
    """
    run_id = "meta"
    first_seen = datetime(2026, 8, 5, 6, 0, 0)
    duck.write_kpi_events([(run_id, "empty_km", 1.0, T)])
    duck.upsert_run_meta(run_id, status="COMPLETED", first_seen=first_seen, n_trucks=7)

    archive.dump_run(duck, run_id, reason="COMPLETED")
    summary = archive.run_summary(run_id)
    assert summary["status"] == "COMPLETED"
    assert summary["first_seen"] == first_seen
    assert summary["n_trucks"] == 7


# ── the columnar read side the fast rehydrate consumes ──────────────────────


def test_read_run_frame_docs_yields_raw_binary_columns_in_order(archive, duck):
    frames = [make_frame(50, frame_idx=i, sim_time_ms=float(i * 100), seed=i) for i in (3, 0, 2, 1)]
    duck.write_frames("raw", frames)
    archive.dump_run(duck, "raw", reason="unit", complete=True)

    docs = list(archive.read_run_frame_docs("raw"))
    assert [d["frame_idx"] for d in docs] == [0, 1, 2, 3]
    assert [d["n"] for d in docs] == [50] * 4
    # zero-copy view straight off the Binary, no Frame object in between
    lng = np.concatenate([np.frombuffer(d["lng"], dtype="<f8") for d in docs])
    state = np.concatenate([np.frombuffer(d["state"], dtype=np.uint8) for d in docs])
    expected = sorted(frames, key=lambda f: f.frame_idx)
    assert np.array_equal(lng, np.concatenate([f.lng for f in expected]))
    assert np.array_equal(state, np.concatenate([f.state for f in expected]))
    assert np.concatenate([np.frombuffer(d["lng"], dtype="<f8") for d in docs]).flags.writeable


def test_read_run_kpi_columns(archive, duck):
    rows = [("kcol", f"m{i % 3}", float(i), T + timedelta(minutes=i)) for i in range(12)]
    duck.write_kpi_events(rows)
    duck.upsert_run_meta("kcol", status="COMPLETED")
    archive.dump_run(duck, "kcol", reason="COMPLETED")

    cols = archive.read_run_kpi_columns("kcol")
    assert set(cols) == {"metric", "value", "sim_clock", "ingested_at", "_seq"}
    assert cols["value"].dtype == np.float64
    assert cols["sim_clock"].dtype == np.dtype("datetime64[us]")
    assert cols["ingested_at"].dtype == np.dtype("datetime64[us]")
    assert cols["metric"].dtype == object
    assert len(cols["value"]) == 12
    assert sorted(cols["value"].tolist()) == [float(i) for i in range(12)]
    assert not np.isnat(cols["ingested_at"]).any(), "a null watermark column breaks the sweep"
    assert cols["_seq"].tolist() == list(range(12))

    empty = archive.read_run_kpi_columns("never_seen")
    assert len(empty["value"]) == 0 and empty["sim_clock"].dtype == np.dtype("datetime64[us]")


def test_read_run_breakdown_columns_agrees_with_the_row_reader(archive, duck):
    run_id = "bdcol"
    duck.write_breakdown(
        run_id, "truck", T, False, [_entity(f"truck_{i}", float(i), i) for i in range(40)]
    )
    duck.write_breakdown(run_id, "haulier", T, True, [_entity("ACME", 873.4, 412)])
    duck.upsert_run_meta(run_id, status="COMPLETED")
    archive.dump_run(duck, run_id, reason="COMPLETED")

    rows = list(archive.read_run_breakdown_rows(run_id))
    cols = archive.read_run_breakdown_columns(run_id)
    assert cols is not None
    assert "run_id" not in cols, "run_id is bound as a parameter, never materialised per row"
    assert len(cols["scope"]) == len(rows) == 41
    assert cols["sim_clock"].dtype == np.dtype("datetime64[us]")
    assert cols["ingested_at"].dtype == np.dtype("datetime64[us]")
    assert cols["final"].dtype == np.bool_
    assert cols["empty_km"].dtype == np.float64
    assert cols["scope"].dtype == object

    by_entity = {e: i for i, e in enumerate(cols["entity_id"].tolist())}
    for row in rows:
        i = by_entity[row["entity_id"]]
        assert cols["scope"][i] == row["scope"]
        assert cols["empty_km"][i] == row["empty_km"]
        assert bool(cols["final"][i]) is bool(row["final"])
        assert cols["num_orders_completed"][i] == float(row["num_orders_completed"])
    assert cols["empty_km"][by_entity["ACME"]] == 873.4
    assert bool(cols["final"][by_entity["ACME"]]) is True


def test_breakdown_columns_are_none_only_when_the_read_failed(archive, duck, monkeypatch):
    """Empty is a dict; failure is None. A caller must not delete rows it cannot replace."""
    duck.write_kpi_events([("bdnone", "empty_km", 1.0, T)])
    duck.upsert_run_meta("bdnone", status="COMPLETED")
    archive.dump_run(duck, "bdnone", reason="COMPLETED")

    empty = archive.read_run_breakdown_columns("bdnone")
    assert empty is not None, "zero rows is an ANSWER, not a failure"
    assert len(empty["scope"]) == 0
    assert empty["sim_clock"].dtype == np.dtype("datetime64[us]")

    from pymongo.errors import OperationFailure

    def boom(*a, **kw):
        raise OperationFailure("cursor died")

    monkeypatch.setattr(archive.breakdown, "find", boom)
    assert archive.read_run_breakdown_columns("bdnone") is None


def test_breakdown_columns_carry_nulls_as_nan(archive):
    """``None`` -> ``nan`` -> SQL NULL, which is what the row path produced too."""
    row = {
        "run_id": "nulls",
        "scope": "truck",
        "sim_clock": T,
        "final": False,
        "entity_id": "t0",
        "haulier_id": None,
        "haulier_name": None,
        "empty_km": None,
        "num_orders_completed": None,
        "payload": None,
    }
    archive.write_breakdown_rows("nulls", [row])
    cols = archive.read_run_breakdown_columns("nulls")
    assert np.isnan(cols["empty_km"][0])
    assert np.isnan(cols["num_orders_completed"][0])
    assert cols["haulier_id"][0] is None
    assert cols["payload"][0] is None


def test_breakdown_column_spec_matches_the_real_duckdb_table(duck):
    """The archive declares the columns itself (independence) — this stops it drifting.

    Checked against the LIVE table, not against a constant in the store module, so a
    schema change is caught even if the store renames its own copy of the list.
    """
    from apps.dataplane.archive.mongo import (
        _BREAKDOWN_BOOL_COLUMNS,
        _BREAKDOWN_COLUMNS,
        _BREAKDOWN_NUM_COLUMNS,
        _BREAKDOWN_STR_COLUMNS,
        _BREAKDOWN_TS_COLUMNS,
    )

    live = [r["name"] for r in duck.query("PRAGMA table_info('kpi_breakdown_rows')")]
    assert list(_BREAKDOWN_COLUMNS) == live
    split = set(
        _BREAKDOWN_STR_COLUMNS + _BREAKDOWN_TS_COLUMNS + _BREAKDOWN_BOOL_COLUMNS + _BREAKDOWN_NUM_COLUMNS
    )
    assert split == set(_BREAKDOWN_COLUMNS) - {"run_id"}


def test_columnar_read_reloads_a_run_into_a_fresh_duckdb(archive, duck, tmp_path):
    """End to end with real objects: DuckStore -> Mongo -> columns -> a fresh DuckDB.

    This is the shape the fast rehydrate uses (register + INSERT .. SELECT with run_id
    bound once), proved against the real archive rather than asserted.
    """
    from apps.dataplane.store.duck import DuckStore

    run_id = "reload"
    duck.write_frames(run_id, [make_frame(60, frame_idx=i, sim_time_ms=float(i), seed=i) for i in range(8)])
    duck.write_kpi_events([(run_id, f"m{i % 4}", float(i), T + timedelta(minutes=i)) for i in range(30)])
    duck.write_breakdown(
        run_id, "truck", T, True, [_entity(f"truck_{i}", float(i), i) for i in range(25)]
    )
    duck.upsert_run_meta(run_id, status="COMPLETED")
    archive.dump_run(duck, run_id, reason="COMPLETED")

    target = DuckStore(str(tmp_path / "target.duckdb"))  # a REAL store, real schema
    con = target._cursor()
    try:
        docs = list(archive.read_run_frame_docs(run_id))
        fcols = {
            "frame_idx": np.concatenate([np.full(int(d["n"]), int(d["frame_idx"]), np.uint32) for d in docs]),
            "sim_time_ms": np.concatenate(
                [np.full(int(d["n"]), float(d["sim_time_ms"]), np.float64) for d in docs]
            ),
            "slot": np.concatenate([np.frombuffer(d["slot"], dtype="<u4") for d in docs]),
            "lng": np.concatenate([np.frombuffer(d["lng"], dtype="<f8") for d in docs]),
            "lat": np.concatenate([np.frombuffer(d["lat"], dtype="<f8") for d in docs]),
            "state": np.concatenate([np.frombuffer(d["state"], dtype=np.uint8) for d in docs]),
            "haulier": np.concatenate([np.frombuffer(d["haulier"], dtype=np.uint8) for d in docs]),
        }
        con.register("dp_frames", fcols)
        con.execute(
            "INSERT INTO frames (run_id, frame_idx, sim_time_ms, slot, lng, lat, state, haulier) "
            "SELECT ?, frame_idx, sim_time_ms, slot, lng, lat, state, haulier FROM dp_frames",
            [run_id],
        )
        con.unregister("dp_frames")

        kcols = archive.read_run_kpi_columns(run_id)
        con.register("dp_kpi", kcols)
        con.execute(
            "INSERT INTO kpi_events (run_id, metric, value, sim_clock, ingested_at) "
            "SELECT ?, metric, value, sim_clock, ingested_at FROM dp_kpi "
            "QUALIFY row_number() OVER (PARTITION BY metric, sim_clock ORDER BY _seq DESC) = 1",
            [run_id],
        )
        con.unregister("dp_kpi")

        bcols = archive.read_run_breakdown_columns(run_id)
        names = [c for c in bcols if c != "_seq"]
        con.register("dp_bd", bcols)
        con.execute(
            f"INSERT INTO kpi_breakdown_rows (run_id, {', '.join(names)}) "
            f"SELECT ?, {', '.join(names)} FROM dp_bd "
            "QUALIFY row_number() OVER "
            "(PARTITION BY scope, sim_clock, entity_id ORDER BY _seq DESC) = 1",
            [run_id],
        )
        con.unregister("dp_bd")

        assert con.execute("SELECT count(*) FROM frames").fetchone()[0] == 8 * 60
        assert con.execute("SELECT count(*) FROM kpi_events").fetchone()[0] == 30
        assert con.execute("SELECT count(*) FROM kpi_breakdown_rows").fetchone()[0] == 25
        # no NULL timestamps: the content watermark must stay answerable after a rehydrate
        assert con.execute(
            "SELECT count(*) FROM kpi_breakdown_rows WHERE ingested_at IS NULL"
        ).fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM kpi_events WHERE ingested_at IS NULL").fetchone()[0] == 0
        # values survive the round trip unchanged
        assert con.execute(
            "SELECT empty_km, num_orders_completed, final FROM kpi_breakdown_rows "
            "WHERE entity_id = 'truck_7'"
        ).fetchone() == (7.0, 7, True)
        got = con.execute(
            "SELECT lng, lat, slot, state, haulier FROM frames WHERE frame_idx = 3 ORDER BY slot"
        ).fetchall()
        want = duck.read_frames(run_id, 3, 3)
        assert [r[0] for r in got] == want["lng"].tolist()
        assert [r[3] for r in got] == want["state"].tolist()
    finally:
        target.close()


def test_columnar_readers_survive_a_tz_aware_client(mongo_client, archive, duck):
    """Mongo hands back tz-AWARE datetimes when the client is; numpy warns today and will
    raise tomorrow, and the value would land under a different primary key."""
    import warnings

    from pymongo import MongoClient

    run_id = "tzaware"
    duck.write_breakdown(run_id, "haulier", T, True, [_entity("ACME", 1.0, 1)])
    duck.write_kpi_events([(run_id, "empty_km", 1.0, T)])
    duck.upsert_run_meta(run_id, status="COMPLETED")
    archive.dump_run(duck, run_id, reason="COMPLETED")

    tz_client = MongoClient(
        *(mongo_client.address or ("localhost", 27017)),
        serverSelectionTimeoutMS=1000,
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    try:
        tz_archive = MongoArchive(client=tz_client, db_name=archive.db_name)
        raw = tz_archive.breakdown.find_one({"run_id": run_id, "doc_type": "row"})
        assert raw["sim_clock"].tzinfo is not None, "the client really is tz-aware"

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cols = tz_archive.read_run_breakdown_columns(run_id)
            kcols = tz_archive.read_run_kpi_columns(run_id)
        assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []
        assert cols["sim_clock"][0] == np.datetime64(T, "us")
        assert kcols["sim_clock"][0] == np.datetime64(T, "us")
    finally:
        tz_client.close()


def test_concurrent_sweeps_of_the_same_run_produce_no_duplicates(archive):
    """Race two reconcilers: deterministic _ids, so the loser writes nothing extra."""
    import threading

    duck = FakeDuckLike()
    run_id = "raced"
    duck.frames[run_id] = [make_frame(20, frame_idx=i, seed=i) for i in range(40)]
    duck.kpi[run_id] = kpi_rows(run_id, 30)
    duck.breakdown[run_id] = breakdown_rows(run_id, 40)

    start = threading.Barrier(4)
    errors: List[str] = []

    def sweep():
        start.wait(timeout=10)
        try:
            archive.reconcile(duck, reason="race")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=sweep) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert archive.frames.count_documents({"run_id": run_id}) == 40
    assert archive.kpi.count_documents({"run_id": run_id}) == 30
    assert archive.breakdown.count_documents({"run_id": run_id, "doc_type": "row"}) == 40
    assert archive.runs.count_documents({"_id": run_id}) == 1
