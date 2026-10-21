"""Dispatch contract v2: ``Plan``, ``check_plan`` and the ``DispatchRunner``.

What is locked: a plan built from pairs keeps pair order and refuses a repeated
truck; ``check_plan`` reports every contract breach the plan lists (§4 P1.7); the
runner discards a violating plan with the exact greppable ERROR line, counts it,
commits only chain heads under ``online`` timing, and lets non-contract exceptions
through unchanged (§4 P1.8).

Why: the runner sits between every dispatch policy and the commit on both
topologies. A breach that slipped through would double-assign trucks or orders; an
exception it swallowed would change today's error behaviour. Policies here
subclass ``DispatchPolicy``/``PairSolver`` directly (no registry).
"""

from __future__ import annotations

import logging
import random

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch import scopes
from apps.container_logistics.policies.dispatch.allowed import PartitionRule
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.context import DispatchClock
from apps.container_logistics.policies.dispatch.helpers.candidates import CandidateSettings
from apps.container_logistics.policies.dispatch.invariants import check_plan
from apps.container_logistics.policies.dispatch.pair_solver import PairSolver
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.resolved import ResolvedDispatch
from apps.container_logistics.policies.dispatch.runner import DispatchRunner
from apps.container_logistics.policies.framework.errors import (
    DispatchContractViolation,
    PolicyTimingError,
)
from tests.test_assignment_cooperation import _order, _truck

_CLOCK = DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=42)


def _resolved(name="Scripted", kind="sampled", timing="online"):
    return ResolvedDispatch(name=name, version="1", timing=timing, params=None,
                            candidate_kind=kind, source="unregistered")


class _Scripted(DispatchPolicy):
    """Returns whatever it was told to (or raises it)."""

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


def _ctx(runner, trucks, orders, rule=None):
    # Round 2 C3: contexts are built by dispatch/scopes.py only (the runner lost its
    # partition_context method); a parent-less partition scope, as offline fixtures use.
    return scopes.partition_context(
        policy=runner.policy, params=runner.resolved.params, candidate_kind=runner.candidate_kind,
        trucks=trucks, orders=orders, partition_key=("haulier", "acme"),
        rule=rule or PartitionRule(max_travel_time_pickup=None, adjacency={}),
        settings=CandidateSettings.from_profile({}),
        accounting_cost=assign_constraints.assignment_cost, sample_rng=random, clock=_CLOCK,
    )


def _plain_ctx(trucks=None, orders=None, rule=None):
    t, o = _world()
    runner = DispatchRunner(policy=_Scripted(None), resolved=_resolved())
    return _ctx(runner, trucks if trucks is not None else t, orders if orders is not None else o, rule)


# --- Plan ----------------------------------------------------------------------


def test_from_pairs_preserves_pair_order():
    trucks, orders = _world()
    plan = Plan.from_pairs([(trucks[1], orders[2]), (trucks[0], orders[0])], notes={"k": 1})
    assert list(plan.chains) == ["t2", "t1"]
    assert plan.heads() == [("t2", "o3"), ("t1", "o1")]
    assert dict(plan.notes) == {"k": 1}


def test_from_pairs_rejects_a_truck_twice():
    trucks, orders = _world()
    with pytest.raises(DispatchContractViolation, match="truck 't1' appears twice in pairs"):
        Plan.from_pairs([(trucks[0], orders[0]), (trucks[0], orders[1])])


def test_plan_is_read_only_after_construction():
    plan = Plan(chains={"t1": ["o1"]})
    assert plan.chains["t1"] == ("o1",)
    with pytest.raises(TypeError):
        plan.chains["t2"] = ("o2",)


# --- check_plan ----------------------------------------------------------------


def test_check_plan_accepts_a_valid_plan():
    assert check_plan(Plan(chains={"t1": ("o1", "o3"), "t2": ("o2",)}), _plain_ctx()) == []


def test_check_plan_flags_unknown_truck():
    """Verified to have teeth: deleting the ``tid not in trucks`` check failed this test."""
    problems = check_plan(Plan(chains={"t9": ("o1",)}), _plain_ctx())
    assert problems == ["truck 't9' is not in scope partition:haulier:acme"]


def test_check_plan_flags_unknown_order():
    """Verified to have teeth: deleting the ``oid not in orders`` check failed this test."""
    problems = check_plan(Plan(chains={"t1": ("o1", "o9")}), _plain_ctx())
    assert problems == ["order 'o9' is not in scope partition:haulier:acme"]


def test_check_plan_flags_order_twice():
    """Across chains and within one chain.

    Verified to have teeth: removing ``seen_orders.add(oid)`` failed this test.
    """
    ctx = _plain_ctx()
    assert check_plan(Plan(chains={"t1": ("o1",), "t2": ("o1",)}), ctx) == ["order 'o1' appears twice in the plan"]
    assert check_plan(Plan(chains={"t1": ("o2", "o2")}), ctx) == ["order 'o2' appears twice in the plan"]


def test_check_plan_flags_disallowed_head():
    """Verified to have teeth: negating the ``allowed_raw`` test in ``check_plan`` failed this test."""
    trucks, orders = _world()
    trucks[0]["profile"]["truck_size"] = "20ft"
    orders[1]["profile"]["order_size"] = "2x20"
    ctx = _plain_ctx(trucks, orders)
    assert check_plan(Plan(chains={"t1": ("o2",)}), ctx) == ["head pair ('t1', 'o2') is not allowed"]
    assert check_plan(Plan(chains={"t1": ("o1", "o2")}), ctx) == [], "tails are P4's to time-check"


