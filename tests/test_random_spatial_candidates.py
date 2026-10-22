"""RandomAssignment under ``candidates="spatial"``: the new spatial arm (plan §4 P1.9).

What is locked: with ``params.candidates == "spatial"``, RandomAssignment matches
only among the spatial index's candidate pairs (never a pair outside them), never
gives a truck or an order twice, and draws only from the RNG it was handed, so a
seeded RNG reproduces the match exactly. End to end, an ``AssignmentApp`` whose
profile selects ``policies.planner: {type: RandomAssignment, params: {candidates:
spatial}}`` commits only pairs that ``spatial.iter_candidate_pairs`` emitted.

Why: ``RandomAssignmentSolver.solve_pairs`` is NEW behaviour (the default stays
``sampled``, which the dispatch golden test locks). It is a separate,
non-comparable arm, so it needs its own guarantees: a random baseline that leaked
outside the candidate set would no longer be the "spatial" arm it claims to be.
"""

from __future__ import annotations

import random

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import spatial
from apps.container_logistics.assignment.solver import RandomAssignmentSolver
from apps.container_logistics.policies.dispatch import scopes
from apps.container_logistics.policies.dispatch.allowed import PartitionRule
from apps.container_logistics.policies.dispatch.context import DispatchClock
from apps.container_logistics.policies.dispatch.helpers.candidates import CandidateSettings
from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile
from apps.container_logistics.policies.dispatch.runner import DispatchRunner
from tests.test_assignment_cooperation import _app, _order, _truck

_CLOCK = DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=42)
_SPATIAL_PROFILE = {
    "policies": {"planner": {"type": "RandomAssignment", "timing": "online",
                              "params": {"candidates": "spatial"}}},
    "respect_truck_online_state": True,
    "reject_if_active_haul_trip": True,
    # Few candidates per order, so the spatial set is a strict subset of all pairs.
    "spatial_candidates_per_order": 2,
}


def _line_world(n_trucks=10, n_orders=10, haulier="acme"):
    """Trucks and orders spread ~2.2 km apart along a line (one haulier)."""
    trucks = [_truck(f"t{i}", haulier, 103.70 + 0.02 * i, 1.30) for i in range(n_trucks)]
    orders = [_order(f"o{i}", haulier, 103.70 + 0.02 * i + 0.003, 1.30) for i in range(n_orders)]
    return trucks, orders


def _spatial_runner(policy=None):
    policy = policy or RandomAssignmentSolver()
    resolved = resolve_dispatch_for_profile(_SPATIAL_PROFILE)
    assert resolved.candidate_kind == "spatial"
    return DispatchRunner(policy=policy, resolved=resolved)


def _ctx(runner, trucks, orders):
    # Round 2 C3: contexts come from dispatch/scopes.py only (no runner.partition_context).
    return scopes.partition_context(
        policy=runner.policy, params=runner.resolved.params, candidate_kind=runner.candidate_kind,
        trucks=trucks, orders=orders, partition_key=("haulier", "acme"),
        rule=PartitionRule(max_travel_time_pickup=None, adjacency={}),
        settings=CandidateSettings.from_profile(_SPATIAL_PROFILE),
        accounting_cost=assign_constraints.assignment_cost, sample_rng=random, clock=_CLOCK,
    )


def _ids(pairs):
    return [(t["_id"], o["_id"]) for t, o in pairs]


def test_random_spatial_uses_only_candidate_pairs():
    """Verified to have teeth: making ``RandomAssignmentSolver.decide`` call ``solve``
    over the sampled trucks on the spatial branch failed this test."""
    trucks, orders = _line_world()
    full = {(t["_id"], o["_id"]) for t in trucks for o in orders}
    for seed in range(20):
        policy = RandomAssignmentSolver()
        policy.set_rng(random.Random(seed))
        runner = _spatial_runner(policy)
        ctx = _ctx(runner, trucks, orders)
        candidates = set(_ids(ctx.candidate_pairs("spatial")))
        assert candidates and candidates < full, "the world must make spatial a strict subset"
        result = _ids(runner.run(ctx).pairs)
        assert result, f"seed {seed}: nothing matched"
        outside = [p for p in result if p not in candidates]
        assert not outside, f"seed {seed}: pairs outside the spatial candidates: {outside}"
        assert runner.stats["violations"] == 0


