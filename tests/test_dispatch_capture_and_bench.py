"""Dispatch capture (``dispatch/capture.py``) and the single-tick bench (``testing/bench.py``).

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.4, P3.5):

- With ``profile["policy_capture"]`` set, the runner writes one byte-stable
  ``openride.dispatch_context/1`` file per decided scope on capture ticks, at most
  ``max_files``; without it nothing is written and the runner has no hook.
- Capturing never changes what is assigned (it reads RNG state, never draws).
- A captured context replays to its live heads EXACTLY, on the partitioned
  (``partition:*`` scopes, module RNG, shuffle tie-breaks) and the pooled
  (``company:*`` scopes, tick ``Random``, prebuilt spatial pairs) topologies,
  including the sampled kind, because the fixture records the RNG state at decide
  time and the tiebreak seed.
- A failing write is logged with ``DISPATCH CAPTURE FAILED`` and the tick goes on.
- ``bench`` reports one row per policy with the fixed header and counts plans the
  invariants reject as violations.

Why: captured ticks are the input of offline policy screening; a capture that did
not replay exactly, or that perturbed the run it captured, would screen a policy
on a world that never happened.
"""

from __future__ import annotations

import gzip
import logging
import os
import random
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.dispatch import capture
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import build_runner_for_app, build_runner_for_policy
from apps.container_logistics.policies.framework import PolicyParams, policy
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.testing import fixtures as fx
from apps.container_logistics.policies.testing.bench import BENCH_HEADER, bench, load_fixtures, render_table
from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app


def _ids(matches):
    return sorted((str(t["_id"]), str(o["_id"])) for t, o in matches)


def _world():
    """Three hauliers, acme+borax partnered; co-located groups so tie-breaking decides."""
    trucks, orders = [], []
    for h, lon in (("acme", 103.85), ("borax", 103.86), ("cargo", 103.90)):
        for i in range(4):
            t = _truck(f"t_{h}_{i}", h, lon, 1.30)
            t["profile"]["last_dropoff_loc"] = {"type": "Point", "coordinates": [lon, 1.30]}
            trucks.append(t)
        for i in range(3):
            orders.append(_order(f"o_{h}_{i}", h, lon + 0.001 * i, 1.30))
    trucks.append(_truck("t_acme_far", "acme", 103.95, 1.32))
    return trucks, orders


def _capturing_app(tmp_path, *, topology, strategy, spatial, every=1, max_files=50):
    trucks, orders = _world()
    app = _app(trucks, orders, cooperation=_coop([("acme", "borax")]), topology=topology, strategy=strategy)
    prof = app.behavior["profile"]
    prof["use_spatial_matching"] = spatial
    prof["max_trucks_per_haulier"] = 3  # below the scope sizes, so the sampled kind really samples
    if tmp_path is not None:
        prof["policy_capture"] = {"dir": str(tmp_path), "every_n_ticks": every, "max_files": max_files}
    return app


_LIVE_CASES = [
    ("partitioned", "GreedyNearest", True),
    ("partitioned", "GreedyNearest", False),
    ("partitioned", "RandomAssignment", True),
    ("pooled", "GreedyNearest", True),
    ("pooled", "GreedyNearest", False),
    ("pooled", "RandomAssignment", True),
]


