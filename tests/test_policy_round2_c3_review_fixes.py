"""Runtime fixes from the C3 review of round 2 (``docs/plans/policy_slots_round2_whole_tick.md``,
"Review fixes after C3 (runtime)").

What is locked:

- #2 (performance, without weakening the guarantee): the tick check skips only the
  PHYSICAL half of a head a sub-scope check already proved, under a rule that proves it
  with the world's pickup limit; the who-may-serve-whom half is always re-checked, and
  a central solver's heads are checked in full. The share accounting is unchanged.
- #4: a reused top whose composites are stale (a profile patched after construction)
  is rebuilt around the same leaf, so what RUNS is what ``runner.resolved`` reports.
- #5: ``solve_in_thread`` reaches every top-level solver exactly once per decision
  (``ctx.execute``); a decomposition keeps its own per-scope offloading.
- #6: a contract-violation tick does not log the haulier-scoping ERROR and keeps its
  one-shot armed.
- #3: the tick-scope sampling cap covers ``cap`` trucks per company (the documented
  numbers on the golden chain tick).
"""

from __future__ import annotations

import copy
import itertools
import logging
import random
from dataclasses import dataclass

import pytest

import apps.container_logistics.assignment.app as app_module
from apps.container_logistics.assignment import accounting
from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.policies.dispatch import compose, runner as runner_module, scopes
from apps.container_logistics.policies.dispatch.allowed import PartitionRule
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.composite import CompositePolicy, innermost_policy
from apps.container_logistics.policies.dispatch.context import DispatchContext
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.profile import build_policy_tree, resolve_dispatch_for_profile
from apps.container_logistics.policies.dispatch.world import DispatchWorld
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.registry import isolated_registry
from tests import test_dispatch_golden_identity as _golden
from tests.golden.harness import RUN_ID, StubManager, StubMessenger
from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_whole_tick_dispatch import _app as _plain_app
from tests.test_whole_tick_dispatch import _base_profile

_FIXTURE = _golden._FIXTURE
_CASES = {c["case_id"]: c for c in _FIXTURE["cases"]}
_TICKS = _FIXTURE["tick_states"]
_TICK_A = _TICKS["A"]
_CLOCK = _TICK_A["sim_clock"]
_CHAIN_CASE = "A-greedy-partitioned-chain-spatial-cap500"


def _selection_profile(selection, *, case_id=_CHAIN_CASE, **extra):
    """The golden case's profile with ``policies.planner = selection`` (legacy selection keys removed)."""
    prof = copy.deepcopy(_CASES[case_id]["profile"])
    prof.pop("strategy", None)
    prof.pop("planner", None)
    prof["policies"] = {"planner": copy.deepcopy(selection)}
    prof.update(extra)
    return prof


def _tick_a_app(profile, solver=None):
    """A real ``AssignmentApp`` on golden tick A; ``_solver`` is the profile's full tree (as ``__init__``)."""
    state = copy.deepcopy(_TICK_A)
    app = AssignmentApp.__new__(AssignmentApp)
    app.behavior = {"profile": profile}
    app.run_id = RUN_ID
    app.manager = StubManager(state["trucks"], state["orders"], state["claimed_order_ids"], state["busy_ids"])
    app.messenger = StubMessenger()
    app._solver = solver if solver is not None else build_policy_tree(resolve_dispatch_for_profile(profile))
    app._solver_params = profile.get("solver_params") or {}
    app._warned_haulier_block = False
    app._share_tags = {}
    return app


def _ids(pairs):
    return [(str(t["_id"]), str(o["_id"])) for t, o in pairs]


_PARTITIONED = {"type": "Partitioned", "params": {"inner": {"type": "GreedyNearest"}}}
_POOLED = {"type": "PooledMarket", "params": {"inner": {"type": "GreedyNearest"}}}
_CENTRAL = {"type": "GreedyNearest"}


# --- #2: the tick check's duplicate physical work ----------------------------------------


def _physical(world, t, o):
    return assign_constraints.pair_allowed_ignoring_haulier(t, o, max_travel_time_pickup=world.max_travel_time_pickup)


