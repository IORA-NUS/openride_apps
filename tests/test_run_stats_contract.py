"""Run-stats contract (plan ``plan_contract_final.md`` §2.8, §7.8 WS4a) and the market's own bid cost (G2).

What is pinned:

- the generic driver (``dispatch.run_stats.record_tick`` / ``flush``) with a toy
  aggregator under a generic meta key: one instance per key, created on first use,
  forced push before the scheduled one, flush forces every aggregator;
- the market aggregator (``_market_stats.RoundStatsAggregator``): first-tick push (the
  max grew), the every-5-ticks cadence, the forced push on truncation (twice on a
  cadence tick, as before), the backstop ERROR logged once, the empty snapshot;
- the app: a pooled top folds through the declared aggregator and ``close`` flushes it
  under ``meta.market``; a partitioned top declares none and never creates the store;
- ``PooledMarket.bind`` declares ``run_stats`` at the top only;
- G2: the market bids with its inner's dual-cycle params, else the constraint defaults,
  never the framework's accounting dials.
"""

from __future__ import annotations

import copy
import logging

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import pooled_planner
from apps.container_logistics.assignment.solver.greedy_nearest import GreedyNearestSolver
from apps.container_logistics.policies.dispatch import run_stats
from apps.container_logistics.policies.dispatch.library import pooled_market
from apps.container_logistics.policies.dispatch.library._market_stats import (
    ROUND_STATS_PATCH_EVERY,
    RoundStatsAggregator,
)
from apps.container_logistics.policies.dispatch.tick import DispatchClock
from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app

CLOCK = DispatchClock(sim_clock="2020-01-01 08:00:00", time_step=0)


def _notes(rounds=1, converged=True, max_rounds=20):
    return {"rounds_used": rounds, "converged": converged, "candidate_pairs_round1": 3,
            "candidate_pairs_total": 4, "max_rounds": max_rounds}


class _Toy:
    """A minimal aggregator under a generic key: counts ticks, pushes when a note asks."""

    meta_key = "toy"

    def __init__(self):
        self.n = 0
        self.pushed_at = None

    def fold(self, tick_notes, clock):
        self.n += 1
        return bool(tick_notes.get("urgent"))

    def push_due(self, *, force=False):
        return force or self.n % 2 == 0

    def mark_pushed(self):
        self.pushed_at = self.n

    def snapshot(self):
        return {"n": self.n}

    @classmethod
    def empty_snapshot(cls):
        return {"n": 0}


def _recorder():
    pushes = []
    return pushes, lambda key, value: pushes.append((key, value)) or True


# --- the generic driver --------------------------------------------------------------

def test_toy_aggregator_under_a_generic_key():
    store, (pushes, push) = {}, _recorder()
    first = run_stats.record_tick(store, _Toy, {}, CLOCK, push)
    assert store == {"toy": first} and pushes == []
    assert run_stats.record_tick(store, _Toy, {}, CLOCK, push) is first, "one instance per meta key"
    assert pushes == [("toy", {"n": 2})]
    run_stats.record_tick(store, _Toy, {"urgent": True}, CLOCK, push)
    assert pushes[-1] == ("toy", {"n": 3}), "a forced fold pushes at once"
    # forced AND scheduled on the same tick: both pushes, forced first (app.py order kept).
    run_stats.record_tick(store, _Toy, {"urgent": True}, CLOCK, push)
    assert pushes[-2:] == [("toy", {"n": 4}), ("toy", {"n": 4})]
    run_stats.flush(store, push)
    assert pushes[-1] == ("toy", {"n": 4}) and len(pushes) == 5


def test_flush_of_an_empty_store_pushes_nothing():
    pushes, push = _recorder()
    run_stats.flush({}, push)
    assert pushes == []


# --- the market aggregator -----------------------------------------------------------

def test_market_cadence_first_tick_then_every_fifth():
    store, (pushes, push) = {}, _recorder()
    for _ in range(2 * ROUND_STATS_PATCH_EVERY):
        run_stats.record_tick(store, RoundStatsAggregator, _notes(rounds=2), CLOCK, push)
    assert [v["ticks"] for _, v in pushes] == [1, 5, 10]
    assert {k for k, _ in pushes} == {"market"}