def test_check_plan_flags_empty_chain():
    """Verified to have teeth: dropping ``or not chain`` failed the empty-tuple case."""
    ctx = _plain_ctx()
    assert check_plan(Plan(chains={"t1": ()}), ctx) == ["chain for truck 't1' is empty or not a tuple: ()"]
    assert len(check_plan(Plan(chains={"t1": "o1"}), ctx)) == 1, "a bare string is not a chain"


def test_check_plan_flags_non_plan_return():
    """Verified to have teeth: disabling the isinstance guard in ``check_plan`` failed this test."""
    ctx = _plain_ctx()
    assert check_plan([("t1", "o1")], ctx) == ["decide() returned list, expected Plan"]
    assert check_plan(None, ctx) == ["decide() returned NoneType, expected Plan"]


# --- runner --------------------------------------------------------------------


def test_runner_discards_violating_plan_and_logs_error(caplog):
    """Verified to have teeth: dropping the ``return []`` after the report in ``run`` failed this test."""
    trucks, orders = _world()
    runner = DispatchRunner(policy=_Scripted(Plan(chains={"t1": ("o1",), "t2": ("o1",)})),
                            resolved=_resolved(name="Dup"))
    with caplog.at_level(logging.ERROR, logger="apps.container_logistics.policies.dispatch.compose"):
        assert runner.run(_ctx(runner, trucks, orders)).pairs == []
    [rec] = [r for r in caplog.records if r.levelno == logging.ERROR]
    msg = rec.getMessage()
    # A sub-scope context: message 19 (round 2 §3.7), logged by dispatch/compose.py.
    assert msg.startswith(
        "DISPATCH POLICY CONTRACT VIOLATION: inner Dup v1 in scope partition:haulier:acme at step 42 "
        "returned an invalid plan: order 'o1' appears twice in the plan. The scope's plan was DISCARDED"
    ), msg
    assert msg.endswith("run `openride policy validate dispatch Dup`."), msg


def test_runner_counts_violations():
    """Bad plans, a raised DispatchContractViolation, and a non-Plan return all count.

    Verified to have teeth: removing ``self.stats["violations"] += 1`` failed this test.
    """
    trucks, orders = _world()
    bad = [Plan(chains={"t9": ("o1",)}), DispatchContractViolation("nope"), "not a plan"]
    runner = DispatchRunner(policy=_Scripted(None), resolved=_resolved())
    for result in bad:
        runner.policy.result = result
        assert runner.run(_ctx(runner, trucks, orders)).pairs == []
    runner.policy.result = Plan(chains={"t1": ("o1",)})
    assert len(runner.run(_ctx(runner, trucks, orders)).pairs) == 1
    assert runner.stats["calls"] == 4 and runner.stats["violations"] == 3
    assert runner.stats["total_ms"] >= runner.stats["max_ms"] >= 0.0


def test_runner_online_timing_commits_heads_only():
    """Heads, in chain-insertion order, as the SAME raw document objects.

    Verified to have teeth: committing every chain element (tails included) in
    ``compose.heads_to_pairs`` (``DispatchRunner._commit_heads`` before round 2) failed this test.
    """
    trucks, orders = _world()
    runner = DispatchRunner(policy=_Scripted(Plan(chains={"t2": ("o3", "o1"), "t1": ("o2",)})),
                            resolved=_resolved())
    got = runner.run(_ctx(runner, trucks, orders)).pairs
    assert [(t["_id"], o["_id"]) for t, o in got] == [("t2", "o3"), ("t1", "o2")]
    assert got[0][0] is trucks[1] and got[0][1] is orders[2]
    with pytest.raises(PolicyTimingError):
        DispatchRunner(policy=_Scripted(None), resolved=_resolved(timing="plan"))


def test_runner_propagates_non_contract_exceptions():
    """Verified to have teeth: widening the except to ``Exception`` failed this test."""
    trucks, orders = _world()
    runner = DispatchRunner(policy=_Scripted(KeyError("boom")), resolved=_resolved())
    with pytest.raises(KeyError, match="boom"):
        runner.run(_ctx(runner, trucks, orders))
    assert runner.stats["violations"] == 0 and runner.stats["calls"] == 1


def test_runner_calls_capture_only_when_given():
    trucks, orders = _world()
    seen = []
    plan = Plan(chains={"t1": ("o1",)})
    runner = DispatchRunner(policy=_Scripted(plan), resolved=_resolved(),
                            capture=lambda **kw: seen.append(kw))
    ctx = _ctx(runner, trucks, orders)
    runner.run(ctx)
    assert seen == [{"ctx": ctx, "plan": plan, "problems": []}]


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
def test_pair_solver_default_decide_reproduces_the_legacy_call(kind):
    """The adapter hands ``solve``/``solve_pairs`` exactly what the inline code did."""
    trucks = [_truck(f"t{i}", "acme", 103.85 + i * 0.01, 1.30) for i in range(3)]
    orders = [_order(f"o{i}", "acme", 103.85 + i * 0.012, 1.30) for i in range(4)]
    policy = _FirstFeasible()
    runner = DispatchRunner(policy=policy, resolved=_resolved(kind=kind))
    rule = PartitionRule(max_travel_time_pickup=None, adjacency={})
    random.seed(3)
    got = runner.run(_ctx(runner, trucks, orders, rule)).pairs
    if kind == "sampled":
        want = policy.solve(trucks, orders, pair_allowed=rule.allowed, cost=assign_constraints.assignment_cost)
    else:
        from apps.container_logistics.policies.dispatch.helpers.candidates import spatial_pairs_for_partition
        pairs = spatial_pairs_for_partition(trucks, orders, rule=rule, settings=CandidateSettings.from_profile({}))
        want = policy.solve_pairs(pairs, cost=assign_constraints.assignment_cost)
    assert [(t["_id"], o["_id"]) for t, o in got] == [(t["_id"], o["_id"]) for t, o in want]
    assert got
