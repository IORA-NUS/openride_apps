"""Store-level regressions for three ways a single write could wedge the dataplane.

All three use the REAL :class:`DuckStore`; none of them stubs the insert path.

1. An all-NULL VARCHAR column in a >= 2 000-row batch — the real haulier-scope payload
   shape (``AnalyticsManager`` builds haulier entities with no ``haulier_id``, measured as
   243/243 NULL in both surviving production runs).  duckdb's pandas analyzer samples
   1 000 values and, on a bare object ndarray, reached for ``first_valid_index()``.  No
   other test in the suite builds a batch over 1 000 rows, so the threshold was never hit.
2. A JSON number outside INT64 in a BIGINT metric column.  ``orjson`` parses ``1e30`` and
   widens any integer over 2**63 to a float, so it reaches the INSERT as a finite float64
   and only failed on the cast.
3. A rehydrate whose breakdown reader cannot answer while the working set is empty.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from apps.dataplane.contract.frame import Frame
from apps.dataplane.store.duck import _BD_INT, DuckStore, DuckStoreError

T0 = datetime(2026, 8, 5, 12, 0, 0)


@pytest.fixture()
def store(tmp_path):
    s = DuckStore(tmp_path / "dataplane.duckdb")
    try:
        yield s
    finally:
        s.close()


# ── 1. all-NULL VARCHAR over duckdb's 1 000-row analyze sample ───────────────


def _haulier_entities(n, offset=0):
    """Haulier-scope entities exactly as ``AnalyticsManager._new_haulier_acc`` builds them:
    there is no ``haulier_id`` key at all, so the column is NULL for every row."""
    return [
        {
            "id": f"h{offset + i}", "haulier_name": f"Haulier {offset + i}",
            "num_trucks": 40, "num_orders_completed": 12, "empty_km": 100.0 + i,
            "loaded_km": 200.0, "total_km": 300.0, "empty_ratio": 0.33,
        }
        for i in range(n)
    ]


def test_all_null_varchar_column_writes_at_the_haulier_scope_batch_size(store):
    """2 040 rows (34 snapshots x 60 hauliers) with haulier_id NULL on every one."""
    batch = [
        ("haulier", T0.replace(minute=s), False, _haulier_entities(60))
        for s in range(34)
    ]
    assert store.write_breakdown_batch("RNULL", batch) == 2040
    rows = store.query(
        "SELECT count(*) AS n, count(haulier_id) AS non_null, count(haulier_name) AS named "
        "FROM kpi_breakdown_rows WHERE run_id = 'RNULL'"
    )
    assert rows[0]["n"] == 2040
    assert rows[0]["non_null"] == 0      # NULLs preserved, not turned into a sentinel
    assert rows[0]["named"] == 2040      # the populated VARCHAR beside it is intact


def test_every_varchar_column_may_be_null_across_the_whole_batch(store):
    """The 1 000-value sample is strided, so "one valid value somewhere" is not a fix."""
    entities = [{"id": f"e{i}"} for i in range(2500)]
    assert store.write_breakdown_batch("RALLNULL", [("truck", T0, True, entities)]) == 2500
    row = store.query(
        "SELECT count(*) AS n, count(haulier_id) AS h, count(haulier_name) AS hn "
        "FROM kpi_breakdown_rows WHERE run_id = 'RALLNULL'"
    )[0]
    assert (row["n"], row["h"], row["hn"]) == (2500, 0, 0)


def test_a_frame_batch_over_the_analyze_threshold_still_writes(store):
    """The frames path shares ``_insert_columnar``; 3 000 rows must not regress."""
    rng = np.random.default_rng(0)
    frames = [
        Frame(
            frame_idx=i, sim_time_ms=float(i) * 1000.0,
            lng=rng.uniform(3.0, 7.5, 500), lat=rng.uniform(50.5, 53.5, 500),
            slot=np.arange(500, dtype=np.uint32),
            state=np.zeros(500, dtype=np.uint8), haulier=np.zeros(500, dtype=np.uint8),
        )
        for i in range(6)
    ]
    assert store.write_frames("RF", frames) == 3000
    assert store.run_frame_count("RF") == 6


# ── 2. a JSON number the BIGINT columns cannot hold ──────────────────────────


@pytest.mark.parametrize("column", _BD_INT)
@pytest.mark.parametrize("value", [1e30, -1e30, 9.3e18, float("inf"), 1.2345678901234568e29])
def test_an_out_of_range_count_lands_as_null_instead_of_wedging_the_write(
    store, column, value
):
    entity = {"id": "T1", column: value, "empty_km": 5.0}
    run = f"ROV_{column}_{abs(hash(value))}"
    assert store.write_breakdown(run, "truck", T0, False, [entity]) == 1
    row = store.query(
        f"SELECT {column} AS v, empty_km FROM kpi_breakdown_rows WHERE run_id = '{run}'"
    )[0]
    assert row["v"] is None            # unrepresentable -> NULL, exactly like None
    assert row["empty_km"] == 5.0      # and the rest of the row survives


def test_in_range_counts_still_round_the_way_the_cast_did(store):
    entity = {
        "id": "T1", "num_orders_completed": 3.7, "num_trucks": 9.2e18,
        "dual_cycle_count": 0, "chain_opportunities": None,
    }
    assert store.write_breakdown("RROUND", "truck", T0, False, [entity]) == 1
    row = store.query("SELECT * FROM kpi_breakdown_rows WHERE run_id = 'RROUND'")[0]
    assert row["num_orders_completed"] == 4
    assert row["num_trucks"] == 9200000000000000000
    assert row["dual_cycle_count"] == 0
    assert row["chain_opportunities"] is None


def test_one_poisoned_entity_does_not_destroy_its_healthy_neighbours(store):
    entities = [{"id": f"t{i}", "num_trucks": i} for i in range(5)]
    entities[2]["num_trucks"] = 1e30
    assert store.write_breakdown("RMIX", "truck", T0, False, entities) == 5
    got = {
        r["entity_id"]: r["num_trucks"]
        for r in store.query(
            "SELECT entity_id, num_trucks FROM kpi_breakdown_rows WHERE run_id = 'RMIX'"
        )
    }
    assert got == {"t0": 0, "t1": 1, "t2": None, "t3": 3, "t4": 4}


# ── 3. an unanswerable breakdown reader against an empty working set ─────────


class _Source:
    """A frame source shaped like ``MongoArchive``: it HAS the breakdown capability."""

    def __init__(self, frames, *, breakdown_answer):
        self._frames = list(frames)
        self._breakdown_answer = breakdown_answer

    def has_run(self, run_id):
        return True

    def read_run_frames(self, run_id):
        return iter(self._frames)

    def read_run_kpi_events(self, run_id):
        return iter([(run_id, "empty_km", 1.0, T0)])

    def read_run_breakdown_columns(self, run_id):
        # ``MongoArchive.read_run_breakdown_columns`` returns None on ANY exception.
        return self._breakdown_answer

    def run_summary(self, run_id):
        return {"complete": True, "status": "completed", "kpi_count": 1, "frame_count": 1}


def _bd_columns(n):
    from apps.dataplane.store.duck import _BD_COLUMN_SPEC

    cols = {col: [None] * n for col, _kind in _BD_COLUMN_SPEC}
    cols["scope"] = ["haulier"] * n
    cols["sim_clock"] = [T0] * n
    cols["entity_id"] = [f"h{i}" for i in range(n)]
    return cols


def test_a_blip_on_the_breakdown_read_aborts_instead_of_half_restoring(store):
    """One Mongo blip must not leave a completed/exported run with zero breakdown rows."""
    frames = [
        Frame(
            frame_idx=0, sim_time_ms=0.0, lng=np.zeros(3), lat=np.zeros(3),
            slot=np.arange(3, dtype=np.uint32),
            state=np.zeros(3, dtype=np.uint8), haulier=np.zeros(3, dtype=np.uint8),
        )
    ]
    with pytest.raises(DuckStoreError) as exc:
        store.rehydrate_run("RB", _Source(frames, breakdown_answer=None))
    assert "breakdown" in str(exc.value)

    # PHASE 1 aborted: DuckDB is byte-identical, so nothing looks restored...
    assert store.run_frame_count("RB") == 0
    assert store.kpi_row_count("RB") == 0
    assert store.breakdown_row_count("RB") == 0
    assert store.get_run_meta("RB") is None
    assert store.get_export_state("RB") is None

    # ...and because nothing was written, the PHASE 0 guard lets the retry through.
    assert store.rehydrate_run("RB", _Source(frames, breakdown_answer=_bd_columns(4))) == 1
    assert store.breakdown_row_count("RB") == 4
    assert store.get_run_meta("RB")["status"] == "completed"
    assert store.get_export_state("RB")["status"] == "exported"


def test_an_unanswerable_reader_is_still_tolerated_when_the_rows_are_already_there(store):
    """The original invariant is untouched: a source that cannot replace rows that DO
    exist must never destroy them, and that rehydrate still completes."""
    frames = [
        Frame(
            frame_idx=0, sim_time_ms=0.0, lng=np.zeros(3), lat=np.zeros(3),
            slot=np.arange(3, dtype=np.uint32),
            state=np.zeros(3, dtype=np.uint8), haulier=np.zeros(3, dtype=np.uint8),
        )
    ]
    store.write_breakdown("RK", "truck", T0, True, [{"id": "t0", "empty_km": 7.0}])
    assert store.rehydrate_run("RK", _Source(frames, breakdown_answer=None), force=True) == 1
    assert store.breakdown_row_count("RK") == 1
