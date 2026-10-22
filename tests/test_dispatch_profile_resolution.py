"""Assignment profile -> dispatch policy resolution (plan §4 P1.6, P1.10).

What is locked:
- the legacy keys (``strategy``, ``solver_params``, ``use_spatial_matching``) translate
  to a validated ``policies.planner`` entry, keeping only declared params;
- an injected solver the registry does not know gets the legacy candidate rule
  (spatial iff ``use_spatial_matching`` and it has ``solve_pairs``,
  ``compat.unregistered``) and runs through ``PairSolver``'s default decision
  (``compat.unregistered.UnregisteredPairLeaf``); the candidate kind a caller reads is
  the bound leaf's ``Declaration.candidate_hint``;
- every wiring error fails loud: a registered solver of the wrong class, an
  unregistered subclass of a registered policy, ``use_spatial_matching: false`` next
  to a ``policies`` block, bad ``policies.planner.params``, a non-executable timing
  at run start;
- the harness profile of ``tests/test_container_logistics_hauliers.py`` (no
  ``strategy``) resolves to the slot default, RandomAssignment;
- ``AssignmentApp._dispatch_runner`` caches on its inputs (including the world keys
  the bound tree's binds read) and rebuilds when any of them changes;
- ``build_policy_tree`` refuses, with ``PolicyError``, a class with an ``inner`` ref
  that rejects the construction keywords;
- round 2 (C2): ``bind_instance`` fits a built policy into the selection tree (leaf at
  the innermost, composite at the top, an unregistered leaf under the legacy kind
  rule) or names both trees; ``mirror_dispatch_selection`` writes the legacy mirrors
  and ``check_mirrors`` refuses a profile whose mirrors drifted (message 13).

Why: this is the one place a profile becomes the policy that decides every tick.
A silent fallback here (wrong class, dropped params, wrong candidate kind) would
change results without any error, and runs would stop being comparable.
"""

from __future__ import annotations

import logging
import random

import pytest

from dataclasses import dataclass

from apps.container_logistics.assignment.solver import (
    GreedyNearestSolver,
    RandomAssignmentSolver,
    get_solver,
)
from apps.container_logistics.policies.compat.unregistered import UnregisteredPairLeaf
from apps.container_logistics.policies.dispatch.bind import bind_profile, bind_tree
from apps.container_logistics.policies.dispatch.composite import CompositePolicy
from apps.container_logistics.policies.dispatch.tick import DispatchClock
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.solver_lib import candidates as C
from apps.container_logistics.policies.solver_lib.pair_solver import PairSolver
from apps.container_logistics.policies.dispatch.profile import (
    assert_assignment_profiles_resolvable,
    bind_instance,
    build_policy_tree,
    check_mirrors,
    mirror_dispatch_selection,
    legacy_dispatch_from_profile,
    resolve_dispatch_for_profile,
    bind_profile_instance,
)
from apps.container_logistics.policies.dispatch.runner import build_runner_for_app
from apps.container_logistics.policies.framework import (
    PolicyError,
    PolicyParamsError,
    PolicyTimingError,
    UnknownPolicyError,
)
from tests.policy_tree_standins import decompositions
from tests.test_assignment_cooperation import _app, _order, _truck

_CLOCK = DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=42)
_LEGACY_DC = {"dual_cycle_bonus_km": 2.5, "dual_cycle_radius_km": 1.0}


def _policies(type_, params=None, timing="online"):
    # policies_format 2: the compile-time marker every round-2 bundle carries (C4).
    return {"policies": {"planner": {"type": type_, "timing": timing, "params": params or {}}},
            "policies_format": 2}


