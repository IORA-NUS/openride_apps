"""Verification review fixes of round 2 (``docs/plans/policy_slots_round2_whole_tick.md``,
"Verification review fixes").

What is locked:

- MINOR-A: the tick documents are READ-ONLY for a policy (``DispatchPolicy`` docstring).
  The shipped selections honour it; and the boundary is pinned: a composite that moves
  a truck for a sub-solve and restores it gets the sub-scope's physical verdict at the
  tick (``DispatchWorld.tick_allowed`` re-checks only who may serve whom). That is the
  documented contract, not an accident: a per-head fingerprint cost more than the
  shortcut saves. If the shortcut ever gains a fingerprint, invert that test.
- MINOR-B: the composite recipe is ``sample_rng=ctx.rng``, ``inner.set_rng(ctx.rng)``
  AND ``inner.set_tiebreak(ctx.tick_seed)``; without the tiebreak a GreedyNearest inner
  makes the composite fail ``determinism.order_independent``.
- MINOR-C: ``determinism.no_module_rng`` attributes a draw to the node whose method is
  on the stack by the method's FIRST PARAMETER, whatever its name (``decide(me, ctx)``);
  a scope a node builds for itself is that node's own draw, not its parent's scope
  sample; a thread a node starts during the decision is that node's, and a draw from a
  thread no node started is "provenance cannot be separated" (SKIP), never a FAIL.
- NITs: the ``policies_format`` marker is the int 2 only (``2.0``, ``"2"``, ``True``,
  ``3`` are "unknown format"; an absent one is "compiled before round 2"); the control
  message says ``N passed, k skipped``.
"""

from __future__ import annotations

import copy
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch import compose, scopes
from apps.container_logistics.policies.dispatch import runner as runner_module
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.composite import CompositePolicy
from apps.container_logistics.policies.dispatch.legacy import check_policies_format
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.registry import get_policy_spec, isolated_registry
from apps.container_logistics.policies.testing import battery
from apps.container_logistics.policies.testing.battery import CheckResult
from openride_control import policies as control_policies
from tests.test_policy_round2_c3_review_fixes import (
    _CENTRAL,
    _CLOCK,
    _PARTITIONED,
    _POOLED,
    _selection_profile,
    _tick_a_app,
)


def _greedy(ctx):
    """A deterministic central greedy over the context's candidates (id order)."""
    used_t, used_o, chains = set(), set(), {}
    for t, o in sorted(ctx.candidate_pairs(), key=lambda p: (str(p[0]["_id"]), str(p[1]["_id"]))):
        tid, oid = str(t["_id"]), str(o["_id"])
        if tid not in used_t and oid not in used_o:
            used_t.add(tid)
            used_o.add(oid)
            chains[tid] = (oid,)
    return Plan(chains=chains)


# --- MINOR-A: the documents are read-only -----------------------------------------------


@pytest.mark.parametrize("selection", [_PARTITIONED, _POOLED, _CENTRAL], ids=["Partitioned", "PooledMarket", "central"])
def test_the_shipped_selections_leave_the_tick_documents_unchanged(selection, monkeypatch):
    """Every truck and order dict a decision sees is byte-equal before and after it.

    Verified to have teeth: a ``t.setdefault("profile", {})["seen"] = True`` in
    ``GreedyNearest``'s solve failed all three cases.
    """
    real_run = runner_module.DispatchRunner.run
    seen = []

    def run(self, ctx):
        before = copy.deepcopy((list(ctx.raw_trucks), list(ctx.raw_orders)))
        out = real_run(self, ctx)
        seen.append(before == (list(ctx.raw_trucks), list(ctx.raw_orders)))
        return out

    monkeypatch.setattr(runner_module.DispatchRunner, "run", run)
    app = _tick_a_app(_selection_profile(selection))
    random.seed(11)
    assert app.assign(_CLOCK, 5), "vacuous: tick A must commit pairs"
    assert seen == [True], "a shipped policy mutated the tick's documents (they are read-only)"


