"""Whole-tick dispatch capture (``dispatch/capture.py``) and the whole-tick bench (``testing/bench.py``).

What is locked (round 2, ``docs/plans/policy_slots_round2_whole_tick.md`` §5 C5; plan
``docs/plans/policy_slots_p0_p3.md`` §4 P3.4, P3.5):

- With ``profile["policy_capture"]`` set, the runner writes one byte-stable
  ``openride.dispatch_context/2`` file per captured TICK, ``step_<n>.json.gz``, at
  most ``max_files``, carrying the tick seed, the module RNG state, the selection in
  stamp form, the world (structure, ``max_travel_time_pickup``, accounting params),
  the documents, the raw plan's heads and ``rebate_aware``; without it nothing is
  written and the runner has no hook.
- Capturing never changes what is assigned (it reads RNG state, never draws).
- A captured tick replays to its live heads EXACTLY, through ``Partitioned`` (module
  RNG, shuffle tie-breaks) and ``PooledMarket`` (tick ``Random``, rounds), with
  either leaf and either candidate kind.
- Format ``/1`` (one scope) is refused with message 22, and a tick captured with
  ``rebate_aware: true`` is refused on replay (the rebate book is not captured).
- A failing write is logged with ``DISPATCH CAPTURE FAILED`` and the tick goes on.
- ``bench`` decides whole ticks, accepts aliases, counts plans the invariants reject
  as violations, and reports the framework's accounting (``cross_company``,
  ``benefit_km_total`` from ``assignment.accounting.account_shares``) and the
  selection's ``info_model`` and ``label`` for every policy alike.

Why: captured ticks are the input of offline policy screening; a capture that did
not replay exactly, or that perturbed the run it captured, would screen a policy
on a world that never happened, and a bench that accounted each solver its own way
would compare nothing.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import random
from dataclasses import dataclass

import pytest

from apps.container_logistics.assignment import accounting
from apps.container_logistics.policies.dispatch import capture
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import build_runner_for_app, build_runner_for_policy
from apps.container_logistics.policies.dispatch.world import tick_seed
from apps.container_logistics.policies.framework import PolicyParams, policy
from apps.container_logistics.policies.framework.errors import PolicyError
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.testing import fixtures as fx
from apps.container_logistics.policies.testing.bench import (
    BENCH_COLUMNS,
    BENCH_HEADER,
    bench,
    load_fixtures,
    render_table,
)
from openride_control.policies import cmd_policy_bench, parse_bench_params
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
_LIVE_IDS = [f"{t}-{s}-{'spatial' if sp else 'sampled'}" for t, s, sp in _LIVE_CASES]
_TOP = {"partitioned": "Partitioned", "pooled": "PooledMarket"}


@pytest.mark.parametrize("topology,strategy,spatial", _LIVE_CASES, ids=_LIVE_IDS)
def test_replay_whole_tick_reproduces_live_heads(tmp_path, topology, strategy, spatial):
    """Verified to have teeth (each mutation in ``capture.py`` restored after, by hash):
    recording ``module_rng_state`` from a fresh ``random.Random(0)`` instead of the
    module failed exactly the three partitioned cases (the pooled ones never read the
    module); recording ``tick_seed + 1`` failed all six; rebuilding the replay world
    without the captured structure (``fixture_world``) failed all six (the
    acme+borax component became two haulier groups / no pools)."""
    app = _capturing_app(tmp_path, topology=topology, strategy=strategy, spatial=spatial)
    random.seed(11)
    for step in range(3):
        app.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=step)
    paths = sorted(tmp_path.glob("step_*.json.gz"))
    assert [p.name for p in paths] == ["step_0.json.gz", "step_1.json.gz", "step_2.json.gz"], (
        "one file per captured tick"
    )
    for step, path in enumerate(paths):
        fixture = capture.read_fixture(str(path))
        assert fixture["format"] == "openride.dispatch_context/2"
        assert fixture["selection"]["name"] == _TOP[topology]
        assert fixture["selection"]["label"] == f"{_TOP[topology]}/{strategy}"
        assert fixture["tick_seed"] == tick_seed("run_test", step)
        assert fixture["world"]["structure"]["adjacency"] == {"acme": ["borax"], "borax": ["acme"]}
        assert fixture["rebate_aware"] is False
        assert fixture["live_heads"], f"{path.name}: an empty tick would make the replay comparison vacuous"
        assert capture.replay_heads(fixture) == fixture["live_heads"], (
            f"{path.name}: replay of a captured {topology} {strategy} tick did not reproduce its live heads"
        )
    # The captured directory benches like a synthetic one.
    report = bench(["GreedyNearest", "RandomAssignment", "Partitioned"], load_fixtures(str(tmp_path)))
    assert [r.fixtures for r in report.rows] == [3, 3, 3]
    assert all(r.violations == 0 for r in report.rows)


@pytest.mark.parametrize("topology,strategy,spatial", _LIVE_CASES, ids=_LIVE_IDS)
def test_capture_does_not_change_what_is_assigned(tmp_path, topology, strategy, spatial):
    """Verified to have teeth: drawing ``random.random()`` inside ``before_decide`` failed
    all six cases (the partitioned assignments, and the module stream after every case)."""
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
    fixture = fx.three_company_chain()
    for step in (3, 4, 6, 8):  # 3 is off-cadence; 8 is past max_files
        fixture = fx.Fixture(**{**fixture.__dict__, "time_step": step})
        runner.run(fixture.context(runner))
    names = sorted(os.listdir(tmp_path / "cap"))
    assert names == ["step_4.json.gz", "step_6.json.gz"]
    raw = (tmp_path / "cap" / names[0]).read_bytes()
    assert raw[4:8] == b"\x00\x00\x00\x00", "gzip mtime must be 0 (byte-stable files)"
    doc = capture.read_fixture(str(tmp_path / "cap" / names[0]))
    assert set(doc) == {"format", "run_id", "time_step", "sim_clock", "horizon_steps", "tick_seed",
                        "module_rng_state", "selection", "candidate_settings", "world", "trucks", "orders",
                        "live_heads", "rebate_aware"}
    assert doc["format"] == "openride.dispatch_context/2"
    assert doc["time_step"] == 4 and doc["tick_seed"] == fx.Fixture(**{**fixture.__dict__, "time_step": 4}).tick_seed
    assert doc["selection"]["label"] == "Partitioned/GreedyNearest"
    assert doc["selection"]["params"]["inner"] == {
        "type": "GreedyNearest", "version": "1",
        "params": {"candidates": "spatial", "dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5}}
    assert set(doc["world"]) == {"structure", "max_travel_time_pickup", "accounting_params"}
    assert doc["world"]["structure"]["adjacency"]["h_b"] == ["h_a", "h_c"]
    assert len(doc["module_rng_state"][1]) == 625
    assert len(doc["trucks"]) == 12 and len(doc["orders"]) == 8 and len(doc["live_heads"]) == 8
    assert capture.replay_heads(doc) == doc["live_heads"]


def _write_raw(path, doc):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh)


def test_format_1_is_refused(tmp_path):
    """Message 22, from the reader, the bench loader and the replay alike."""
    path = tmp_path / "step_5_partition-haulier-h_a.json.gz"
    _write_raw(path, {"format": "openride.dispatch_context/1", "scope": {"kind": "partition"}})
    want = (f"dispatch fixture {path} is format openride.dispatch_context/1 (one scope); this version "
            "replays whole ticks (format /2). Re-capture with --capture-dispatch.")
    with pytest.raises(PolicyError) as exc:
        capture.read_fixture(str(path))
    assert str(exc.value) == want
    with pytest.raises(PolicyError, match=r"format /2\)\. Re-capture with --capture-dispatch\."):
        load_fixtures(str(tmp_path))
    with pytest.raises(PolicyError, match="is format openride.dispatch_context/1"):
        capture.replay_heads({"format": "openride.dispatch_context/1"})


def test_rebate_aware_capture_is_refused_on_replay(tmp_path):
    prof = {"strategy": "GreedyNearest", "planner": {"rebate_aware": True},
            "policy_capture": {"dir": str(tmp_path), "every_n_ticks": 1, "max_files": 5}}
    runner = build_runner_for_app(build_runner_for_policy("GreedyNearest").policy, prof)
    runner.run(fx.one_haulier().context(runner))
    (path,) = tmp_path.glob("step_*.json.gz")
    doc = capture.read_fixture(str(path))
    assert doc["rebate_aware"] is True
    with pytest.raises(PolicyError, match="rebate_aware: true"):
        capture.replay_heads(doc)


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
        pairs = runner.run(fx.one_haulier().context(runner)).pairs
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


def _accounted(name, params, fixture):
    """``(cross_company, benefit_km)`` recomputed here, straight from ``account_shares``."""
    runner = build_runner_for_policy(name, params)
    with fixture.module_random():
        ctx = fixture.context(runner)
        pairs = runner.run(ctx).pairs
    shares = accounting.account_shares(pairs, candidates=list(ctx.raw_trucks), cost=ctx.world.accounting_cost,
                                       own_allowed=ctx.world.own_allowed)
    benefit = sum(a.owner_reserve_km - a.awarded_cost_km for a in shares
                  if a.owner_reserve_km is not None and math.isfinite(a.owner_reserve_km)
                  and math.isfinite(a.awarded_cost_km))
    return len(shares), benefit


def test_bench_reports_framework_accounting_columns():
    """Verified to have teeth (each restored after, by hash): counting ``cross_company``
    from the pairs whose truck and order ids differ (not ``account_shares``) in
    ``bench._bench_one`` failed this test; summing ``awarded_cost_km`` instead of
    ``reserve - awarded`` failed it too."""
    fixtures = [fx.three_company_chain(), fx.tied_costs(), fx.scale(n_trucks=60, n_orders=40, hauliers=3, seed=2)]
    entries = ["Partitioned", "pm=PooledMarket", "pg=PooledMarket", "GreedyNearest"]
    overrides = {"pg": {"inner": "GreedyNearest"}}
    report = bench(entries, fixtures, overrides)
    for col in ("info_model", "cross_company", "benefit_km_total", "label"):
        assert col in BENCH_COLUMNS and col in render_table(report).splitlines()[2]
    rows = {r.policy: r for r in report.rows}
    assert list(rows) == ["Partitioned", "pm", "pg", "GreedyNearest"]
    assert {k: (r.label, r.info_model) for k, r in rows.items()} == {
        "Partitioned": ("Partitioned/RandomAssignment", "per_group"),
        "pm": ("PooledMarket/RandomAssignment", "per_company"),
        "pg": ("PooledMarket/GreedyNearest", "per_company"),
        "GreedyNearest": ("GreedyNearest", "central"),
    }
    params = {"Partitioned": None, "pm": None, "pg": {"inner": {"type": "GreedyNearest"}}, "GreedyNearest": None}
    names = {"Partitioned": "Partitioned", "pm": "PooledMarket", "pg": "PooledMarket", "GreedyNearest": "GreedyNearest"}
    for alias, row in rows.items():
        got = [_accounted(names[alias], params[alias], f) for f in fixtures]
        assert row.cross_company == sum(c for c, _b in got), alias
        assert row.benefit_km_total == pytest.approx(round(sum(b for _c, b in got), 3), abs=1e-6), alias
        assert row.violations == 0
    assert rows["GreedyNearest"].cross_company > 0, "the chain must produce shares, or the columns are vacuous"


def test_bench_aliases_through_the_control_plane():
    """Spec §3.3's form: ``--policies pm=PooledMarket,pr=PooledMarket --policy-param pr.inner=...``."""
    assert parse_bench_params(["pm=PooledMarket", "pr=PooledMarket"],
                              ["pr.inner=GreedyNearest", "pr.inner.candidates=sampled", "pm.max_rounds=3"]) == {
        "pr": {"inner": {"type": "GreedyNearest", "params": {"candidates": "sampled"}}},
        "pm": {"max_rounds": 3},
    }
    result = cmd_policy_bench("dispatch", "pm=PooledMarket,pr=PooledMarket", synthetic="three_company_chain",
                              params=["pr.inner=GreedyNearest"])
    assert result["ok"], result
    assert [(r["policy"], r["label"]) for r in result["report"]["rows"]] == [
        ("pm", "PooledMarket/RandomAssignment"), ("pr", "PooledMarket/GreedyNearest")]
    bad = cmd_policy_bench("dispatch", "pm=PooledMarket", synthetic="tied_costs", params=["px.inner=GreedyNearest"])
    assert not bad["ok"] and "'px', which is not benched" in bad["message"]
    dup = cmd_policy_bench("dispatch", "pm=PooledMarket,pm=Partitioned", synthetic="tied_costs")
    assert not dup["ok"] and "used twice" in dup["message"]


def test_bench_counts_violations():
    """Verified to have teeth: reading ``violations`` as ``0`` in ``_bench_one`` failed this test."""
    with isolated_registry():
        @policy(slot="dispatch", information_model="central", name="DoubleBooker", version="1", description="one order to every truck")
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
        @policy(slot="dispatch", information_model="central", name="DoubleBooker2", version="1", description="one order to two trucks")
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
        assert runner.run(fx.one_haulier().context(runner)).pairs == []
    assert seen == [[]], "capture must see the plan before the invariants judged it"
    (path,) = tmp_path.glob("step_*.json.gz")
    assert capture.read_fixture(str(path))["live_heads"] == [["t000", "o000"], ["t001", "o000"]]