class _FirstFree(PairSolver):
    """Unregistered pair solver with ``solve`` only: each order takes the first free allowed truck."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def solve(self, trucks, orders, *, pair_allowed, cost):
        self.calls.append(("solve", [t["_id"] for t in trucks], [o["_id"] for o in orders], pair_allowed))
        used, out = set(), []
        for o in orders:
            for t in trucks:
                if t["_id"] not in used and pair_allowed(t, o):
                    used.add(t["_id"])
                    out.append((t, o))
                    break
        return out


class _FirstFreeWithPairs(_FirstFree):
    """Same, plus ``solve_pairs``: takes candidate pairs in order, first-free wins."""

    def solve_pairs(self, candidate_pairs, *, cost):
        pairs = list(candidate_pairs)
        self.calls.append(("solve_pairs", [(t["_id"], o["_id"]) for t, o in pairs]))
        used_t, used_o, out = set(), set(), []
        for t, o in pairs:
            if t["_id"] not in used_t and o["_id"] not in used_o:
                used_t.add(t["_id"])
                used_o.add(o["_id"])
                out.append((t, o))
        return out


def _acme_world():
    trucks = [_truck("t1", "acme", 103.85, 1.30), _truck("t2", "acme", 103.95, 1.30)]
    orders = [_order("o1", "acme", 103.95, 1.30), _order("o2", "acme", 103.85, 1.30)]
    return trucks, orders


def _leaf_node(policy, prof):
    """The innermost node ``policy`` is bound as under ``prof`` (round 2 C3: the selection
    is a tree, so the round-1 ``resolve_for_instance`` became ``bind_profile_instance``)."""
    return bind_profile_instance(prof, policy)[1].innermost


def _kind(prof, instance=None):
    """The leaf's candidate kind as a caller reads it: the bound leaf's hint, else ``sampled``."""
    return bind_profile(prof, instance).leaf.declaration.candidate_hint or "sampled"


def _innermost(policy):
    """Follow ``inner`` down a built tree (the construction protocol)."""
    while getattr(policy, "inner", None) is not None:
        policy = policy.inner
    return policy


def _whole_tick(runner, trucks, orders, prof):
    return runner.tick(candidates=trucks, orders=orders, profile=prof, clock=_CLOCK, seed=1)


def test_legacy_profile_translates_strategy_params_and_use_spatial():
    """Verified to have teeth: dropping the ``use_spatial_matching: false`` ->
    ``candidates='sampled'`` branch, and passing ``solver_params`` through unfiltered
    (``params = dict(raw)``), each failed this test."""
    # An undeclared key in solver_params (legacy keys were never validated) is dropped.
    prof = {"strategy": "GreedyNearest", "solver_params": {**_LEGACY_DC, "legacy_unused_knob": 1},
            "use_spatial_matching": False}
    assert legacy_dispatch_from_profile(prof) == {
        "type": "GreedyNearest",
        "timing": "online",
        "params": {**_LEGACY_DC, "candidates": "sampled"},
    }, "legacy keys must translate to exactly the declared params plus candidates='sampled'"

    # Round 2: a legacy profile resolves to the tree Partitioned/<strategy>; these
    # assertions are about the per-scope solver, its innermost node.
    tree = resolve_dispatch_for_profile(prof)
    assert tree.label == "Partitioned/GreedyNearest"
    resolved = tree.innermost
    assert (resolved.name, resolved.timing, resolved.source) == ("GreedyNearest", "online", "legacy-profile")
    assert resolved.params.candidates == "sampled" and _kind(prof) == "sampled", (
        "use_spatial_matching: false must give the sampled kind")
    assert resolved.params.to_dict() == {"candidates": "sampled", **_LEGACY_DC}, (
        "the legacy solver_params must reach the validated GreedyNearest params unchanged"
    )

    for spatial_on in ({"use_spatial_matching": True}, {}):
        r = resolve_dispatch_for_profile({"strategy": "GreedyNearest", **spatial_on}).innermost
        kind = _kind({"strategy": "GreedyNearest", **spatial_on})
        assert r.params.candidates == "spatial" and kind == "spatial", (
            f"GreedyNearest with {spatial_on or 'no use_spatial_matching'} must keep its "
            f"default spatial candidates, got {kind!r}"
        )
        assert r.params.dual_cycle_bonus_km == 5.0 and r.params.dual_cycle_radius_km == 0.5

    # Legacy keys hold unvalidated data, but the params that ARE declared get validated.
    with pytest.raises(PolicyParamsError, match="dual_cycle_bonus_km"):
        resolve_dispatch_for_profile({"strategy": "GreedyNearest", "solver_params": {"dual_cycle_bonus_km": "5"}})
    with pytest.raises(UnknownPolicyError, match="Nope"):
        resolve_dispatch_for_profile({"strategy": "Nope"})


def test_random_drops_undeclared_legacy_dual_cycle_keys(caplog):
    """Verified to have teeth: passing ``solver_params`` through unfiltered made
    RandomAssignment's strict params refuse the dual-cycle keys and failed this test.
    The drop goes through the framework's ONE rule (review F4), logged at WARNING."""
    prof = {"strategy": "RandomAssignment", "solver_params": dict(_LEGACY_DC)}
    with caplog.at_level(logging.WARNING, logger="apps.container_logistics.policies.framework.resolve"):
        entry = legacy_dispatch_from_profile(prof)
    assert entry["params"] == {}, (
        f"RandomAssignment declares no dual-cycle params, so they must be dropped; got {entry['params']}"
    )
    assert any("dual_cycle_bonus_km" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records), "dropping legacy keys must be logged at WARNING"

    resolved = resolve_dispatch_for_profile(prof).innermost  # round 2: Partitioned/RandomAssignment
    assert resolved.params.to_dict() == {"candidates": "sampled"}
    # use_spatial_matching never turns RandomAssignment spatial: its default is sampled.
    for flag in (True, False):
        assert _kind({**prof, "use_spatial_matching": flag}) == "sampled", (
            f"RandomAssignment with use_spatial_matching={flag} must stay sampled (golden baseline)"
        )