@pytest.mark.parametrize("topology,strategy,spatial", _LIVE_CASES)
def test_replaying_a_captured_context_reproduces_its_live_heads(tmp_path, topology, strategy, spatial):
    """Verified to have teeth: recording a ``Random(0)`` state instead of the decide-time
    snapshot failed every case but pooled spatial Greedy (whose tiebreak is a total
    order, so it never reads the RNG); recording ``tiebreak_seed`` as ``None`` failed
    both pooled Greedy cases (RandomAssignment ignores the tiebreak)."""
    app = _capturing_app(tmp_path, topology=topology, strategy=strategy, spatial=spatial)
    random.seed(11)
    for step in range(3):
        app.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=step)
    paths = sorted(tmp_path.glob("step_*.json.gz"))
    assert paths, "capture wrote no fixture"
    want_kind = "partition" if topology == "partitioned" else "company"
    nonempty = 0
    for path in paths:
        fixture = capture.read_fixture(str(path))
        assert fixture["scope"]["kind"] == want_kind
        assert fixture["policy"]["name"] == strategy
        if want_kind == "company":
            assert fixture["spatial_pairs"] is not None
        else:
            assert fixture["spatial_pairs"] is None
        nonempty += bool(fixture["live_heads"])
        assert capture.replay_heads(fixture) == fixture["live_heads"], (
            f"{path.name}: replay of a captured {topology} {strategy} context did not reproduce its live heads"
        )
    assert nonempty, "every captured scope was empty; the replay comparison would be vacuous"
    # The captured directory benches like a synthetic one.
    report = bench(["GreedyNearest", "RandomAssignment"], load_fixtures(str(tmp_path)))
    assert [r.fixtures for r in report.rows] == [len(paths), len(paths)]


@pytest.mark.parametrize("topology,strategy,spatial", _LIVE_CASES)
def test_capture_does_not_change_what_is_assigned(tmp_path, topology, strategy, spatial):
    """Verified to have teeth: drawing ``ctx.rng.random()`` inside ``before_decide`` failed
    every case but pooled spatial Greedy (which never reads the RNG)."""
    results = []
    for directory in (None, tmp_path):
        app = _capturing_app(directory, topology=topology, strategy=strategy, spatial=spatial)
        random.seed(11)
        ticks = [_ids(app.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=s)) for s in range(3)]
        results.append((ticks, random.random()))
    assert results[0] == results[1], "capturing changed the assignments or the module RNG stream"
    assert list(tmp_path.glob("step_*.json.gz"))


def test_capture_writes_fixture_when_enabled(tmp_path):
    prof = {"strategy": "GreedyNearest",
            "policy_capture": {"dir": str(tmp_path / "cap"), "every_n_ticks": 2, "max_files": 2}}
    runner = build_runner_for_app(build_runner_for_policy("GreedyNearest").policy, prof)
    fixture = fx.one_haulier()
    for step in (3, 4, 6, 8):  # 3 is off-cadence; 8 is past max_files
        fixture = fx.Fixture(**{**fixture.__dict__, "time_step": step})
        runner.run(fixture.context(runner))
    names = sorted(os.listdir(tmp_path / "cap"))
    assert names == ["step_4_partition-haulier-h_a.json.gz", "step_6_partition-haulier-h_a.json.gz"]
    raw = (tmp_path / "cap" / names[0]).read_bytes()
    assert raw[4:8] == b"\x00\x00\x00\x00", "gzip mtime must be 0 (byte-stable files)"
    doc = capture.read_fixture(str(tmp_path / "cap" / names[0]))
    assert doc["format"] == "openride.dispatch_context/1"
    assert set(doc) >= {"format", "run_id", "time_step", "sim_clock", "scope", "policy", "candidate_settings",
                        "rule", "tiebreak_seed", "trucks", "orders", "spatial_pairs", "live_heads", "rng"}
    assert doc["policy"] == {"name": "GreedyNearest", "version": "1",
                             "params": {"candidates": "spatial", "dual_cycle_bonus_km": 5.0,
                                        "dual_cycle_radius_km": 0.5}}
    assert doc["rule"] == {"kind": "partition", "max_travel_time_pickup": None, "adjacency": {}}
    assert len(doc["live_heads"]) == 6 and len(doc["trucks"]) == 8


def test_capture_off_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert capture.CaptureSettings.from_profile({"strategy": "GreedyNearest"}) is None
    app = _capturing_app(None, topology="partitioned", strategy="GreedyNearest", spatial=True)
    app.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=0)
    assert app._dispatch_runner(app.behavior["profile"])._capture is None
    assert list(tmp_path.rglob("*.json.gz")) == []


