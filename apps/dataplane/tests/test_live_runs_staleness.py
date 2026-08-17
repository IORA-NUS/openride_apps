"""`/runs/live` must not advertise a run that died without publishing a terminal status.

A run killed with SIGKILL (or whose host died) never emits a terminal ``run_status``, so the
non-terminal filter alone kept it "in flight" forever. That is not cosmetic: the frontend
treats a listed run as live, which suppresses the historical replay controls and latches
newest-run discovery onto a corpse. Observed 2026-08-12 — `run_20260812_170144` was killed at
17:01 and was still being served by `/runs/live` at 18:01, costing two soak attempts.

The row that actually gets stuck is the awkward one: it had ``last_seen`` AND ``first_seen``
both NULL, so a `last_seen`-only rule fails open on precisely the case it was written for.
Hence the run-id fallback. Every ambiguous case must fail OPEN — hiding a live run is worse
than listing a dead one.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from apps.dataplane.read_api import (
    LIVE_RUN_STALE_AFTER_S,
    _live_row_is_stale,
    _run_id_start_ms,
    live_runs,
)

NOW = 1_786_540_000_000.0
OLD = NOW - (LIVE_RUN_STALE_AFTER_S * 1000.0) - 60_000.0
RECENT = NOW - 30_000.0


def test_wall_clock_last_seen_ages_out():
    assert _live_row_is_stale(OLD, now_ms=NOW, run_id="run_20260812_170144") is True
    assert _live_row_is_stale(RECENT, now_ms=NOW, run_id="run_20260812_170144") is False


def test_last_seen_arrives_as_a_datetime_and_must_still_be_honoured():
    """The regression that shipped: `run_meta.last_seen` is TIMESTAMP (`store/duck.py:194`,
    written as `datetime.utcnow()` at `:1365`), so DuckDB returns a **datetime**. `float(dt)`
    raises, the except swallowed it, and every real row fell through to the run-id fallback —
    which ages out on START time. A run publishing normally but running longer than the cutoff
    was then hidden from `/runs/live` mid-flight, flipping `isLiveRun` false and rendering an
    in-flight run as historical. The original six tests all passed floats, so none of them
    could see it.
    """
    live_dt = datetime.utcfromtimestamp(RECENT / 1000.0)
    stale_dt = datetime.utcfromtimestamp(OLD / 1000.0)
    old_id = "run_20260812_170144"  # id far older than the cutoff — the fallback would fire

    # Actively publishing, old id: last activity must win over the id timestamp.
    assert _live_row_is_stale(live_dt, now_ms=NOW, run_id=old_id) is False
    # Genuinely silent past the cutoff, same id: still ages out.
    assert _live_row_is_stale(stale_dt, now_ms=NOW, run_id=old_id) is True
    # tz-aware is handled identically to naive-UTC.
    aware = datetime.fromtimestamp(RECENT / 1000.0, tz=timezone.utc)
    assert _live_row_is_stale(aware, now_ms=NOW, run_id=old_id) is False


def test_a_long_running_but_live_run_is_not_hidden():
    """A 1000-truck run took 452 s wall; contention or more demand pushes past the 600 s
    cutoff. Its run-id start time is then older than the cutoff while it is still publishing."""
    started_ms = NOW - (LIVE_RUN_STALE_AFTER_S * 1000.0) - 300_000.0
    run_id = "run_" + datetime.utcfromtimestamp(started_ms / 1000.0).strftime("%Y%m%d_%H%M%S")
    publishing_now = datetime.utcfromtimestamp((NOW - 2_000.0) / 1000.0)
    assert _live_row_is_stale(publishing_now, now_ms=NOW, run_id=run_id) is False


def test_null_last_seen_falls_back_to_the_run_id_timestamp():
    """The regression case: both timestamps NULL, so the id is the only evidence."""
    assert _live_row_is_stale(None, now_ms=NOW, run_id="run_20260812_170144") is True
    fresh_id = time.strftime("run_%Y%m%d_%H%M%S")
    assert _live_row_is_stale(None, now_ms=time.time() * 1000.0, run_id=fresh_id) is False


def test_sim_valued_last_seen_is_not_treated_as_ancient():
    """Some rows carry a 2020-01-01 SIM epoch. Ageing that out arithmetically would hide a
    genuinely live run, so it must fail open."""
    sim_epoch = 1_577_865_600_000
    assert _live_row_is_stale(sim_epoch, now_ms=NOW, run_id="run_20260812_170144") is False


def test_unparseable_or_missing_evidence_fails_open():
    for run_id in ("run_collab_gate_a3", "", None, "not-a-run"):
        assert _live_row_is_stale(None, now_ms=NOW, run_id=run_id) is False
    assert _live_row_is_stale("", now_ms=NOW, run_id=None) is False
    # A future timestamp is clock skew, not staleness.
    assert _live_row_is_stale(NOW + 60_000.0, now_ms=NOW, run_id="run_20260812_170144") is False


def test_run_id_timestamp_parsing_tolerates_a_name_prefix():
    assert _run_id_start_ms("run_verify_20260617_061251") is not None
    assert _run_id_start_ms("run_20260812_170144") is not None
    assert _run_id_start_ms("run_collab_gate_a3") is None
    assert _run_id_start_ms("run_20261332_999999") is None  # invalid calendar date


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_args, **_kwargs):
        return self._rows


def test_live_runs_drops_the_dead_row_and_keeps_the_live_one():
    rows = [
        # killed: non-terminal, no timestamps at all — the observed failure
        {"run_id": "run_20260812_170144", "run_name": "killtest", "scenario_slug": "s",
         "scenario_name": "S", "status": None, "last_seen": None},
        # genuinely in flight
        {"run_id": "run_20260812_170145", "run_name": "live", "scenario_slug": "s",
         "scenario_name": "S", "status": None, "last_seen": time.time() * 1000.0},
    ]
    out = live_runs(_FakeStore(rows))
    ids = [r["runId"] for r in out["runs"]]
    assert "run_20260812_170144" not in ids
    assert "run_20260812_170145" in ids