def test_unregistered_pair_solver_uses_the_legacy_kind_rule_and_default_decide():
    """Verified to have teeth: replacing ``hasattr(instance, "solve_pairs")`` with
    ``True`` in ``compat.unregistered.legacy_spatial_rule`` failed this test."""
    plain, paired = _FirstFree(), _FirstFreeWithPairs()
    cases = [
        (plain, {}, "sampled"),
        (plain, {"use_spatial_matching": True}, "sampled"),
        (paired, {}, "spatial"),
        (paired, {"use_spatial_matching": False}, "sampled"),
    ]
    for policy, prof, kind in cases:
        r = _leaf_node(policy, prof)
        leaf = bind_profile(prof, policy).leaf
        assert isinstance(leaf.solver, UnregisteredPairLeaf) and leaf.instance is policy
        got = leaf.declaration.candidate_hint or "sampled"
        assert got == kind, f"{type(policy).__name__} under {prof}: legacy rule says {kind!r}, got {got!r}"
        assert (r.source, r.params, r.timing, r.name) == (
            "unregistered", None, "online", type(policy).__name__
        )

    # Default PairSolver decision on the sampled kind -> solve(sampled trucks, orders, permitted),
    # here as the inner of the profile's Partitioned (one group: the acme haulier).
    trucks, orders = _acme_world()
    runner = build_runner_for_app(plain, {})
    tick = _whole_tick(runner, trucks, orders, {})
    result = runner.run(tick).pairs
    assert [(t["_id"], o["_id"]) for t, o in result] == [("t1", "o1"), ("t2", "o2")]
    (call,) = plain.calls
    assert call[0] == "solve" and call[1] == ["t1", "t2"] and call[2] == ["o1", "o2"]
    assert call[3] is tick.permitted, "solve must receive the framework's feasibility"

    # ... and on the spatial kind -> solve_pairs(spatial candidates), never solve.
    runner = build_runner_for_app(paired, {})
    tick = _whole_tick(runner, trucks, orders, {})
    expected_pairs = [(t["_id"], o["_id"]) for t, o in C.spatial_pairs(tick, C.CandidateSettings.from_world({}))]
    result = runner.run(tick).pairs
    assert [c[0] for c in paired.calls] == ["solve_pairs"], f"spatial kind must call only solve_pairs: {paired.calls}"
    assert paired.calls[0][1] == expected_pairs, "solve_pairs must get exactly the spatial candidates"
    assert result and runner.stats["violations"] == 0


def test_injected_solver_class_mismatch_raises():
    """Verified to have teeth: removing the name-mismatch ``raise`` in
    ``resolve_for_instance`` failed this test."""
    with pytest.raises(PolicyError) as exc:
        bind_profile_instance({"strategy": "RandomAssignment"}, GreedyNearestSolver())
    msg = str(exc.value)
    assert "GreedyNearest" in msg and "RandomAssignment" in msg, f"message must name both policies: {msg}"

    with pytest.raises(PolicyError, match="GreedyNearest.*RandomAssignment"):
        build_runner_for_app(GreedyNearestSolver(), {})  # no strategy -> slot default RandomAssignment

    app = _app([], [], strategy="GreedyNearest")
    app._solver = RandomAssignmentSolver()
    with pytest.raises(PolicyError, match="RandomAssignment.*GreedyNearest"):
        app.assign("Wed, 01 Jan 2020 08:00:00 GMT")


