"""The framework's world of one tick, after ``DispatchWorld`` was deleted (WS1, plan_contract_final §2).

What is locked:
- the top tick's ``permitted`` under the built-in top binds (ownership read off the two
  documents + the arm's declared ``(owner, carrier)`` pairs + physical feasibility)
  equals the partitioned path's inline rule at ``c30fd25`` (``app.py`` 615-650):
  checked against a FROZEN oracle copy of ``PartitionRule.allowed`` on a pair grid, so a
  later edit to either side cannot silently move both;
- ``solver_lib.cooperation.resolve_arm``'s ``component_of`` and the tick seed equal the
  app's inline/S2 and ``_tick_seed``; ``tick.measure.cost`` is the accounting cost and
  the owner reserve's rule is ``constraints.pair_allowed``;
- ``directed_pairs`` is every cross-company ``(owner, carrier)`` pair the arm permits
  (self-loops are not declared: a company always serves its own orders, which
  ``tick.serves`` answers from the documents);
- ``validate_arm`` refuses a pool that pairs companies the adjacency does not
  (message 14), and passes every structure the compiler normalizes.

Why: every planner result is re-checked against the top tick's ``permitted`` and
accounted with ``tick.measure.cost``; if that differed from the old inline rule,
partitioned runs would stop being byte-identical and cooperation feasibility would
silently change.
"""

from __future__ import annotations

import itertools

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.datagen.hauliers import normalize_cooperation
from apps.container_logistics.policies.dispatch.bind import RunWorld
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import DispatchClock, tick_seed
from apps.container_logistics.policies.framework import PolicyError
from apps.container_logistics.policies.solver_lib.cooperation import (
    directed_pairs,
    resolve_arm,
    validate_arm,
)
from tests.test_assignment_cooperation import _order, _truck


def validate_cooperation_world(profile):
    """Message 14 as the run-start bind reaches it (``validate_arm`` over the profile's run world)."""
    validate_arm(RunWorld.from_profile(profile))


def _tick(profile, trucks=(), orders=(), *, policy="Partitioned", dials=None):
    runner = build_runner_for_policy(policy, profile=profile)
    return runner.tick(candidates=list(trucks), orders=list(orders), profile=profile,
                       clock=DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=1),
                       seed=7, dials=dials)


def _oracle_allowed(t, o, mtt, adjacency):
    """FROZEN copy of ``app.py`` ``_pair_allowed`` at ``c30fd25`` (do not edit)."""
    if C.haulier_matches(t, o):
        return C.pair_allowed(t, o, max_travel_time_pickup=mtt)
    if adjacency and C.share_eligible(t, o, adjacency):
        return C.pair_allowed_ignoring_haulier(t, o, max_travel_time_pickup=mtt)
    return False


def _profile(structure, mtt=None, solver_params=None):
    prof = {"max_travel_time_pickup": mtt, "solver_params": solver_params or {}}
    if structure is not None:
        prof["cooperation"] = {"active": structure["id"], "structures": [structure]}
    return prof


_CHAIN = {"id": "chain", "edges": [["a", "b"], ["b", "c"]],
          "adjacency": {"a": ["b"], "b": ["a", "c"], "c": ["b"]},
          "components": [["a", "b", "c"], ["d"]], "pools": []}


@pytest.mark.parametrize("policy", ["Partitioned", "PooledMarket", "GreedyNearest", "RandomAssignment"])
@pytest.mark.parametrize("structure", [None, _CHAIN], ids=["no-coop", "chain"])
@pytest.mark.parametrize("mtt", [None, 300])
def test_world_rule_equals_partition_rule_on_a_pair_grid(structure, mtt, policy):
    """Every built-in top declares exactly the arm's pairs, so ``permitted`` is the old rule.

    Verified to have teeth: making ``directed_pairs`` return ``frozenset()``
    (own-haulier only) failed the chain cases; passing ``max_travel_time_pickup=None``
    to ``_top_permitted`` in ``Tick.top`` failed the mtt=300 cases."""
    trucks = [_truck(f"t{h}{i}", h, 103.85 + 0.03 * i, 1.30) for h in "abcd" for i in range(2)]
    trucks.append({"_id": "tx", "state": "online", "profile": {"current_loc": trucks[0]["profile"]["current_loc"]}})
    orders = [_order(f"o{h}{i}", h, 103.85 + 0.04 * i, 1.30) for h in "abcd" for i in range(2)]
    tick = _tick(_profile(structure, mtt), trucks, orders, policy=policy)
    adjacency = C.share_adjacency(structure)
    differences = [(t["_id"], o["_id"]) for t, o in itertools.product(trucks, orders)
                   if tick.permitted(t, o) != _oracle_allowed(t, o, mtt, adjacency)]
    assert not differences, f"tick.permitted differs from the frozen rule on {differences[:5]}"
    assert any(tick.permitted(t, o) for t, o in itertools.product(trucks, orders)), "grid must allow something"
    if structure is not None:
        assert tick.permitted(trucks[0], orders[2]) and not tick.permitted(trucks[0], orders[4]), \
            "a-b is an edge, a-c is not (edge-direct, not component)"


