"""Planner contract: ``Result``, the legacy ``Plan`` conversion, ``check_result`` and the ``DispatchRunner``.

What is locked: a result built from pairs keeps pair order and refuses a repeated
truck; a legacy ``Plan`` converts to a ``Result`` and a chain longer than one (a
tail) or an empty chain is refused (tails are not a framework concept); ``check_result``
reports every contract breach (§2.6); the runner discards a violating result with the
exact greppable ERROR line (message 18), counts it, commits the pairs in result order
as the tick's own documents, and lets non-contract exceptions through unchanged (§2.7).

Why: the runner sits between every planner policy and the commit. A breach that
slipped through would double-assign trucks or orders; an exception it swallowed would
change today's error behaviour. Policies here subclass ``DispatchPolicy``/``PairSolver``
directly (no registry): they run as unregistered leaves (``compat.unregistered``).
"""

from __future__ import annotations

import logging
import random

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.compat.legacy_context import Plan, result_from_legacy
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.bind import bind_tree
from apps.container_logistics.policies.dispatch.invariants import check_result
from apps.container_logistics.policies.dispatch.resolved import ResolvedDispatch
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.runner import DispatchRunner
from apps.container_logistics.policies.dispatch.tick import DispatchClock
from apps.container_logistics.policies.framework.errors import (
    DispatchContractViolation,
    PolicyTimingError,
)
from apps.container_logistics.policies.solver_lib.candidates import CandidateSettings, spatial_pairs_for_partition
from apps.container_logistics.policies.solver_lib.pair_solver import PairSolver
from tests.test_assignment_cooperation import _order, _truck

_CLOCK = DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=42)


def _resolved(name="Scripted", timing="online"):
    return ResolvedDispatch(name=name, version="1", timing=timing, params=None, source="unregistered")


def _runner(policy, *, name="Scripted", timing="online", capture=None, profile=None):
    prof = profile or {}
    return DispatchRunner(bind_tree(_resolved(name, timing), policy, prof), capture=capture, profile=prof)


class _Scripted(DispatchPolicy):
    """Returns whatever it was told to (or raises it), from a legacy ``decide``."""

    def __init__(self, result):
        super().__init__()
        self.result = result

    def decide(self, ctx):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result(ctx) if callable(self.result) else self.result


def _world():
    trucks = [_truck("t1", "acme", 103.85, 1.30), _truck("t2", "acme", 103.86, 1.30)]
    orders = [_order("o1", "acme", 103.85, 1.30), _order("o2", "acme", 103.86, 1.30),
              _order("o3", "acme", 103.87, 1.30)]
    return trucks, orders


def _tick(runner, trucks, orders, profile=None):
    return runner.tick(candidates=trucks, orders=orders, profile=profile or {}, clock=_CLOCK, seed=7)


def _plain_sub(trucks=None, orders=None):
    """A partition sub-tick over the whole world (its scope text names it in the messages)."""
    t, o = _world()
    tick = _tick(_runner(_Scripted(None)), trucks if trucks is not None else t, orders if orders is not None else o)
    return tick.restrict(trucks=tick.raw_trucks, orders=tick.raw_orders, scope="partition:haulier:acme",
                         sample_rng=random)


# --- Result / the legacy Plan --------------------------------------------------


def test_from_pairs_preserves_pair_order():
    trucks, orders = _world()
    result = Result.from_pairs([(trucks[1], orders[2]), (trucks[0], orders[0])], tick_notes={"k": 1})
    assert result.pairs == (("t2", "o3"), ("t1", "o1"))
    assert dict(result.tick_notes) == {"k": 1} and dict(result.solver_notes) == {}
    plan = Plan.from_pairs([(trucks[1], orders[2]), (trucks[0], orders[0])], notes={"k": 1})
    assert list(plan.chains) == ["t2", "t1"] and plan.heads() == [("t2", "o3"), ("t1", "o1")]


def test_from_pairs_rejects_a_truck_twice():
    trucks, orders = _world()
    with pytest.raises(DispatchContractViolation, match="truck 't1' appears twice in pairs"):
        Result.from_pairs([(trucks[0], orders[0]), (trucks[0], orders[1])])
    with pytest.raises(DispatchContractViolation, match="truck 't1' appears twice in pairs"):
        Plan.from_pairs([(trucks[0], orders[0]), (trucks[0], orders[1])])


def test_result_and_plan_are_read_only_after_construction():
    plan = Plan(chains={"t1": ["o1"]})
    assert plan.chains["t1"] == ("o1",)
    with pytest.raises(TypeError):
        plan.chains["t2"] = ("o2",)
    result = Result(pairs=[["t1", "o1"]], solver_notes={("t1", "o1"): {"x": 1}})
    assert result.pairs == (("t1", "o1"),)
    with pytest.raises(TypeError):
        result.solver_notes[("t2", "o2")] = {}