def test_random_spatial_never_double_assigns():
    """Verified to have teeth: removing the ``used_truck_ids`` filter in
    ``RandomAssignmentSolver.solve_pairs`` failed this test."""
    # Many orders competing for few trucks, plus repeated input pairs.
    trucks = [_truck(f"t{i}", "acme", 103.85 + 0.001 * i, 1.30) for i in range(3)]
    orders = [_order(f"o{i}", "acme", 103.85 + 0.001 * i, 1.30) for i in range(8)]
    pairs = [(t, o) for o in orders for t in trucks]
    pairs = pairs + pairs[:6]
    for seed in range(30):
        solver = RandomAssignmentSolver()
        solver.set_rng(random.Random(seed))
        result = _ids(solver.solve_pairs(list(pairs), cost=assign_constraints.assignment_cost))
        t_ids = [t for t, _ in result]
        o_ids = [o for _, o in result]
        assert len(t_ids) == len(set(t_ids)), f"seed {seed}: a truck was assigned twice: {result}"
        assert len(o_ids) == len(set(o_ids)), f"seed {seed}: an order was assigned twice: {result}"
        assert len(result) == len(trucks), f"seed {seed}: every truck has a candidate order: {result}"


def test_random_spatial_reproducible_under_seeded_rng():
    """Verified to have teeth: making ``solve_pairs`` shuffle with the ``random``
    module instead of ``self.rng`` failed this test."""
    trucks, orders = _line_world(n_trucks=8, n_orders=12)

    def _run(seed):
        policy = RandomAssignmentSolver()
        policy.set_rng(random.Random(seed))
        runner = _spatial_runner(policy)
        return _ids(runner.run(_ctx(runner, trucks, orders)).pairs)

    random.seed(1234)
    module_state = random.getstate()
    first = _run(7)
    assert random.getstate() == module_state, "an injected RNG must leave the random module untouched"
    random.seed(999)  # a different module state must not matter
    assert _run(7) == first, "the same seed must reproduce the same match"
    outcomes = {tuple(_run(seed)) for seed in range(10)}
    assert len(outcomes) > 1, "different seeds never changed the match: the RNG is not being used"


def test_assignment_app_random_spatial_commits_only_emitted_pairs(monkeypatch):
    """Verified to have teeth: making the spatial branch of
    ``RandomAssignmentSolver.decide`` read ``ctx.candidate_pairs("full")`` failed this test."""
    emitted = []
    real = spatial.iter_candidate_pairs

    def _recording(*args, **kwargs):
        for t, o in real(*args, **kwargs):
            emitted.append((t["_id"], o["_id"]))
            yield (t, o)

    monkeypatch.setattr(spatial, "iter_candidate_pairs", _recording)
    trucks, orders = _line_world()
    trucks += [_truck(f"b{i}", "borax", 103.70 + 0.02 * i, 1.31) for i in range(4)]
    orders += [_order(f"q{i}", "borax", 103.70 + 0.02 * i, 1.31) for i in range(4)]
    app = _app(trucks, orders, strategy="RandomAssignment")
    app.behavior = {"profile": dict(_SPATIAL_PROFILE)}
    app._solver = RandomAssignmentSolver()

    random.seed(5)
    result = _ids(app.assign("Wed, 01 Jan 2020 08:00:00 GMT", 42))
    runner = app._dispatch_runner(app.behavior["profile"])
    assert runner.candidate_kind == "spatial" and runner.resolved.source == "policies"
    assert emitted, "the spatial generator was never called: the profile did not select spatial"
    assert result, "nothing was assigned"
    outside = [p for p in result if p not in set(emitted)]
    assert not outside, f"committed pairs the spatial generator never emitted: {outside}"
    full = {(t["_id"], o["_id"]) for t in trucks for o in orders if t["haulier_id"] == o["haulier_id"]}
    assert set(emitted) < full, "the world must make spatial a strict subset of the allowed pairs"