def test_hauliers_harness_profile_without_strategy_resolves_to_random():
    """Mirrors ``tests/test_container_logistics_hauliers.py::_make_app``.

    Verified to have teeth: making ``legacy_dispatch_from_profile`` default to
    ``"GreedyNearest"`` instead of the slot default failed this test (the injected
    RandomAssignment then mismatched)."""
    profile = {"respect_truck_online_state": True, "reject_if_active_haul_trip": True}
    assert resolve_dispatch_for_profile(profile).label == "Partitioned/RandomAssignment"

    trucks = [_truck("ta", "a", 103.85, 1.30), _truck("tb", "b", 103.86, 1.30)]
    orders = [_order("oa", "a", 103.85, 1.30), _order("ob", "b", 103.86, 1.30)]
    app = _app(trucks, orders, strategy="RandomAssignment")
    app.behavior = {"profile": dict(profile)}
    app._solver = RandomAssignmentSolver()
    result = app.assign("Wed, 01 Jan 2020 08:00:00 GMT")
    assert app._dispatch_runner(app.behavior["profile"]).resolved.label == "Partitioned/RandomAssignment"
    assert sorted((t["_id"], o["_id"]) for t, o in result) == [("ta", "oa"), ("tb", "ob")], (
        f"each haulier's only truck must take its own order: {result}"
    )


def test_policies_block_with_use_spatial_false_is_refused():
    """Verified to have teeth: removing the ``use_spatial_matching`` check in
    ``_resolve_policies_block`` failed this test."""
    prof = {**_policies("GreedyNearest"), "use_spatial_matching": False}
    with pytest.raises(PolicyError, match="use_spatial_matching"):
        resolve_dispatch_for_profile(prof)
    # Explicit True is a no-op, and the block's own candidates param is what counts.
    ok = resolve_dispatch_for_profile({**_policies("GreedyNearest", {"candidates": "sampled"}),
                                       "use_spatial_matching": True})
    assert (ok.source, ok.params.candidates) == ("policies", "sampled")
    assert _kind({**_policies("GreedyNearest", {"candidates": "sampled"}), "use_spatial_matching": True}) == "sampled"


def test_unregistered_subclass_of_a_registered_policy_is_refused():
    """Verified to have teeth: skipping ``_refuse_unregistered_subclass`` in
    ``resolve_for_instance`` failed this test."""

    class _TweakedGreedy(GreedyNearestSolver):
        pass

    with pytest.raises(PolicyError) as exc:
        bind_profile_instance({"strategy": "GreedyNearest"}, _TweakedGreedy())
    msg = str(exc.value)
    assert "_TweakedGreedy" in msg and "GreedyNearest" in msg and "not registered" in msg, msg


def test_policies_block_params_are_validated():
    """Verified to have teeth: resolving the block with
    ``from_mapping(None, ...)`` (ignoring the given params) failed this test."""
    bad = [
        ({"bogus": 1}, "bogus"),
        ({"dual_cycle_bonus_km": "5"}, "dual_cycle_bonus_km"),
        ({"dual_cycle_bonus_km": True}, "dual_cycle_bonus_km"),
        ({"dual_cycle_bonus_km": -1.0}, "dual_cycle_bonus_km"),
        ({"dual_cycle_radius_km": float("nan")}, "dual_cycle_radius_km"),
        ({"candidates": "full"}, "candidates"),
    ]
    for params, key in bad:
        with pytest.raises(PolicyParamsError, match=key):
            resolve_dispatch_for_profile(_policies("GreedyNearest", params))
    with pytest.raises(PolicyParamsError):
        resolve_dispatch_for_profile({"policies": {"planner": {"type": "GreedyNearest", "params": [1]}}})
    with pytest.raises(PolicyParamsError, match="dual_cycle_bonus_km"):
        resolve_dispatch_for_profile(_policies("RandomAssignment", {"dual_cycle_bonus_km": 5.0}))

    good = resolve_dispatch_for_profile(_policies("GreedyNearest", {"dual_cycle_bonus_km": 3}))
    assert good.params.dual_cycle_bonus_km == 3.0 and isinstance(good.params.dual_cycle_bonus_km, float)