@pytest.mark.parametrize("coop", [True, False], ids=["chain", "no-coop"])
def test_world_allowed_is_exactly_its_structure_half_and_its_physical_half(coop):
    """The identity ``DispatchWorld.tick_allowed`` relies on, over real tick documents.

    Verified to have teeth: dropping the ``share_eligible`` branch from
    ``DispatchWorld.structure_allows`` failed the chain case; returning True for a
    haulier-less document failed both.
    """
    prof = copy.deepcopy(_CASES[_CHAIN_CASE]["profile"])
    if not coop:
        prof.pop("cooperation", None)
    world = DispatchWorld.from_profile(prof, accounting_params={})
    trucks = _TICK_A["trucks"][::4]
    orders = _TICK_A["orders"][::3]
    stray = copy.deepcopy(trucks[0])
    stray["haulier_id"] = None
    stray.setdefault("profile", {})["haulier_id"] = None
    cross_allowed = 0
    for t, o in itertools.product(trucks + [stray], orders):
        want = world.allowed(t, o)
        assert want == (world.structure_allows(t, o) and _physical(world, t, o)), (t["_id"], o["_id"])
        if want and assign_constraints.haulier_of(t) != assign_constraints.haulier_of(o):
            cross_allowed += 1
    if coop:
        assert cross_allowed > 0, "vacuous: no allowed cross pair exercised the edge branch"


def _spy_tick_check(monkeypatch):
    """Count physical / structure calls made by the TICK-level ``check_plan`` only."""
    seen = {"physical": 0, "structure": 0, "heads": 0}
    real_check = runner_module.check_plan
    real_physical = assign_constraints.pair_allowed_ignoring_haulier
    real_structure = DispatchWorld.structure_allows
    inside = []

    def physical(*a, **kw):
        if inside:
            seen["physical"] += 1
        return real_physical(*a, **kw)

    def structure(self, t, o):
        if inside:
            seen["structure"] += 1
        return real_structure(self, t, o)

    def check(plan, ctx):
        inside.append(True)
        try:
            seen["heads"] += len(plan.heads())
            return real_check(plan, ctx)
        finally:
            inside.pop()

    monkeypatch.setattr(assign_constraints, "pair_allowed_ignoring_haulier", physical)
    monkeypatch.setattr(DispatchWorld, "structure_allows", structure)
    monkeypatch.setattr(runner_module, "check_plan", check)
    return seen


@pytest.mark.parametrize("selection,proved", [(_PARTITIONED, True), (_POOLED, True), (_CENTRAL, False)],
                         ids=["Partitioned", "PooledMarket", "central"])
def test_tick_check_repeats_no_physical_work_a_scope_proved_but_checks_central_heads_in_full(
        selection, proved, monkeypatch):
    """Decomposition heads re-check only who may serve whom; a central leaf's heads get the full rule.

    Verified to have teeth: removing the ``_record_verified_heads`` call from
    ``compose.decide_in_scope`` failed both decomposition cases (one physical call per
    head); recording heads from ``runner.run`` too (for any solver) failed the central case.
    """
    seen = _spy_tick_check(monkeypatch)
    app = _tick_a_app(_selection_profile(selection))
    random.seed(11)
    pairs = app.assign(_CLOCK, 5)
    assert pairs and seen["heads"] == len(pairs), seen
    if proved:
        assert seen == {"physical": 0, "structure": len(pairs), "heads": len(pairs)}, seen
    else:
        assert seen["physical"] == len(pairs) and seen["structure"] == 0, seen