def test_market_push_when_the_round_profile_grows():
    store, (pushes, push) = {}, _recorder()
    for r in (1, 1, 3, 3):
        run_stats.record_tick(store, RoundStatsAggregator, _notes(rounds=r), CLOCK, push)
    assert [v["ticks"] for _, v in pushes] == [1, 3], "a new rounds_max is new information"


def test_market_truncation_forces_a_push_and_logs_once(caplog):
    store, (pushes, push) = {}, _recorder()
    for _ in range(4):
        run_stats.record_tick(store, RoundStatsAggregator, _notes(rounds=1), CLOCK, push)
    assert [v["ticks"] for _, v in pushes] == [1]
    with caplog.at_level(logging.ERROR):
        # tick 5 truncates: forced push, then the cadence push (5 % 5 == 0), as before.
        run_stats.record_tick(store, RoundStatsAggregator, _notes(rounds=1, converged=False), CLOCK, push)
        run_stats.record_tick(store, RoundStatsAggregator, _notes(rounds=1, converged=False), CLOCK, push)
    assert [v["ticks"] for _, v in pushes] == [1, 5, 5, 6]
    assert pushes[-1][1]["ticks_truncated_by_backstop"] == 2
    errors = [r for r in caplog.records if "TRUNCATED BY BACKSTOP" in r.getMessage()]
    assert len(errors) == 1, "the backstop ERROR is logged once per run"


def test_market_horizon_split_reads_the_clock():
    agg = RoundStatsAggregator()
    agg.fold(_notes(), DispatchClock(sim_clock="x", time_step=5, horizon_steps=10))
    agg.fold(_notes(), DispatchClock(sim_clock="x", time_step=10, horizon_steps=10))
    agg.fold(_notes(), DispatchClock(sim_clock="x", time_step=99, horizon_steps=None))
    snap = agg.snapshot()
    assert (snap["ticks_in_horizon"], snap["ticks_drain"]) == (2, 1)


def test_market_empty_snapshot_and_nothing_due_before_a_tick():
    agg = RoundStatsAggregator()
    assert agg.snapshot() == RoundStatsAggregator.empty_snapshot()
    assert agg.push_due(force=True) is False


# --- the app ---------------------------------------------------------------------------

def _clique():
    hs = ["acme", "borax", "cargo"]
    trucks = [_truck(f"t_{h}_{j}", h, 103.80 + 0.004 * (i * 3 + j), 1.30) for i, h in enumerate(hs) for j in range(3)]
    orders = [_order(f"o_{h}_{j}", h, 103.85 + 0.003 * (i * 3 + j), 1.30) for i, h in enumerate(hs) for j in range(3)]
    return trucks, orders, _coop([[a, b] for i, a in enumerate(hs) for b in hs[i + 1:]])


def test_pooled_app_folds_the_declared_aggregator_and_close_flushes_it():
    trucks, orders, coop = _clique()
    app = _app(trucks, orders, cooperation=coop)
    pushes = []
    app._patch_run_config_meta = lambda key, value: pushes.append((key, value)) or True
    app.assign("2020-01-01 08:00:00", time_step=0)
    assert set(app._run_stats) == {"market"}
    assert isinstance(app._run_stats["market"], RoundStatsAggregator)
    assert pushes == [("market", app.round_stats())], "first tick pushes (the max grew)"
    app.close("2020-01-01 09:00:00")
    assert pushes[-1] == ("market", app.round_stats()) and len(pushes) == 2


def test_partitioned_app_declares_no_run_stats():
    trucks, orders, coop = _clique()
    app = _app(trucks, orders, cooperation=coop, topology="partitioned")
    app.assign("2020-01-01 08:00:00", time_step=0)
    assert getattr(app, "_run_stats", None) is None
    assert app.round_stats() == RoundStatsAggregator.empty_snapshot()


def test_pooled_market_declares_run_stats_at_the_top_only():
    market = pooled_market.PooledMarket.__new__(pooled_market.PooledMarket)  # bind reads only the world
    world = {}
    assert market.bind(None, world, top=True).run_stats is RoundStatsAggregator
    assert market.bind(None, world, top=False).run_stats is None


# --- G2: the market's own bid cost ---------------------------------------------------

def _params_seen(monkeypatch, cost_fn_factory):
    seen = []
    monkeypatch.setattr(assign_constraints, "assignment_cost",
                        lambda t, o, params=None: seen.append(dict(params or {})) or 0.0)
    cost_fn_factory()({}, {})
    return seen


