"""Runtime fixes from the final review of the policy-slots branch (MAJOR-4, MAJOR-5).

Recorded in ``docs/plans/policy_slots_round2_whole_tick.md``, "Final review fixes
(runtime)". What is locked:

- MAJOR-4: ``Tick.restrict`` (the generic successor of ``scopes.partition_context``,
  WS1) takes the cap-sample RNG as a REQUIRED keyword (``sample_rng``). ``Partitioned``
  passes the ``random`` module (byte identity, golden); a third-party composite built on
  ``tick.restrict`` / ``tick.call`` that passes its decision RNG (``tick.rng``) and
  injects it into its inner passes the battery's ``determinism.no_module_rng`` WITHOUT
  declaring ``module_rng``, and fails it when it passes the module instead.
- MAJOR-5 (FIX-12): every ``PooledMarket`` company solve goes through ``tick.execute``
  (``Tick.call`` offloads a child that offloads ``self``), so ``solve_in_thread``
  offloads each one exactly once (never nested), and the pairs, the share tags and the
  module-RNG state are identical with the flag on and off.
"""

from __future__ import annotations

import inspect
import random
from dataclasses import dataclass

import pytest

import apps.container_logistics.assignment.app as app_module
from apps.container_logistics.assignment.solver import GreedyNearestSolver, RandomAssignmentSolver
from apps.container_logistics.policies.dispatch.bind import Declaration
from apps.container_logistics.policies.dispatch.composite import CompositePolicy
from apps.container_logistics.policies.dispatch.library.partitioned import cooperation_groups
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.tick import Tick
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.registry import get_policy_spec, isolated_registry
from apps.container_logistics.policies.solver_lib import cooperation
from apps.container_logistics.policies.testing import battery
from tests.test_policy_round2_c3_review_fixes import _CLOCK, _ids, _selection_profile, _tick_a_app


# --- MAJOR-4: the partition scope's sample RNG is the caller's ------------------------


def test_restrict_has_no_silent_sample_rng_default():
    """``sample_rng`` is keyword-only and required: every call site names its choice.

    Verified to have teeth: giving ``Tick.restrict``'s parameter a default
    (``sample_rng: Any = None``) failed this test.
    """
    p = inspect.signature(Tick.restrict).parameters["sample_rng"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty


@pytest.fixture
def group_composite():
    """``GroupsVia``: a third-party per-group composite built only on ``tick.restrict`` /
    ``tick.call`` (and ``Partitioned``'s ``cooperation_groups``), whose inner is the
    shipped ``RandomAssignment`` (``candidates=sampled``: it shuffles through
    ``self.rng`` and caps through ``solver_lib.candidates.tick_sampled_trucks``).
    ``GroupsVia.sample_with`` picks what it passes as ``sample_rng``: ``"decision"`` (the
    tick ``tick.rng``, also injected into the inner) or ``"module"`` (the ``random``
    module, as ``Partitioned`` must for byte identity)."""
    with isolated_registry():
        @policy(slot="planner", name="GroupsVia", version="1", information_model="per_group",
                description="one decision per cooperation group, drawing from the tick RNG")
        class GroupsVia(CompositePolicy):
            sample_with = "decision"

            @dataclass(frozen=True)
            class Params(PolicyParams):
                inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True,
                                         help="the per-group solver")

            def bind(self, params, world, *, top):
                arm = cooperation.resolve_arm(world)
                return Declaration(cross_company=cooperation.directed_pairs(arm) if top else frozenset())

            def solve_tick(self, tick, params):
                self.inner.set_rng(tick.rng)
                self.inner.set_tiebreak(tick.seed)  # the full recipe (verification review MINOR-B)
                sample_rng = tick.rng if GroupsVia.sample_with == "decision" else random
                arm = cooperation.resolve_arm(tick.world)
                pairs = []
                for group in cooperation_groups(tick, arm.component_of if arm is not None else {}):
                    kind, value = group.key
                    sub = tick.restrict(trucks=group.trucks, orders=group.orders,
                                        scope=f"partition:{kind}:{value}", sample_rng=sample_rng)
                    result = tick.call(params.inner, sub)
                    if result is not None:
                        pairs.extend(result.pairs)
                return Result(pairs=tuple(pairs))

        yield GroupsVia