def test_run_start_check_refuses_plan_timing():
    """Verified to have teeth: removing the not-executable branch of
    ``framework.resolve._resolve_timing`` (the one resolver the run-start check now
    shares, review F4) failed this test."""
    collection = {
        "assignment_ok": {"profile": {"strategy": "GreedyNearest"}},
        "assignment_planner": {"profile": _policies("GreedyNearest", timing="plan")},
    }
    with pytest.raises(PolicyTimingError) as exc:
        assert_assignment_profiles_resolvable(collection)
    msg = str(exc.value)
    assert "assignment_planner" in msg and "'plan'" in msg, f"must name the agent and the timing: {msg}"
    assert_assignment_profiles_resolvable({"assignment_ok": collection["assignment_ok"]})

    with pytest.raises(UnknownPolicyError, match="assignment_x"):
        assert_assignment_profiles_resolvable({"assignment_x": {"profile": {"strategy": "Nope"}}})


def test_dispatch_runner_cache_rebuilds_when_the_solver_is_swapped():
    """Verified to have teeth: dropping ``id(self._solver)`` from the cache key,
    dropping ``solver_params``, dropping ``planner`` (round 2 C3), and dropping
    ``runner.tree.digest(prof)`` (WS1: the bound world reads), each failed this test."""
    app = _app([], [], strategy="GreedyNearest")
    prof = app.behavior["profile"]
    first = app._dispatch_runner(prof)
    assert app._dispatch_runner(prof) is first, "same inputs must return the cached runner"
    # Round 2: the runner holds the tree; the app's leaf instance is its innermost solver.
    assert first.tree.leaf.instance is app._solver

    app._solver = get_solver("GreedyNearest", None)
    swapped = app._dispatch_runner(prof)
    assert swapped is not first and swapped.tree.leaf.instance is app._solver, (
        "a replaced _solver must get a new runner holding the new solver"
    )

    prof["solver_params"] = {"dual_cycle_bonus_km": 1.0}
    reparam = app._dispatch_runner(prof)
    assert reparam is not swapped and reparam.resolved.innermost.params.dual_cycle_bonus_km == 1.0, (
        "changed solver_params must rebuild the runner with the new params"
    )

    # Round 2 C3: the decomposition is read from planner (topology/market), so a
    # planner patch must rebuild the runner too.
    prof.setdefault("planner", {})["topology"] = "pooled"
    repooled = app._dispatch_runner(prof)
    assert repooled is not reparam and repooled.resolved.label == "PooledMarket/GreedyNearest", (
        "a planner.topology patch must rebuild the runner around the same solver"
    )
    prof["planner"]["market"] = {"max_rounds": 7}
    assert app._dispatch_runner(prof).resolved.params.max_rounds == 7, "planner.market is part of the key"
    prof["planner"]["topology"] = "partitioned"

    prof["strategy"] = "RandomAssignment"
    with pytest.raises(PolicyError):
        app._dispatch_runner(prof)  # strategy changed but the solver did not: mismatch
    app._solver = RandomAssignmentSolver()
    restrat = app._dispatch_runner(prof)
    assert restrat.resolved.leaf == "RandomAssignment" and restrat.tree.leaf.instance is app._solver

    prof["use_spatial_matching"] = False
    respatial = app._dispatch_runner(prof)
    assert respatial is not restrat, "use_spatial_matching is part of the key"

    # WS1: a world key the bound tree's binds read (the cooperation arm) is part of the key,
    # so the top's declared cross-company pairs follow a patched structure.
    assert "cooperation" in respatial.tree.world_reads
    prof["cooperation"] = {"active": "s", "structures": [{"id": "s", "adjacency": {"a": ["b"], "b": ["a"]}}]}
    recoop = app._dispatch_runner(prof)
    assert recoop is not respatial and recoop.tree.declaration.cross_company == {("a", "b"), ("b", "a")}
    assert app._dispatch_runner(prof) is recoop