def test_world_carries_the_app_s_component_partition_and_accounting():
    """Verified to have teeth: dropping the ``len(members) > 1`` filter in
    ``cooperation._component_of`` put the solo ``d`` into ``component_of``."""
    prof = _profile(_CHAIN, solver_params={"dual_cycle_bonus_km": 1.0})
    arm = resolve_arm(RunWorld.from_profile(prof))
    assert dict(arm.component_of) == {"a": 0, "b": 0, "c": 0}
    assert arm.structure == _CHAIN and arm.id == "chain"
    no_edges = resolve_arm(RunWorld.from_profile(_profile({**_CHAIN, "adjacency": {}})))
    assert dict(no_edges.component_of) == {}, "no adjacency: every haulier partitions alone"
    assert resolve_arm(RunWorld.from_profile({})) is None
    t, o = _truck("ta", "a", 103.85, 1.30), _order("ob", "b", 103.90, 1.30)
    tick = _tick(prof, [t], [o], dials={"dual_cycle_bonus_km": 1.0})
    assert tick.measure.cost(t, o) == C.assignment_cost(t, o, {"dual_cycle_bonus_km": 1.0})
    # The owner reserve's rule (AssignmentApp._dispatch_tick's own_allowed): strict own-haulier.
    own = _order("oa", "a", 103.9, 1.3)
    assert C.pair_allowed(t, own, max_travel_time_pickup=None) and not C.pair_allowed(t, o, max_travel_time_pickup=None)
    assert arm.adjacency["b"] == frozenset({"a", "c"}) and arm.adjacency.get("d", frozenset()) == frozenset()
    # serves(carrier, owner): b's trucks may serve a's orders; a's may not serve c's; own always.
    assert tick.serves("a", "b") and not tick.serves("a", "c") and tick.serves("d", "d")


def test_tick_seed_equals_the_app_formula():
    """Verified to have teeth: swapping the ``run_id|step`` order failed this test."""
    for run_id, step in (("run_golden", 42), ("r", 0), ("run_20261021_101010", 2519)):
        assert tick_seed(run_id, step) == AssignmentApp._tick_seed(run_id, step)
    assert tick_seed("r", 1) != tick_seed("r", 2)


def test_directed_pairs_are_the_arm_s_cross_company_pairs():
    """The top's declaration is the arm's directed pairs; own pairs come from the documents.

    Verified to have teeth: dropping the ``owner != carrier`` filter in ``directed_pairs``
    failed the self-loop assertion (``(b, b)`` was declared from a self-adjacency)."""
    looped = {**_CHAIN, "adjacency": {**_CHAIN["adjacency"], "b": ["a", "b", "c"]}}
    arm = resolve_arm(RunWorld.from_profile(_profile(looped)))
    assert directed_pairs(arm) == {("a", "b"), ("b", "a"), ("b", "c"), ("c", "b")}
    assert directed_pairs(None) == frozenset()
    tick = _tick(_profile(looped))
    assert tick._shared.tree.declaration.cross_company == directed_pairs(arm)
    assert all(tick.serves(h, h) for h in "abcdxy"), "a company always serves its own orders"


def test_cooperation_world_check_refuses_pools_outside_adjacency():
    """Message 14. Verified to have teeth: checking one direction only
    (``b in adjacency[a]``) in ``cooperation.validate_arm`` let the one-sided adjacency through."""
    bad = {"id": "hand-edit", "adjacency": {"a": ["b"], "b": ["a"]},
           "pools": [{"id": "trio", "members": ["a", "b", "c"]}]}
    with pytest.raises(PolicyError) as exc:
        validate_cooperation_world(_profile(bad))
    assert str(exc.value) == (
        "cooperation structure 'hand-edit': pool 'trio' lets a and c share orders but the "
        "structure's adjacency does not pair them. Pools and adjacency must agree; recompile "
        "the scenario (pools are canonical)."
    )
    one_sided = {"id": "one-sided", "adjacency": {"a": ["b"]}, "pools": [{"id": "ab", "members": ["a", "b"]}]}
    with pytest.raises(PolicyError, match="pool 'ab' lets a and b"):
        validate_cooperation_world(_profile(one_sided))
    # Edge-derived pools (no 'pools' key) disagreeing with a hand-edited adjacency.
    edges_only = {"id": "edges", "edges": [["a", "b"]], "adjacency": {}}
    with pytest.raises(PolicyError, match="pool 'p:a\\+b'"):
        validate_cooperation_world(_profile(edges_only))
    validate_cooperation_world({})  # no cooperation: nothing to check
    validate_cooperation_world(_profile({"id": "baseline", "edges": [], "adjacency": {}, "pools": []}))


@pytest.mark.parametrize("authored", [
    {"structures": [{"id": "chain", "edges": [["a", "b"], ["b", "c"]]}]},
    {"structures": [{"id": "trio", "pools": [{"id": "p1", "members": ["a", "b", "c"]},
                                             {"id": "p2", "members": ["c", "d"]}]}]},
], ids=["edges", "pools"])
def test_normalized_structures_pass_the_check(authored):
    """Every structure the compiler normalizes is consistent by construction.

    Verified to have teeth: an early ``return`` in ``cooperation.validate_arm`` failed
    the probe (the same normalized structure with its adjacency removed) in both cases.
    """
    normalized = normalize_cooperation(authored, ["a", "b", "c", "d"])
    for structure in normalized["structures"]:
        validate_cooperation_world({"cooperation": {**normalized, "active": structure["id"]}})
    declared = normalized["structures"][-1]
    probe = {**normalized, "active": declared["id"],
             "structures": [{**declared, "adjacency": {}}]}
    with pytest.raises(PolicyError, match="adjacency does not pair them"):
        validate_cooperation_world({"cooperation": probe})