def _no_module_rng(name):
    spec = get_policy_spec("planner", name)
    with battery._quiet_runner_violations():
        return battery._no_module_rng(battery._Outcomes(spec, {}))


def test_a_composite_passing_its_decision_rng_passes_no_module_rng_undeclared(group_composite):
    """The battery's own ``determinism.no_module_rng`` check, on every robustness fixture
    (``scale`` has 1000 trucks in one group, over the 500-truck sample cap): PASS with
    ``sample_rng=ctx.rng`` and no ``module_rng`` declaration; FAIL with the module.

    Verified to have teeth: making ``Tick.sample_rng`` ignore the caller's argument and
    return the ``random`` module on a sub-tick (the pre-fix hardcoding) failed the
    decision case (``decide drew from the random MODULE on scale``).
    """
    assert get_policy_spec("planner", "GroupsVia").module_rng is None
    group_composite.sample_with = "decision"
    ok, detail = _no_module_rng("GroupsVia")
    assert ok and not detail.startswith("skipped"), detail

    group_composite.sample_with = "module"
    ok, detail = _no_module_rng("GroupsVia")
    assert not ok and "scale" in detail, detail


# --- MAJOR-5: PooledMarket company solves go through tick.execute ---------------------


class _CountingTpool:
    """A synchronous stand-in for ``eventlet.tpool``: counts calls and marks "inside"."""

    calls = 0
    depth = 0

    @classmethod
    def execute(cls, fn, *args):
        cls.calls += 1
        cls.depth += 1
        try:
            return fn(*args)
        finally:
            cls.depth -= 1


_POOLED_INNERS = {
    "GreedyNearest": {"type": "PooledMarket", "params": {"inner": {"type": "GreedyNearest"}}},
    "RandomAssignment": {"type": "PooledMarket", "params": {"inner": {"type": "RandomAssignment"}}},
}


@pytest.mark.parametrize("inner", sorted(_POOLED_INNERS))
def test_pooled_market_offloads_every_company_solve_exactly_once(inner, monkeypatch):
    """Golden tick A, pooled, ``solve_in_thread`` on: one ``tpool.execute`` per company
    solve (the inner's ``solve_tick`` on a ``company:`` sub-tick), each solve inside
    exactly one offload, and nothing else offloaded. With the flag off nothing is
    offloaded. The pairs, the share tags and the ``random`` module state after the tick
    are identical.

    Verified to have teeth: calling the child's ``solve_tick`` directly in ``Tick.call``
    (ignoring ``offload == "self"``) failed both cases (0 offloads against the company
    solves); offloading ``children`` nodes in ``DispatchRunner._solve`` too (a nested
    offload) failed both (one offload more, every solve at depth 2).
    """
    cls = {"GreedyNearest": GreedyNearestSolver, "RandomAssignment": RandomAssignmentSolver}[inner]
    real = cls.solve_tick
    solves = []

    def counting_solve_tick(self, tick, params):
        solves.append((tick.scope.partition(":")[0], _CountingTpool.depth))
        return real(self, tick, params)

    monkeypatch.setattr(cls, "solve_tick", counting_solve_tick)
    monkeypatch.setattr(app_module, "_eventlet_tpool", _CountingTpool)

    def run(threaded):
        _CountingTpool.calls = 0
        solves.clear()
        app = _tick_a_app(_selection_profile(_POOLED_INNERS[inner], solve_in_thread=threaded))
        random.seed(2)
        pairs = _ids(app.assign(_CLOCK, 6))
        return pairs, dict(app._share_tags), random.getstate(), _CountingTpool.calls, list(solves)

    on_pairs, on_tags, on_state, on_calls, on_solves = run(True)
    off_pairs, off_tags, off_state, off_calls, off_solves = run(False)

    assert on_solves and {kind for kind, _ in on_solves} == {"company"}
    assert on_calls == len(on_solves)
    assert all(depth == 1 for _, depth in on_solves), on_solves
    assert off_calls == 0 and all(depth == 0 for _, depth in off_solves)
    assert len(off_solves) == len(on_solves)
    assert on_pairs == off_pairs and on_pairs
    assert on_tags == off_tags
    assert on_state == off_state