@pytest.mark.parametrize("value", [0, None, "", "false", 1])
def test_non_boolean_use_spatial_matching_is_refused(value):
    """The pre-v2 code read the flag with ``bool(...)``; a non-boolean is now loud.

    Verified to have teeth: disabling the ``isinstance(value, bool)`` check in
    ``_use_spatial_matching`` let every case resolve (an arm was picked silently).
    """
    from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile
    from apps.container_logistics.policies.framework import PolicyError

    with pytest.raises(PolicyError, match="use_spatial_matching must be true or false"):
        resolve_dispatch_for_profile({"strategy": "GreedyNearest", "use_spatial_matching": value})


@pytest.mark.parametrize("timing", ["bogus", 7, ""])
def test_unknown_dispatch_timing_is_named_as_such(timing):
    """An undeclared timing is refused as "not a dispatch timing", not as "not executable".

    Verified to have teeth: removing the ``_checked_timing`` call let all three resolve.
    """
    from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile
    from apps.container_logistics.policies.framework import PolicyTimingError

    prof = {"policies": {"planner": {"type": "GreedyNearest", "timing": timing, "params": {}}}}
    with pytest.raises(PolicyTimingError, match="is not a planner timing. Allowed: online, plan"):
        resolve_dispatch_for_profile(prof)


# --- round 2 (C2): the selection tree, binding and mirrors ---------------------------
#
# ``tests/policy_tree_standins.decompositions()`` leaves the real ``Partitioned`` /
# ``PooledMarket`` (registered since C3) in place, so these run against them.

_GREEDY_PARTITIONED = {"strategy": "GreedyNearest", "solver_params": dict(_LEGACY_DC)}


def test_bind_leaf_instance_at_innermost():
    """A registered LEAF instance becomes the innermost leaf; the wrappers are built.

    Verified to have teeth: returning the instance unwrapped when it names the innermost
    leaf failed the composite assertions; not forwarding ``set_rng`` in
    ``CompositePolicy`` failed the injection assertion. (C3: ``Partitioned`` deliberately
    does NOT forward, see ``test_partitioned_does_not_forward_rng_injections``, so the
    forwarding is asserted on ``PooledMarket``, which inherits it.)
    """
    with decompositions():
        node = resolve_dispatch_for_profile(_GREEDY_PARTITIONED)
        leaf = GreedyNearestSolver({"dual_cycle_bonus_km": 2.5})
        policy, bound = bind_instance(node, leaf)
    assert node.label == "Partitioned/GreedyNearest"
    assert isinstance(policy, CompositePolicy) and policy.inner is leaf and _innermost(policy) is leaf
    assert bound is node and policy.inner_node is node.inner
    assert policy.params == {"inner": {"type": "GreedyNearest", "params": node.inner.params.to_dict()}}
    pooled_leaf = GreedyNearestSolver({"dual_cycle_bonus_km": 2.5})
    with decompositions():
        pooled, _bound = bind_instance(
            resolve_dispatch_for_profile({**_GREEDY_PARTITIONED, "planner": {"topology": "pooled"}}), pooled_leaf)
    rng = random.Random(3)
    pooled.set_rng(rng)
    pooled.set_tiebreak(17)
    assert pooled_leaf.rng is rng and pooled_leaf.tiebreak_seed == 17, "the composite must forward injections"


def test_partitioned_does_not_forward_rng_injections():
    """``Partitioned`` records ``set_rng``/``set_tiebreak`` but keeps its inner's own values:
    the pre-v2 partitioned path never injected one (byte identity, S12; C2 review NIT).

    Verified to have teeth: deleting ``Partitioned.set_rng`` (inheriting the forwarding)
    failed this test.
    """
    leaf = GreedyNearestSolver()
    with decompositions():
        policy, _bound = bind_instance(resolve_dispatch_for_profile(_GREEDY_PARTITIONED), leaf)
    rng = random.Random(3)
    policy.set_rng(rng)
    policy.set_tiebreak(17)
    assert policy.rng is rng and policy.tiebreak_seed == 17
    assert leaf.rng is random and leaf.tiebreak_seed is None, "Partitioned must not inject into its inner"
    policy.set_rebate_book(None)  # the rebate book is still forwarded (it is the solver's data)