@pytest.fixture
def moving_composite():
    """``MoveForSubSolve``: per cooperation group, a scripted inner pairs each truck with
    the first structure-allowed order; with ``move`` on, the composite puts every chosen
    truck at its order's pickup for the sub-solve and restores it afterwards."""
    script: dict[str, str] = {}
    with isolated_registry():
        @policy(slot="planner", name="Scripted", version="1", information_model="central",
                description="returns the composite's script")
        class Scripted(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                return Plan(chains={t: (o,) for t, o in script.items()})

        @policy(slot="planner", name="MoveForSubSolve", version="1", information_model="per_group",
                description="moves trucks during a sub-solve (out of contract)")
        class MoveForSubSolve(CompositePolicy):
            move = False

            @dataclass(frozen=True)
            class Params(PolicyParams):
                inner: PolicyRef = param(ref("Scripted"), kind="planner", leaf_only=True, help="inner")

            def decide(self, ctx):
                chains = {}
                for group in compose.cooperation_groups(ctx):
                    script.clear()
                    saved, used = [], set()
                    for t in sorted(group.trucks, key=lambda d: str(d["_id"])):
                        for o in sorted(group.orders, key=lambda d: str(d["_id"])):
                            if str(o["_id"]) in used or not ctx.world.structure_allows(t, o):
                                continue
                            if not (assign_constraints.size_compatible(t, o)
                                    and not assign_constraints.restricted_area_blocks(t, o)):
                                continue
                            used.add(str(o["_id"]))
                            script[str(t["_id"])] = str(o["_id"])
                            if MoveForSubSolve.move:
                                prof = t.setdefault("profile", {})
                                saved.append((prof, prof.get("current_loc")))
                                prof["current_loc"] = copy.deepcopy(assign_constraints.order_pickup_loc(o))
                            break
                    sub = scopes.partition_context(
                        policy=self.inner, params=self.inner_node.params,
                        candidate_kind=self.inner_node.candidate_kind, trucks=list(group.trucks),
                        orders=list(group.orders), partition_key=group.key, rule=ctx.world.rule,
                        settings=ctx.candidate_settings, accounting_cost=ctx.accounting_cost,
                        clock=ctx.clock, sample_rng=ctx.rng, parent=ctx)
                    plan = ctx.execute(compose.decide_in_scope, self.inner, self.inner_node, sub)
                    for prof, old in saved:
                        if old is None:
                            prof.pop("current_loc", None)
                        else:
                            prof["current_loc"] = old
                    if plan is not None:
                        chains.update(plan.chains)
                return Plan(chains=chains)

        yield MoveForSubSolve


@pytest.mark.parametrize("move", [False, True], ids=["read-only", "moves-and-restores"])
def test_mutating_documents_during_a_sub_solve_is_outside_the_contract(moving_composite, move):
    """The documented boundary of the tick check's shortcut (MINOR-A).

    Golden tick A with a 300 s pickup limit. Read-only, the scripted pairs that break
    the limit are refused in the sub-scope and nothing infeasible is committed. Moving
    the trucks to the pickups for the sub-solve and restoring them gets those heads
    proved in the sub-scope, and the tick check (structure only, for proved heads)
    commits them. This is why ``DispatchPolicy`` says the documents are read-only.
    If ``tick_allowed`` ever keys on a fingerprint of the documents, both cases commit
    nothing infeasible: change the second expectation and drop the contract text.

    Verified to have teeth: making ``tick_allowed`` ignore ``verified`` (full check on
    every head) committed 0 infeasible pairs in the moving case.
    """
    moving_composite.move = move
    prof = _selection_profile({"type": "MoveForSubSolve"})
    prof["max_travel_time_pickup"] = 300
    app = _tick_a_app(prof)
    pairs = app.assign(_CLOCK, 6)
    infeasible = [(str(t["_id"]), str(o["_id"])) for t, o in pairs
                  if not assign_constraints.pair_allowed_ignoring_haulier(t, o, max_travel_time_pickup=300)]
    if move:
        assert infeasible, "the shortcut re-checked the physical half: update the read-only contract text"
    else:
        assert infeasible == [], infeasible


# --- MINOR-B: the full composite recipe --------------------------------------------------


def _recipe_composite(name: str, *, tiebreak: bool):
    @policy(slot="planner", name=name, version="1", information_model="per_group",
            description="per-group composite following the recipe")
    class Recipe(CompositePolicy):
        @dataclass(frozen=True)
        class Params(PolicyParams):
            inner: PolicyRef = param(ref("GreedyNearest"), kind="planner", leaf_only=True, help="inner")

        def decide(self, ctx):
            self.inner.set_rng(ctx.rng)
            if tiebreak:
                self.inner.set_tiebreak(ctx.tick_seed)
            chains = {}
            for group in compose.cooperation_groups(ctx):
                sub = scopes.partition_context(
                    policy=self.inner, params=self.inner_node.params,
                    candidate_kind=self.inner_node.candidate_kind, trucks=list(group.trucks),
                    orders=list(group.orders), partition_key=group.key, rule=ctx.world.rule,
                    settings=ctx.candidate_settings, accounting_cost=ctx.accounting_cost,
                    clock=ctx.clock, sample_rng=ctx.rng, parent=ctx)
                plan = ctx.execute(compose.decide_in_scope, self.inner, self.inner_node, sub)
                if plan is not None:
                    chains.update(plan.chains)
            return Plan(chains=chains)

    return Recipe


def test_the_composite_recipe_needs_the_tiebreak_for_order_independence():
    """``set_rng`` + ``sample_rng=ctx.rng`` alone fails ``order_independent`` with a
    GreedyNearest inner (its ``self.rng.shuffle`` tiebreak sees the input order); adding
    ``set_tiebreak(ctx.tick_seed)`` passes it, and ``no_module_rng`` passes undeclared."""
    with isolated_registry():
        _recipe_composite("FullRecipe", tiebreak=True)
        _recipe_composite("NoTiebreak", tiebreak=False)
        full = get_policy_spec("planner", "FullRecipe")
        ok, detail = battery._order_independent(full, {})
        assert ok and not isinstance(ok, battery.Skip), detail
        ok, detail = battery._no_module_rng(battery._Outcomes(full, {}))
        assert ok and not isinstance(battery._no_module_rng(battery._Outcomes(full, {})), battery.Skip), detail
        ok, detail = battery._order_independent(get_policy_spec("planner", "NoTiebreak"), {})
        assert not ok and "changed the plan" in detail, detail


# --- MINOR-C: draw attribution -------------------------------------------------------------


def _probe_leaf(name: str, draw, *, declare: bool = False):
    """A central leaf that calls ``draw(self)`` then plans greedily."""
    extra = {"module_rng": "declared on purpose"} if declare else {}

    @policy(slot="planner", name=name, version="1", description="probe", information_model="central", **extra)
    class Leaf(DispatchPolicy):
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def decide(self, ctx):
            draw(self, ctx)
            return _greedy(ctx)

    return Leaf


def _under_partitioned(name: str):
    return battery._no_module_rng(battery._Outcomes(get_policy_spec("planner", "Partitioned"),
                                                    {"inner": {"type": name}}))


def _thread_draw(_self, _ctx):
    worker = threading.Thread(target=random.random)
    worker.start()
    worker.join()


def test_a_renamed_self_is_still_attributed_to_its_node():
    """``def decide(me, ctx)`` drawing from the module FAILS under Partitioned, naming the leaf.

    Before the fix the frame walk looked for a local named ``self``, skipped the leaf's
    frame and credited its draws to Partitioned (SKIP, "direct draw(s) by the declaring
    node(s) Partitioned"). Verified to have teeth: matching ``frame.f_locals.get("self")``
    again in ``_attribute_frame`` gave that SKIP.
    """
    with isolated_registry():
        @policy(slot="planner", name="RenamedSelf", version="1", description="probe", information_model="central")
        class RenamedSelf(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(me, ctx):  # noqa: N805 - the point of the test
                random.random()
                return _greedy(ctx)

        ok, detail = _under_partitioned("RenamedSelf")
    assert not ok and "RenamedSelf on" in detail, detail


def test_a_scope_a_node_builds_for_itself_is_its_own_draw():
    """A leaf that builds its own ``partition_context(sample_rng=random)`` and samples FAILS
    under Partitioned (it is not Partitioned's scope sample).

    Verified to have teeth: dropping the ``_handed_to`` test in ``_attribute_frame`` (any
    non-tick context on the stack is a scope sample) credited it to Partitioned (SKIP).
    """
    def own_scope(self, ctx):
        sub = scopes.partition_context(
            policy=self, params=self.params, candidate_kind="sampled", trucks=list(ctx.raw_trucks),
            orders=list(ctx.raw_orders), partition_key=("haulier", "x"), rule=ctx.world.rule,
            settings=ctx.candidate_settings, accounting_cost=ctx.accounting_cost, clock=ctx.clock,
            sample_rng=random, parent=ctx)
        sub.sampled_trucks()

    with isolated_registry():
        _probe_leaf("OwnScope", own_scope)
        ok, detail = _under_partitioned("OwnScope")
    assert not ok and "OwnScope on" in detail, detail


def test_a_thread_a_node_starts_is_attributed_to_that_node():
    """A declaring leaf drawing in its own thread is SKIP (its own battery and under
    Partitioned); an undeclared one FAILS under Partitioned, named, not "<no policy frame>".

    Verified to have teeth: removing the ``threading.Thread.start`` wrapper in
    ``_attributed_module_draws`` made the undeclared leaf's draws unattributed (SKIP).
    """
    with isolated_registry():
        _probe_leaf("ThreadDeclared", _thread_draw, declare=True)
        _probe_leaf("ThreadUndeclared", _thread_draw)
        own = battery._no_module_rng(battery._Outcomes(get_policy_spec("planner", "ThreadDeclared"), {}))
        assert isinstance(own, battery.Skip) and "direct draw(s) by the declaring node(s) ThreadDeclared" in own.detail, own
        nested = _under_partitioned("ThreadDeclared")
        assert isinstance(nested, battery.Skip), nested
        ok, detail = _under_partitioned("ThreadUndeclared")
    assert not ok and "ThreadUndeclared on" in detail and "<no policy frame>" not in detail, detail


def test_a_draw_from_a_thread_no_node_started_is_unseparable_not_a_failure():
    """A declaring leaf that hands its draw to a pool started BEFORE the decision: the
    draw has no policy frame and no spawner, so it is "provenance cannot be separated"
    (SKIP), not the old false FAIL.

    Verified to have teeth: putting unattributed draws back into the rogue set
    (``rogue["<no policy frame>"]``) failed it.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    pool.submit(lambda: None).result()  # its one worker thread exists before any probe
    try:
        with isolated_registry():
            _probe_leaf("PoolDeclared", lambda self, ctx: pool.submit(random.random).result(), declare=True)
            res = battery._no_module_rng(battery._Outcomes(get_policy_spec("planner", "PoolDeclared"), {}))
    finally:
        pool.shutdown()
    assert isinstance(res, battery.Skip), res
    assert "no policy frame on the stack and no spawning node" in res.detail, res.detail


# --- NITs ---------------------------------------------------------------------------------


@pytest.mark.parametrize("marker", [2.0, "2", True, 3, 1, [2]], ids=repr)
def test_a_marker_other_than_the_int_2_is_an_unknown_format(marker):
    """Verified to have teeth: the old ``isinstance(marker, bool) or marker != 2`` accepted 2.0
    (and said "compiled before round 2" for the others)."""
    with pytest.raises(PolicyError, match=re.escape(f"policies_format={marker!r}: unknown format")):
        check_policies_format({"policies": {"planner": {"type": "GreedyNearest"}}, "policies_format": marker})


def test_an_absent_marker_is_a_round1_bundle_and_the_int_2_is_accepted():
    profile = {"policies": {"planner": {"type": "GreedyNearest"}}}
    with pytest.raises(PolicyError, match="policies_format=None, not 2: it was compiled before round 2"):
        check_policies_format(profile)
    check_policies_format({**profile, "policies_format": 2})
    check_policies_format({"strategy": "GreedyNearest"})  # legacy: no policies block, no marker needed


@pytest.mark.parametrize("skipped,want", [(1, "1 passed, 1 skipped"), (0, "all 2 checks passed")])
def test_the_control_message_counts_passes_and_skips_apart(monkeypatch, skipped, want):
    """Verified to have teeth: the old ``all N checks passed (k skipped)`` failed the first case."""
    from apps.container_logistics.policies.testing import battery as battery_mod

    rows = [CheckResult("determinism", "no_module_rng", True, "d", 0.1, skipped=bool(skipped)),
            CheckResult("structure", "returns_plan", True, "fine", 0.1)]
    monkeypatch.setattr(battery_mod, "run_battery", lambda slot, name: rows)
    assert control_policies.cmd_policy_validate("planner", "GreedyNearest")["message"] == want