def test_legacy_plan_converts_to_pairs_and_refuses_tails():
    """A chain longer than one is refused at conversion: tails are not a framework concept.

    Verified to have teeth: dropping the ``len(chain) > 1`` refusal in
    ``compat.legacy_context.result_from_legacy`` failed the first ``raises``.
    """
    converted = result_from_legacy(Plan(chains={"t2": ("o3",), "t1": ("o1",)},
                                        notes={"pair_notes": {("t1", "o1"): {"pool_id": "p"}}, "market": {"m": 1}}))
    assert converted.pairs == (("t2", "o3"), ("t1", "o1"))
    assert dict(converted.solver_notes) == {("t1", "o1"): {"pool_id": "p"}}
    assert dict(converted.tick_notes) == {"market": {"m": 1}}
    with pytest.raises(DispatchContractViolation, match="chain for truck 't1' has 2 orders"):
        result_from_legacy(Plan(chains={"t1": ("o1", "o3")}))
    assert result_from_legacy("not a plan") == "not a plan", "a non-Plan is left for check_result"
    already = Result(pairs=(("t1", "o1"),))
    assert result_from_legacy(already) is already


def test_legacy_plan_with_an_empty_chain_is_refused():
    """Verified to have teeth: dropping ``or not chain`` in ``result_from_legacy`` failed the empty-tuple case."""
    with pytest.raises(DispatchContractViolation, match=r"chain for truck 't1' is empty or not a tuple: \(\)"):
        result_from_legacy(Plan(chains={"t1": ()}))
    with pytest.raises(DispatchContractViolation, match="is empty or not a tuple"):
        result_from_legacy(Plan(chains={"t1": "o1"}))  # a bare string is not a chain


# --- check_result --------------------------------------------------------------


def test_check_result_accepts_a_valid_result():
    assert check_result(Result(pairs=(("t1", "o1"), ("t2", "o2"))), _plain_sub()) == []


def test_check_result_flags_unknown_truck():
    """Verified to have teeth: deleting the ``tid not in trucks`` check failed this test."""
    problems = check_result(Result(pairs=(("t9", "o1"),)), _plain_sub())
    assert problems == ["truck 't9' is not in scope partition:haulier:acme"]


def test_check_result_flags_unknown_order():
    """Verified to have teeth: deleting the ``oid not in orders`` check failed this test."""
    problems = check_result(Result(pairs=(("t1", "o9"),)), _plain_sub())
    assert problems == ["order 'o9' is not in scope partition:haulier:acme"]


def test_check_result_flags_order_twice_and_truck_twice():
    """Verified to have teeth: removing ``seen_orders.add(oid)`` failed this test."""
    sub = _plain_sub()
    assert check_result(Result(pairs=(("t1", "o1"), ("t2", "o1"))), sub) == ["order 'o1' appears twice in the result"]
    assert check_result(Result(pairs=(("t1", "o1"), ("t1", "o2"))), sub) == ["truck 't1' appears twice in the result"]


def test_check_result_flags_disallowed_pair():
    """Verified to have teeth: negating the ``allowed`` test in ``check_result`` failed this test."""
    trucks, orders = _world()
    trucks[0]["profile"]["truck_size"] = "20ft"
    orders[1]["profile"]["order_size"] = "2x20"
    sub = _plain_sub(trucks, orders)
    assert check_result(Result(pairs=(("t1", "o2"),)), sub) == ["pair ('t1', 'o2') is not allowed"]
    assert check_result(Result(pairs=(("t1", "o1"),)), sub) == []


def test_check_result_flags_non_result_return():
    """Verified to have teeth: disabling the isinstance guard in ``check_result`` failed this test."""
    sub = _plain_sub()
    assert check_result([("t1", "o1")], sub) == ["solve_tick() returned list, expected Result"]
    assert check_result(None, sub) == ["solve_tick() returned NoneType, expected Result"]


# --- runner --------------------------------------------------------------------


def test_runner_discards_violating_result_and_logs_error(caplog):
    """Message 18 on the whole tick, exact text.

    Verified to have teeth: dropping the ``return TickOutcome(pairs=[], ...)`` after the
    report in ``DispatchRunner.run`` failed this test.
    """
    trucks, orders = _world()
    runner = _runner(_Scripted(Plan(chains={"t1": ("o1",), "t2": ("o1",)})), name="Dup")
    with caplog.at_level(logging.ERROR, logger="apps.container_logistics.policies.dispatch.runner"):
        outcome = runner.run(_tick(runner, trucks, orders))
    assert outcome.pairs == [] and outcome.result is None
    [rec] = [r for r in caplog.records if r.levelno == logging.ERROR]
    msg = rec.getMessage()
    assert msg.startswith(
        "PLANNER POLICY CONTRACT VIOLATION: Dup v1 at step 42 returned an invalid tick result: "
        "order 'o1' appears twice in the result. The result was DISCARDED (nothing committed this tick)."
    ), msg
    assert msg.endswith("run `openride policy validate planner Dup`."), msg