def test_bind_composite_instance_at_top():
    """A built tree is reused as is when its names match the profile's tree.

    Verified to have teeth: rebuilding the tree in ``bind_instance`` (instead of reusing
    the top instance) failed the identity assertion.
    """
    with decompositions():
        node = resolve_dispatch_for_profile({**_GREEDY_PARTITIONED, "planner": {"topology": "pooled"}})
        built = build_policy_tree(node)
        policy, bound = bind_instance(node, built)
    assert policy is built and bound is node
    assert node.label == "PooledMarket/GreedyNearest" and type(built.inner) is GreedyNearestSolver


def test_bind_mismatch_names_the_tree():
    """Verified to have teeth: skipping the recursive inner-name check in
    ``_bound_reused_tree`` let the Partitioned/RandomAssignment tree bind."""
    with decompositions():
        node = resolve_dispatch_for_profile(_GREEDY_PARTITIONED)
        with pytest.raises(PolicyError) as exc:
            bind_instance(node, RandomAssignmentSolver())
        assert str(exc.value) == ("AssignmentApp._solver is a RandomAssignment but the profile "
                                  "resolves dispatch to Partitioned/GreedyNearest.")
        wrong_inner = build_policy_tree(resolve_dispatch_for_profile({}))
        with pytest.raises(PolicyError) as exc:
            bind_instance(node, wrong_inner)
        assert str(exc.value) == ("AssignmentApp._solver is a Partitioned/RandomAssignment but the "
                                  "profile resolves dispatch to Partitioned/GreedyNearest.")
        pooled = build_policy_tree(resolve_dispatch_for_profile({**_GREEDY_PARTITIONED,
                                                                  "planner": {"topology": "pooled"}}))
        with pytest.raises(PolicyError, match="is a PooledMarket/GreedyNearest but .* Partitioned/GreedyNearest"):
            bind_instance(node, pooled)


def test_unregistered_leaf_uses_legacy_kind_rule():
    """Verified to have teeth: replacing ``hasattr(instance, "solve_pairs")`` with ``True``
    in ``compat.unregistered.legacy_spatial_rule`` failed the plain-solver row."""

    def _hint(bound, policy, flag):
        return bind_tree(bound, policy, {"use_spatial_matching": flag}).leaf.declaration.candidate_hint or "sampled"

    with decompositions():
        node = resolve_dispatch_for_profile(_GREEDY_PARTITIONED)
        for instance, flag, kind in ((_FirstFreeWithPairs(), True, "spatial"),
                                     (_FirstFreeWithPairs(), False, "sampled"),
                                     (_FirstFree(), True, "sampled")):
            policy, bound = bind_instance(node, instance)
            assert _innermost(policy) is instance and isinstance(policy, CompositePolicy)
            assert (bound.inner.source, bound.inner.params) == ("unregistered", None), bound.inner
            assert _hint(bound, policy, flag) == kind
            assert bound.label == f"Partitioned/{type(instance).__name__}" and bound.leaf == type(instance).__name__
            assert node.inner.name == "GreedyNearest", "the profile's node must not be mutated"
            # Re-binding the built tree accepts its unregistered innermost leaf.
            again, rebound = bind_instance(node, policy)
            assert again is policy and _hint(rebound, again, flag) == kind

        class _TweakedGreedy(GreedyNearestSolver):
            pass

        with pytest.raises(PolicyError, match="_TweakedGreedy subclasses the registered planner policy"):
            bind_instance(node, _TweakedGreedy())


