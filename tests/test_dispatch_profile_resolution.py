"""Assignment profile -> dispatch policy resolution (plan §4 P1.6, P1.10).

What is locked:
- the legacy keys (``strategy``, ``solver_params``, ``use_spatial_matching``) translate
  to a validated ``policies.dispatch`` entry, keeping only declared params;
- an injected solver the registry does not know gets the legacy candidate rule
  (spatial iff ``use_spatial_matching`` and it has ``solve_pairs``) and runs through
  ``PairSolver``'s default ``decide``;
- every wiring error fails loud: a registered solver of the wrong class, an
  unregistered subclass of a registered policy, ``use_spatial_matching: false`` next
  to a ``policies`` block, bad ``policies.dispatch.params``, a non-executable timing
  at run start;
- the harness profile of ``tests/test_container_logistics_hauliers.py`` (no
  ``strategy``) resolves to the slot default, RandomAssignment;
- ``AssignmentApp._dispatch_runner`` caches on its inputs and rebuilds when any of
  them changes.

Why: this is the one place a profile becomes the policy that decides every tick.
A silent fallback here (wrong class, dropped params, wrong candidate kind) would
change results without any error, and runs would stop being comparable.
"""

from __future__ import annotations

import logging

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment.solver import (
    GreedyNearestSolver,
    RandomAssignmentSolver,
    get_solver,
)
from apps.container_logistics.policies.dispatch.allowed import PartitionRule
from apps.container_logistics.policies.dispatch.context import DispatchClock
from apps.container_logistics.policies.dispatch.helpers.candidates import CandidateSettings
from apps.container_logistics.policies.dispatch.pair_solver import PairSolver
from apps.container_logistics.policies.dispatch.profile import (
    assert_assignment_profiles_resolvable,
    legacy_dispatch_from_profile,
    resolve_dispatch_for_profile,
    resolve_for_instance,
)
from apps.container_logistics.policies.dispatch.runner import DispatchRunner, build_runner_for_app
from apps.container_logistics.policies.framework import (
    PolicyError,
    PolicyParamsError,
    PolicyTimingError,
    UnknownPolicyError,
)
from tests.test_assignment_cooperation import _app, _order, _truck

_CLOCK = DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=42)
_LEGACY_DC = {"dual_cycle_bonus_km": 2.5, "dual_cycle_radius_km": 1.0}


def _policies(type_, params=None, timing="online"):
    return {"policies": {"dispatch": {"type": type_, "timing": timing, "params": params or {}}}}


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