def test_runner_counts_violations():
    """Bad results, a raised DispatchContractViolation, and a non-Result return all count.

    Verified to have teeth: removing ``self.stats["violations"] += 1`` failed this test.
    """
    trucks, orders = _world()
    bad = [Plan(chains={"t9": ("o1",)}), DispatchContractViolation("nope"), "not a plan",
           Plan(chains={"t1": ("o1", "o2")})]
    runner = _runner(_Scripted(None))
    for result in bad:
        runner.policy.result = result
        assert runner.run(_tick(runner, trucks, orders)).pairs == []
    runner.policy.result = Plan(chains={"t1": ("o1",)})
    assert len(runner.run(_tick(runner, trucks, orders)).pairs) == 1
    assert runner.stats["calls"] == 5 and runner.stats["violations"] == 4
    assert runner.stats["total_ms"] >= runner.stats["max_ms"] >= 0.0


def test_runner_commits_pairs_in_result_order_as_the_ticks_documents():
    """Pairs in result order, as the SAME raw document objects.

    Verified to have teeth: reversing the list in ``Tick.docs`` failed this test.
    """
    trucks, orders = _world()
    runner = _runner(_Scripted(Result(pairs=(("t2", "o3"), ("t1", "o2")))))
    got = runner.run(_tick(runner, trucks, orders)).pairs
    assert [(t["_id"], o["_id"]) for t, o in got] == [("t2", "o3"), ("t1", "o2")]
    assert got[0][0] is trucks[1] and got[0][1] is orders[2]
    with pytest.raises(PolicyTimingError):
        _runner(_Scripted(None), timing="plan")


def test_runner_propagates_non_contract_exceptions():
    """Verified to have teeth: widening the except in ``DispatchRunner._solve`` to ``Exception`` failed this test."""
    trucks, orders = _world()
    runner = _runner(_Scripted(KeyError("boom")))
    with pytest.raises(KeyError, match="boom"):
        runner.run(_tick(runner, trucks, orders))
    assert runner.stats["violations"] == 0 and runner.stats["calls"] == 1


def test_runner_calls_capture_only_when_given():
    trucks, orders = _world()
    seen = []
    runner = _runner(_Scripted(Plan(chains={"t1": ("o1",)})), capture=lambda **kw: seen.append(kw))
    tick = _tick(runner, trucks, orders)
    runner.run(tick)
    assert len(seen) == 1 and seen[0]["tick"] is tick and seen[0]["problems"] == []
    assert seen[0]["result"].pairs == (("t1", "o1"),)
    with pytest.raises(ValueError, match="whole tick"):
        runner.run(tick.restrict(trucks=trucks, orders=orders, scope="s", sample_rng=None))


class _FirstFeasible(PairSolver):
    """Minimal v1 pair solver: each order takes the first free allowed truck (no RNG)."""

    def solve(self, trucks, orders, *, pair_allowed, cost):
        used, out = set(), []
        for o in orders:
            for t in trucks:
                if t["_id"] not in used and pair_allowed(t, o):
                    used.add(t["_id"])
                    out.append((t, o))
                    break
        return out

    def solve_pairs(self, candidate_pairs, *, cost):
        used_t, used_o, out = set(), set(), []
        for t, o in sorted(candidate_pairs, key=lambda p: cost(*p)):
            if t["_id"] in used_t or o["_id"] in used_o:
                continue
            used_t.add(t["_id"]), used_o.add(o["_id"])
            out.append((t, o))
        return out


@pytest.mark.parametrize("kind", ["sampled", "spatial"])
def test_pair_solver_default_decision_reproduces_the_legacy_call(kind):
    """The adapter hands ``solve``/``solve_pairs`` exactly what the inline code did.

    The unregistered kind follows the legacy rule (``use_spatial_matching`` and
    ``solve_pairs``, ``compat.unregistered``).
    """
    trucks = [_truck(f"t{i}", "acme", 103.85 + i * 0.01, 1.30) for i in range(3)]
    orders = [_order(f"o{i}", "acme", 103.85 + i * 0.012, 1.30) for i in range(4)]
    policy = _FirstFeasible()
    profile = {"use_spatial_matching": kind == "spatial"}
    runner = _runner(policy, profile=profile)

    def allowed(t, o):
        return assign_constraints.pair_allowed(t, o, max_travel_time_pickup=None)

    random.seed(3)
    got = runner.run(_tick(runner, trucks, orders, profile)).pairs
    if kind == "sampled":
        want = policy.solve(trucks, orders, pair_allowed=allowed, cost=assign_constraints.assignment_cost)
    else:
        pairs = spatial_pairs_for_partition(trucks, orders, serves=lambda a, b: a == b, allowed=allowed,
                                            settings=CandidateSettings.from_profile({}))
        want = policy.solve_pairs(pairs, cost=assign_constraints.assignment_cost)
    assert [(t["_id"], o["_id"]) for t, o in got] == [(t["_id"], o["_id"]) for t, o in want]
    assert got