def test_mirrors_written_and_checked():
    """``mirror_dispatch_selection`` writes the legacy keys; ``check_mirrors`` refuses drift.

    Verified to have teeth: skipping the ``planner.topology`` comparison in
    ``check_mirrors`` let the round-1 flat bundle through; mirroring
    ``solver_params`` from the TOP node only (which declares no dual-cycle keys) left
    ``dual_cycle_bonus_km`` at 9.0.
    """
    with decompositions():
        node = resolve_dispatch_for_profile({"strategy": "GreedyNearest",
                                             "solver_params": {"dual_cycle_bonus_km": 2.0},
                                             "planner": {"topology": "pooled", "market": {"max_rounds": 6}}})
        profile = {"strategy": "RandomAssignment",
                   "solver_params": {"dual_cycle_bonus_km": 9.0, "dual_cycle_radius_km": 9.0},
                   "planner": {"topology": "partitioned",
                               "deployment": {"type": "RandomAssignment", "params": {"dual_cycle_bonus_km": 9.0}}}}
        mirror_dispatch_selection(profile, node)
        assert profile["strategy"] == "GreedyNearest"
        assert profile["solver_params"] == {"dual_cycle_bonus_km": 2.0, "dual_cycle_radius_km": 0.5}
        assert profile["planner"]["deployment"] == {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 2.0}}
        assert profile["planner"]["topology"] == "pooled"
        assert profile["planner"]["market"] == {
            "offer": {"type": "OfferAll", "params": {}}, "claim": {"type": "ClaimAllPlanned", "params": {}},
            "arbitration": {"type": "LowestCost", "params": {}}, "max_rounds": 6}
        check_mirrors(profile, node)

        for key, value, match in (
            ("strategy", "RandomAssignment", "profile strategy='RandomAssignment' disagrees"),
            ("planner", {**profile["planner"], "market": {"max_rounds": 7}}, r"max_rounds=7 vs 6"),
            ("planner", {**profile["planner"], "topology": "nonsense"}, "is not a legacy topology"),
        ):
            with pytest.raises(PolicyError, match=match):
                check_mirrors({**profile, key: value}, node)
        with pytest.raises(PolicyError) as exc:
            check_mirrors({**profile, "planner": {**profile["planner"], "topology": "partitioned"}}, node)
        assert str(exc.value) == (
            "profile planner.topology='partitioned' disagrees with its compiled policies.planner "
            "(PooledMarket). The compiled policies block is authoritative; change a run's "
            "decomposition with --policy planner=Partitioned (ORSIM_PLANNER_TOPOLOGY is translated "
            "into exactly that). Fix the bundle or recompile.")

        # A central top has no legacy topology: the mirror removes the key ...
        central = resolve_dispatch_for_profile(_policies("GreedyNearest"))
        flat = {"strategy": "GreedyNearest", "planner": {"topology": "partitioned", "market": {"max_rounds": 3}},
                **_policies("GreedyNearest")}
        mirrored = {**flat, "planner": dict(flat["planner"])}
        mirror_dispatch_selection(mirrored, central)
        assert "topology" not in mirrored["planner"] and mirrored["planner"]["market"] == {"max_rounds": 3}
        check_mirrors(mirrored, central)
        # ... and a round-1 flat bundle (central leaf + topology) is refused at run start.
        with pytest.raises(PolicyError, match=r"^assignment agent 'a0': profile planner.topology='partitioned' "
                                              r"disagrees with its compiled policies.planner \(GreedyNearest\)"):
            assert_assignment_profiles_resolvable({"a0": {"profile": flat}})


def test_run_start_check_refuses_a_policies_block_without_the_format_marker():
    """``assert_assignment_profiles_resolvable`` itself refuses a round-1 flat bundle.

    Verified to have teeth: removing the ``check_policies_format`` call from it let the
    unmarked block resolve as a central GreedyNearest.
    """
    unmarked = {"policies": {"planner": {"type": "GreedyNearest", "timing": "online", "params": {}}}}
    with pytest.raises(PolicyError, match=r"assignment agent 'a0': profile.policies has policies_format=None"):
        assert_assignment_profiles_resolvable({"a0": {"profile": unmarked}})
    assert_assignment_profiles_resolvable({"a0": {"profile": {**unmarked, "policies_format": 2}}})


def test_build_policy_tree_refuses_a_class_that_rejects_the_construction_keywords():
    """A node with an ``inner`` ref is built as ``cls(params=, inner=, inner_node=)``; a class
    that cannot take them is a ``PolicyError`` naming the protocol, never a bare ``TypeError``.

    Verified to have teeth: removing the ``try/except TypeError`` in ``build_policy_tree``
    let the raw ``TypeError`` through and failed this test.
    """
    with isolated_registry():

        @policy(slot="planner", name="TestNoInnerKwargs", version="1", description="test only",
                information_model="per_group")
        class _NoInnerKwargs:
            @dataclass(frozen=True)
            class Params(PolicyParams):
                inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True, help="x")

            def __init__(self, params=None):
                self.params = params

            def solve_tick(self, tick, params):  # pragma: no cover - never reached
                raise AssertionError

        node = resolve_dispatch_for_profile(_policies("TestNoInnerKwargs"))
        with pytest.raises(PolicyError, match=r"does not accept the construction keywords"):
            build_policy_tree(node)