def _partition_ctx(runner, trucks, orders):
    return runner.partition_context(
        trucks=trucks, orders=orders, partition_key=("haulier", "acme"),
        rule=PartitionRule(max_travel_time_pickup=None, adjacency={}),
        settings=CandidateSettings.from_profile({}),
        accounting_cost=assign_constraints.assignment_cost, clock=_CLOCK,
    )


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

    resolved = resolve_dispatch_for_profile(prof)
    assert (resolved.name, resolved.timing, resolved.source) == ("GreedyNearest", "online", "legacy-profile")
    assert resolved.candidate_kind == "sampled", "use_spatial_matching: false must give the sampled kind"
    assert resolved.params.to_dict() == {"candidates": "sampled", **_LEGACY_DC}, (
        "the legacy solver_params must reach the validated GreedyNearest params unchanged"
    )

    for spatial_on in ({"use_spatial_matching": True}, {}):
        r = resolve_dispatch_for_profile({"strategy": "GreedyNearest", **spatial_on})
        assert r.candidate_kind == "spatial", (
            f"GreedyNearest with {spatial_on or 'no use_spatial_matching'} must keep its "
            f"default spatial candidates, got {r.candidate_kind!r}"
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

    resolved = resolve_dispatch_for_profile(prof)
    assert resolved.params.to_dict() == {"candidates": "sampled"}
    # use_spatial_matching never turns RandomAssignment spatial: its default is sampled.
    for flag in (True, False):
        r = resolve_dispatch_for_profile({**prof, "use_spatial_matching": flag})
        assert r.candidate_kind == "sampled", (
            f"RandomAssignment with use_spatial_matching={flag} must stay sampled (golden baseline)"
        )


def test_unregistered_pair_solver_uses_the_legacy_kind_rule_and_default_decide():
    """Verified to have teeth: replacing ``hasattr(policy, 'solve_pairs')`` with
    ``True`` in ``resolve_for_instance`` failed this test."""
    plain, paired = _FirstFree(), _FirstFreeWithPairs()
    cases = [
        (plain, {}, "sampled"),
        (plain, {"use_spatial_matching": True}, "sampled"),
        (paired, {}, "spatial"),
        (paired, {"use_spatial_matching": False}, "sampled"),
    ]
    for policy, prof, kind in cases:
        r = resolve_for_instance(policy, prof)
        assert r.candidate_kind == kind, (
            f"{type(policy).__name__} under {prof}: legacy rule says {kind!r}, got {r.candidate_kind!r}"
        )
        assert (r.source, r.params, r.timing, r.name) == (
            "unregistered", None, "online", type(policy).__name__
        )

    # Default PairSolver.decide on the sampled kind -> solve(sampled trucks, orders, allowed_raw).
    trucks, orders = _acme_world()
    runner = DispatchRunner(policy=plain, resolved=resolve_for_instance(plain, {}))
    ctx = _partition_ctx(runner, trucks, orders)
    result = runner.run(ctx)
    assert [(t["_id"], o["_id"]) for t, o in result] == [("t1", "o1"), ("t2", "o2")]
    (call,) = plain.calls
    assert call[0] == "solve" and call[1] == ["t1", "t2"] and call[2] == ["o1", "o2"]
    assert call[3] == ctx.allowed_raw, "solve must receive the framework's feasibility"

    # ... and on the spatial kind -> solve_pairs(spatial candidates), never solve.
    runner = DispatchRunner(policy=paired, resolved=resolve_for_instance(paired, {}))
    ctx = _partition_ctx(runner, trucks, orders)
    expected_pairs = [(t["_id"], o["_id"]) for t, o in ctx.candidate_pairs("spatial")]
    result = runner.run(ctx)
    assert [c[0] for c in paired.calls] == ["solve_pairs"], f"spatial kind must call only solve_pairs: {paired.calls}"
    assert paired.calls[0][1] == expected_pairs, "solve_pairs must get exactly the spatial candidates"
    assert result and runner.stats["violations"] == 0


def test_injected_solver_class_mismatch_raises():
    """Verified to have teeth: removing the name-mismatch ``raise`` in
    ``resolve_for_instance`` failed this test."""
    with pytest.raises(PolicyError) as exc:
        resolve_for_instance(GreedyNearestSolver(), {"strategy": "RandomAssignment"})
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
    assert resolve_dispatch_for_profile(profile).name == "RandomAssignment"

    trucks = [_truck("ta", "a", 103.85, 1.30), _truck("tb", "b", 103.86, 1.30)]
    orders = [_order("oa", "a", 103.85, 1.30), _order("ob", "b", 103.86, 1.30)]
    app = _app(trucks, orders, strategy="RandomAssignment")
    app.behavior = {"profile": dict(profile)}
    app._solver = RandomAssignmentSolver()
    result = app.assign("Wed, 01 Jan 2020 08:00:00 GMT")
    assert app._dispatch_runner(app.behavior["profile"]).resolved.name == "RandomAssignment"
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
    assert (ok.source, ok.candidate_kind) == ("policies", "sampled")


def test_unregistered_subclass_of_a_registered_policy_is_refused():
    """Verified to have teeth: skipping ``_refuse_unregistered_subclass`` in
    ``resolve_for_instance`` failed this test."""

    class _TweakedGreedy(GreedyNearestSolver):
        pass

    with pytest.raises(PolicyError) as exc:
        resolve_for_instance(_TweakedGreedy(), {"strategy": "GreedyNearest"})
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
        resolve_dispatch_for_profile({"policies": {"dispatch": {"type": "GreedyNearest", "params": [1]}}})
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
    """Verified to have teeth: dropping ``id(self._solver)`` from the cache key, and
    dropping ``solver_params``, each failed this test."""
    app = _app([], [], strategy="GreedyNearest")
    prof = app.behavior["profile"]
    first = app._dispatch_runner(prof)
    assert app._dispatch_runner(prof) is first, "same inputs must return the cached runner"
    assert first.policy is app._solver

    app._solver = get_solver("GreedyNearest", None)
    swapped = app._dispatch_runner(prof)
    assert swapped is not first and swapped.policy is app._solver, (
        "a replaced _solver must get a new runner holding the new solver"
    )

    prof["solver_params"] = {"dual_cycle_bonus_km": 1.0}
    reparam = app._dispatch_runner(prof)
    assert reparam is not swapped and reparam.resolved.params.dual_cycle_bonus_km == 1.0, (
        "changed solver_params must rebuild the runner with the new params"
    )

    prof["strategy"] = "RandomAssignment"
    with pytest.raises(PolicyError):
        app._dispatch_runner(prof)  # strategy changed but the solver did not: mismatch
    app._solver = RandomAssignmentSolver()
    restrat = app._dispatch_runner(prof)
    assert restrat.resolved.name == "RandomAssignment" and restrat.policy is app._solver

    prof["use_spatial_matching"] = False
    assert app._dispatch_runner(prof) is not restrat, "use_spatial_matching is part of the key"


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

    prof = {"policies": {"dispatch": {"type": "GreedyNearest", "timing": timing, "params": {}}}}
    with pytest.raises(PolicyTimingError, match="is not a dispatch timing. Allowed: online, plan"):
        resolve_dispatch_for_profile(prof)