def test_bid_cost_uses_the_inner_dual_cycle_params(monkeypatch):
    inner = GreedyNearestSolver.Params(dual_cycle_bonus_km=7.5, dual_cycle_radius_km=1.25)
    seen = _params_seen(monkeypatch, lambda: pooled_market._bid_cost(inner))
    assert seen == [{"dual_cycle_bonus_km": 7.5, "dual_cycle_radius_km": 1.25}]


def test_bid_cost_falls_back_to_the_constraint_defaults(monkeypatch):
    seen = _params_seen(monkeypatch, lambda: pooled_market._bid_cost(None))
    assert seen == [{"dual_cycle_bonus_km": assign_constraints._DEFAULT_DUAL_CYCLE_BONUS_KM,
                     "dual_cycle_radius_km": assign_constraints._DEFAULT_DUAL_CYCLE_RADIUS_KM}]


def _dual_cycle_probe(trucks, orders):
    """A pair the dual-cycle bonus applies to (same facility), so the dials change its cost."""
    t, o = copy.deepcopy(trucks[0]), copy.deepcopy(orders[0])
    t.setdefault("profile", {})["last_dropoff_facility_name"] = "F"
    o.setdefault("profile", {})["pickup_facility_name"] = "F"
    return t, o


@pytest.mark.parametrize("strategy", ["RandomAssignment", "GreedyNearest"])
def test_the_market_never_bids_with_the_accounting_dials(monkeypatch, strategy):
    """The documented G2 boundary: off-default ``solver_params`` no longer reach the bids.

    Both inners here run at their default dual-cycle params (the cost-blind
    RandomAssignment declares none), so the market bids at 5.0 / 0.5 while the app's
    accounting dials say 99 / 9.

    Verified to have teeth: restoring ``cost=tick.measure.cost`` in
    ``PooledMarket.solve_tick`` fails both cases (the bid cost then prices with 99 / 9).
    """
    trucks, orders, coop = _clique()
    app = _app(trucks, orders, cooperation=coop, strategy=strategy)
    dials = {"dual_cycle_bonus_km": 99.0, "dual_cycle_radius_km": 9.0}
    app._solver_params = dials
    real = pooled_planner.run_pooled_market
    captured = {}

    def spy(**kw):
        captured["cost"] = kw["ctx"].cost
        return real(**kw)

    monkeypatch.setattr(pooled_planner, "run_pooled_market", spy)
    app.assign("2020-01-01 08:00:00", time_step=0)
    t, o = _dual_cycle_probe(trucks, orders)
    defaults = assign_constraints.assignment_cost(t, o, {})
    assert defaults != assign_constraints.assignment_cost(t, o, dials), "the probe is not dial-sensitive"
    assert captured["cost"](t, o) == defaults


# ------------------------------------------------- the optional run-end warnings (V-M1)

class _WarnAgg:
    meta_key = "warn_stats"

    def __init__(self, lines=("plans degraded",), boom=False):
        self.lines, self.boom = list(lines), boom

    def fold(self, notes, clock):
        return False

    def push_due(self, *, force=False):
        return force

    def mark_pushed(self):
        pass

    def snapshot(self):
        return {"n": 1}

    def run_end_warnings(self):
        if self.boom:
            raise RuntimeError("hook bug")
        return self.lines


def test_flush_logs_an_aggregators_run_end_warnings(caplog):
    pushed = []
    store = {"warn_stats": _WarnAgg()}
    with caplog.at_level(logging.WARNING):
        run_stats.flush(store, lambda k, v: pushed.append((k, v)) or True)
    assert pushed == [("warn_stats", {"n": 1})]
    assert "RUN STATS WARNING (run_config.meta.warn_stats): plans degraded" in caplog.text


def test_a_raising_run_end_hook_never_breaks_the_flush(caplog):
    pushed = []
    store = {"warn_stats": _WarnAgg(boom=True), "quiet": _WarnAgg(lines=())}
    with caplog.at_level(logging.WARNING):
        run_stats.flush(store, lambda k, v: pushed.append(k) or True)
    assert pushed == ["warn_stats", "quiet"]
    assert "run_end_warnings raised" in caplog.text and "RUN STATS WARNING" not in caplog.text