@pytest.fixture
def wide_scope_composite():
    """A composite that decides ONE sub-scope under a caller-chosen ``PartitionRule``, with a
    leaf that pairs every truck with the first order (honest under that rule)."""
    with isolated_registry():
        @policy(slot="planner", name="FirstOrder", version="1", information_model="central",
                description="every truck takes the first order it is allowed")
        class FirstOrder(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                for t in ctx.raw_trucks:
                    for o in ctx.raw_orders:
                        if ctx.allowed_raw(t, o):
                            return Plan(chains={str(t["_id"]): (str(o["_id"]),)})
                return Plan(chains={})

        @policy(slot="planner", name="OwnRuleScope", version="1", information_model="per_group",
                description="one sub-scope under OwnRuleScope.rule")
        class OwnRuleScope(CompositePolicy):
            rule = None

            @dataclass(frozen=True)
            class Params(PolicyParams):
                inner: PolicyRef = param(ref("FirstOrder"), kind="planner", leaf_only=True, help="inner")

            def decide(self, ctx):
                sub = scopes.partition_context(
                    policy=self.inner, params=self.inner_node.params,
                    candidate_kind=self.inner_node.candidate_kind, trucks=list(ctx.raw_trucks),
                    orders=list(ctx.raw_orders), partition_key=("all", 0), rule=OwnRuleScope.rule,
                    settings=ctx.candidate_settings, accounting_cost=ctx.accounting_cost,
                    sample_rng=random, clock=ctx.clock, parent=ctx)
                plan = compose.decide_in_scope(self.inner, self.inner_node, sub)
                return plan if plan is not None else Plan(chains={})

        yield OwnRuleScope


def test_a_sub_scope_with_a_wider_structure_cannot_widen_the_tick(wide_scope_composite, caplog):
    """The sub-scope rule lets cargo serve acme; the world does not. The framework refuses it.

    Verified to have teeth: returning True on a ``verified_heads`` hit in
    ``DispatchWorld.tick_allowed`` (instead of re-checking ``structure_allows``)
    committed the cargo truck to acme's order.
    """
    trucks = [_truck("t_cargo", "cargo", 103.85, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    profile = _base_profile(policies={"planner": {"type": "OwnRuleScope"}},
                            cooperation=_coop([("acme", "borax")], active="s1"))
    wide_scope_composite.rule = PartitionRule(max_travel_time_pickup=None,
                                              adjacency={"cargo": {"acme"}, "acme": {"cargo"}})
    app = _plain_app(trucks, orders, profile)
    with caplog.at_level(logging.ERROR):
        assert app.assign("Wed, 01 Jan 2020 08:00:00 GMT", 4) == []
    assert app._dispatch_runner(profile).stats["violations"] == 1
    assert any("head pair ('t_cargo', 'o_acme') is not allowed" in r.getMessage() for r in caplog.records)


def test_a_sub_scope_with_another_pickup_limit_does_not_vouch_for_the_physical_half(wide_scope_composite, caplog):
    """An own-company pair 0.5 degrees away: allowed with no pickup limit, refused by the world's 60 s.

    Verified to have teeth: dropping the ``max_travel_time_pickup`` comparison from
    ``DispatchWorld.proves_physical`` committed the pair.
    """
    trucks = [_truck("t_acme", "acme", 103.35, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    profile = _base_profile(policies={"planner": {"type": "OwnRuleScope"}}, max_travel_time_pickup=60)
    wide_scope_composite.rule = PartitionRule(max_travel_time_pickup=None, adjacency={})
    app = _plain_app(trucks, orders, profile)
    world = app._dispatch_world(profile)
    assert not world.allowed(trucks[0], orders[0]), "vacuous: the world must refuse the pair"
    with caplog.at_level(logging.ERROR):
        assert app.assign("Wed, 01 Jan 2020 08:00:00 GMT", 4) == []
    assert app._dispatch_runner(profile).stats["violations"] == 1


def _reference_accounts(pairs, candidates, cost, own_allowed):
    """The pre-fix accounting: every owner truck, re-filtered against the commits per share."""
    committed = {str(t["_id"]) for t, _o in pairs}
    out = []
    for t, o in pairs:
        carrier, owner = assign_constraints.haulier_of(t), assign_constraints.haulier_of(o)
        if not (carrier and owner and carrier != owner):
            continue
        fleet = [c for c in candidates if assign_constraints.haulier_of(c) == owner]
        reserve = accounting.owner_reserve_km(o, fleet, committed, pair_allowed=own_allowed, cost=cost)
        out.append(accounting.ShareAccount(str(t["_id"]), str(o["_id"]), owner, carrier, reserve,
                                           float(cost(t, o))))
    return out


@pytest.mark.parametrize("selection,priced", [(_PARTITIONED, True), (_POOLED, False), (_CENTRAL, True)],
                         ids=["Partitioned", "PooledMarket", "central"])
def test_accounting_over_the_free_owner_index_equals_the_per_share_rescan(selection, priced, monkeypatch):
    """On the pooled tick every owner truck is committed (no reserve at all); elsewhere some are free.

    Verified to have teeth: inverting the committed filter in
    ``accounting._free_trucks_by_haulier`` (keeping only committed trucks) failed the
    Partitioned and central cases (the pooled tick has no free owner truck, so it pins
    only the all-committed shape)."""
    captured = {}
    real = accounting.account_shares

    def spy(pairs, *, candidates, cost, own_allowed):
        candidates = list(candidates)
        captured.update(pairs=list(pairs), candidates=candidates, cost=cost, own_allowed=own_allowed)
        return real(pairs, candidates=candidates, cost=cost, own_allowed=own_allowed)

    monkeypatch.setattr(accounting, "account_shares", spy)
    app = _tick_a_app(_selection_profile(selection))
    random.seed(3)
    app.assign(_CLOCK, 5)
    got = real(captured["pairs"], candidates=captured["candidates"], cost=captured["cost"],
               own_allowed=captured["own_allowed"])
    want = _reference_accounts(captured["pairs"], captured["candidates"], captured["cost"], captured["own_allowed"])
    assert got == want
    assert len(got) > 10
    assert any(a.owner_reserve_km is not None for a in got) is priced


# --- #4: a stale reused top -------------------------------------------------------------


def test_a_patched_market_profile_runs_the_patched_market():
    """The instance that RUNS follows a profile patch: backstop, offer component, awards.

    Verified to have teeth: reusing the top instance unconditionally in
    ``bind_instance`` (the pre-fix behaviour) kept ``max_rounds_backstop`` at 3, kept the
    ``OfferAll`` component and kept awarding cross-company shares; not carrying the
    rebate book in ``profile._rebuilt_around_leaf`` and not swapping ``_solver`` in
    ``AssignmentApp._dispatch_runner`` each failed this test too.
    """
    profile = _selection_profile({"type": "PooledMarket", "params": {
        "max_rounds": 3, "offer": {"type": "OfferAll"}, "inner": {"type": "GreedyNearest"}}})
    app = _tick_a_app(profile)
    random.seed(1)
    app.assign(_CLOCK, 1)
    assert app.round_stats()["max_rounds_backstop"] == 3
    assert app._share_tags, "vacuous: OfferAll must award cross-company shares on the chain tick"
    old_top, old_leaf = app._solver, innermost_policy(app._solver)
    book = object()
    old_top._rebate_book = book  # what set_rebate_book records on the top

    profile["policies"]["planner"]["params"]["max_rounds"] = 9
    profile["policies"]["planner"]["params"]["offer"] = {"type": "OfferNone"}
    random.seed(1)
    pairs = app.assign(_CLOCK, 2)
    runner = app._dispatch_runner(profile)
    assert runner.policy is app._solver and app._solver is not old_top, "the stale top must be replaced"
    assert innermost_policy(app._solver) is old_leaf, "the injected leaf is kept"
    assert app._solver.rebate_book is book, "injections are carried onto the rebuilt top"
    assert runner.policy.params["max_rounds"] == 9
    assert type(runner.policy.components()[0]).__name__ == "OfferNonePolicy"
    assert app.round_stats()["max_rounds_backstop"] == 9, "the market ran with the stale backstop"
    assert pairs and app._share_tags == {}, "OfferNone pools nothing: no cross-company award"
    assert app._dispatch_runner(profile) is runner, "a rebuilt runner must be cached on the new top"


def test_a_patched_inner_selection_decides_like_a_fresh_app():
    """``Partitioned`` reads its groups' params and candidate kind from ``inner_node``.

    Verified to have teeth: reusing the top instance unconditionally in
    ``bind_instance`` made the patched app decide like the unpatched one (spatial).
    """
    spatial = _selection_profile(_PARTITIONED, max_trucks_per_haulier=5)
    sampled = copy.deepcopy(spatial)
    sampled["policies"]["planner"]["params"]["inner"]["params"] = {"candidates": "sampled"}

    def decide(profile, app=None):
        app = app or _tick_a_app(profile)
        random.seed(7)
        return _ids(app.assign(_CLOCK, 3)), app

    before, app = decide(spatial)
    fresh, _ = decide(sampled)
    assert before != fresh, "vacuous: the two candidate kinds must decide differently"
    app.behavior["profile"]["policies"] = copy.deepcopy(sampled["policies"])
    patched, _ = decide(app.behavior["profile"], app)
    assert patched == fresh
    assert app._dispatch_runner(app.behavior["profile"]).policy.inner_node.candidate_kind == "sampled"


def test_a_current_top_is_still_reused_as_is():
    """No patch, no rebuild: the injected top stays ``_solver`` and runs."""
    profile = _selection_profile(_POOLED)
    app = _tick_a_app(profile)
    top = app._solver
    app.assign(_CLOCK, 1)
    assert app._solver is top and app._dispatch_runner(profile).policy is top


# --- #5: solve_in_thread ---------------------------------------------------------------


class _CountingTpool:
    calls = 0

    @classmethod
    def execute(cls, fn, *args):
        cls.calls += 1
        return fn(*args)


@pytest.mark.parametrize("selection,calls", [(_CENTRAL, 1), (_PARTITIONED, 2), (_POOLED, 11)],
                         ids=["central", "Partitioned", "PooledMarket"])
def test_solve_in_thread_reaches_every_top_level_solver_once_per_decision(selection, calls, monkeypatch):
    """A central leaf: one call; Partitioned: one per group (2 on the chain tick), never
    wrapped again; PooledMarket: one per company solve (11 on the chain tick; final
    review MAJOR-5, was 0 before), never wrapped again. The pairs do not change.

    Verified to have teeth: calling ``self.policy.decide(ctx)`` directly for a leaf in
    ``DispatchRunner._decide`` failed the central case (0 calls); routing composites
    through ``ctx.execute`` too failed the Partitioned (3) and PooledMarket (12) cases;
    the pre-MAJOR-5 direct company solve failed the PooledMarket case (0).
    """
    def run(threaded):
        _CountingTpool.calls = 0
        app = _tick_a_app(_selection_profile(selection, solve_in_thread=threaded))
        random.seed(2)
        return _ids(app.assign(_CLOCK, 6)), _CountingTpool.calls

    monkeypatch.setattr(app_module, "_eventlet_tpool", _CountingTpool)
    threaded_pairs, threaded_calls = run(True)
    plain_pairs, plain_calls = run(False)
    assert (threaded_calls, plain_calls) == (calls, 0)
    assert threaded_pairs == plain_pairs and plain_pairs


# --- #6: the haulier-scoping ERROR on a violation tick -------------------------------------


@pytest.fixture
def rule_blind():
    with isolated_registry():
        @policy(slot="planner", name="RuleBlind", version="1", information_model="central",
                description="pairs the first truck with the first order, ignoring the rule")
        class RuleBlind(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                return Plan(chains={str(ctx.raw_trucks[0]["_id"]): (str(ctx.raw_orders[0]["_id"]),)})

        yield RuleBlind


def test_a_violation_tick_does_not_spend_the_haulier_scoping_warning(rule_blind, caplog):
    """Verified to have teeth: dropping the ``_tick_contract_events`` condition in
    ``AssignmentApp.assign`` logged the haulier ERROR on the violation tick and then
    swallowed the real block on the next tick."""
    trucks = [_truck("t_cargo", "cargo", 103.85, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    profile = _base_profile(policies={"planner": {"type": "RuleBlind"}})
    app = _plain_app(trucks, orders, profile)
    with caplog.at_level(logging.ERROR):
        assert app.assign("Wed, 01 Jan 2020 08:00:00 GMT", 1) == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("CONTRACT VIOLATION" in m for m in messages)
    assert not any("haulier scoping blocked everything" in m for m in messages), messages
    assert app._warned_haulier_block is False

    caplog.clear()
    app.behavior["profile"]["policies"] = {"planner": _PARTITIONED}
    app._solver = build_policy_tree(resolve_dispatch_for_profile(app.behavior["profile"]))
    with caplog.at_level(logging.ERROR):
        assert app.assign("Wed, 01 Jan 2020 08:00:00 GMT", 2) == []
    assert any("haulier scoping blocked everything" in r.getMessage() for r in caplog.records)
    assert app._warned_haulier_block is True


# --- #3: the tick-scope sampling cap, as documented ------------------------------------------


def test_tick_sampling_cap_is_per_company_unlike_partitioned_under_cooperation(monkeypatch):
    """``context.py``'s numbers: on the golden chain tick at cap 40 a central leaf samples
    4 x 40 = 160 trucks, Partitioned 40 + 40 = 80."""
    sampled = []
    real = DispatchContext.sampled_trucks

    def spy(self):
        out = real(self)
        if not any(ctx is self for ctx, _n in sampled):
            sampled.append((self, len(out)))
        return out

    monkeypatch.setattr(DispatchContext, "sampled_trucks", spy)
    leaf = {"type": "GreedyNearest", "params": {"candidates": "sampled"}}
    for selection, want in ((leaf, [160]), ({"type": "Partitioned", "params": {"inner": leaf}}, [40, 40])):
        sampled.clear()
        random.seed(4)
        _tick_a_app(_selection_profile(selection, max_trucks_per_haulier=40)).assign(_CLOCK, 1)
        assert sorted(n for _c, n in sampled) == want, selection