def test_capture_write_failure_is_logged_and_does_not_block_the_tick(tmp_path, caplog):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    prof = {"strategy": "GreedyNearest", "policy_capture": {"dir": str(blocker), "every_n_ticks": 1, "max_files": 5}}
    runner = build_runner_for_app(build_runner_for_policy("GreedyNearest").policy, prof)
    with caplog.at_level(logging.ERROR):
        pairs = runner.run(fx.one_haulier().context(runner))
    assert len(pairs) == 6, "a failed capture must not change the committed pairs"
    assert "DISPATCH CAPTURE FAILED" in caplog.text


def test_bench_reports_both_policies_on_synthetic():
    fixtures = [fx.scale(n_trucks=200, n_orders=100, hauliers=1, seed=3), fx.tied_costs(), fx.empty_orders()]
    report = bench(["GreedyNearest", "RandomAssignment"], fixtures)
    assert report.header == BENCH_HEADER
    assert [r.policy for r in report.rows] == ["GreedyNearest", "RandomAssignment"]
    for row in report.rows:
        assert row.fixtures == 3 and row.orders == 106 and row.violations == 0
        assert 0 < row.assign_rate <= 1 and row.assigned <= row.orders
        assert row.solve_ms_max >= row.solve_ms_p50 >= 0
    greedy, rand = report.rows
    assert greedy.deadhead_km_total < rand.deadhead_km_total, "nearest-greedy must beat random on deadhead"
    table = render_table(report)
    assert table.splitlines()[0] == BENCH_HEADER
    assert "GreedyNearest" in table and "RandomAssignment" in table and "deadhead_km_mean" in table


def test_bench_counts_violations():
    """Verified to have teeth: reading ``violations`` as ``0`` in ``_bench_one`` failed this test."""
    with isolated_registry():
        @policy(slot="dispatch", name="DoubleBooker", version="1", description="one order to every truck")
        class DoubleBooker(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                if not ctx.raw_orders:
                    return Plan(chains={})
                oid = str(ctx.raw_orders[0]["_id"])
                return Plan(chains={str(t["_id"]): (oid,) for t in ctx.raw_trucks})

        report = bench(["DoubleBooker"], [fx.one_haulier(), fx.tied_costs(), fx.single_truck()])
    (row,) = report.rows
    assert row.violations == 2, "one_haulier and tied_costs double-book; single_truck cannot"
    assert row.assigned == 1, "violating plans commit nothing; single_truck's one head is committed"


def test_capture_sees_the_raw_plan_before_the_invariants(tmp_path):
    """The hook runs after ``decide`` and BEFORE ``check_plan``: a double-booking plan reaches
    it with no problems attached, and its fixture records the raw heads the runner discards.

    Verified to have teeth: moving the capture call after ``check_plan`` in
    ``DispatchRunner.run`` failed this test."""
    with isolated_registry():
        @policy(slot="dispatch", name="DoubleBooker2", version="1", description="one order to two trucks")
        class DoubleBooker2(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                oid = str(ctx.raw_orders[0]["_id"])
                return Plan(chains={str(t["_id"]): (oid,) for t in ctx.raw_trucks[:2]})

        seen = []
        recorder = capture.CaptureRecorder(
            capture.CaptureSettings(dir=str(tmp_path), every_n_ticks=1, max_files=5),
            resolved=build_runner_for_policy("DoubleBooker2").resolved,
        )

        def hook(**kw):
            seen.append(kw["problems"])
            recorder(**kw)

        hook.before_decide = recorder.before_decide
        runner = build_runner_for_policy("DoubleBooker2", capture=hook)
        assert runner.run(fx.one_haulier().context(runner)) == []
    assert seen == [[]], "capture must see the plan before the invariants judged it"
    (path,) = tmp_path.glob("step_*.json.gz")
    assert capture.read_fixture(str(path))["live_heads"] == [["t000", "o000"], ["t001", "o000"]]
