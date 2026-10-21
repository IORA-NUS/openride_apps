"""``DispatchWorld``: the framework's read-only world of one tick (plan round 2 §2.2, §5 C2).

What is locked:
- ``DispatchWorld.from_profile`` builds the same feasibility rule the partitioned
  path built inline at ``c30fd25`` (``app.py`` 615-650): checked against a FROZEN
  oracle copy of ``PartitionRule.allowed`` on a pair grid, so a later edit to either
  side cannot silently move both;
- ``component_of`` and the tick seed equal the app's inline/S2 and ``_tick_seed``;
- ``cooperation_pairs`` is every ``(owner, server)`` pair that may cooperate,
  INCLUDING self-loops (the cooperation set NetworkFlow needs);
- ``validate_cooperation_world`` refuses a pool that pairs companies the adjacency
  does not (message 14), and passes every structure the compiler normalizes.

Why: from C3 on, every dispatch policy's heads are re-checked against
``world.allowed`` and accounted with ``world.accounting_cost``/``own_allowed``; if the
world differed from the old inline rule, partitioned runs would stop being
byte-identical and cooperation feasibility would silently change.
"""

from __future__ import annotations

import itertools

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.datagen.hauliers import normalize_cooperation
from apps.container_logistics.policies.dispatch.world import (
    DispatchWorld,
    tick_seed,
    validate_cooperation_world,
)
from apps.container_logistics.policies.framework import PolicyError
from tests.test_assignment_cooperation import _order, _truck


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


@pytest.mark.parametrize("structure", [None, _CHAIN], ids=["no-coop", "chain"])
@pytest.mark.parametrize("mtt", [None, 300])
def test_world_rule_equals_partition_rule_on_a_pair_grid(structure, mtt):
    """Verified to have teeth: making ``DispatchWorld.allowed`` ignore the adjacency
    (``own-haulier only``) failed the chain cases; passing ``max_travel_time_pickup=None``
    to the rule failed the mtt=300 cases."""
    trucks = [_truck(f"t{h}{i}", h, 103.85 + 0.03 * i, 1.30) for h in "abcd" for i in range(2)]
    trucks.append({"_id": "tx", "state": "online", "profile": {"current_loc": trucks[0]["profile"]["current_loc"]}})
    orders = [_order(f"o{h}{i}", h, 103.85 + 0.04 * i, 1.30) for h in "abcd" for i in range(2)]
    world = DispatchWorld.from_profile(_profile(structure, mtt), accounting_params={})
    adjacency = C.share_adjacency(structure)
    differences = [(t["_id"], o["_id"]) for t, o in itertools.product(trucks, orders)
                   if world.allowed(t, o) != _oracle_allowed(t, o, mtt, adjacency)]
    assert not differences, f"world.allowed differs from the frozen rule on {differences[:5]}"
    assert any(world.allowed(t, o) for t, o in itertools.product(trucks, orders)), "grid must allow something"
    if structure is not None:
        assert world.allowed(trucks[0], orders[2]) and not world.allowed(trucks[0], orders[4]), \
            "a-b is an edge, a-c is not (edge-direct, not component)"


def test_world_carries_the_app_s_component_partition_and_accounting():
    """Verified to have teeth: dropping the ``len(members) > 1`` filter put the solo
    ``d`` into ``component_of``."""
    world = DispatchWorld.from_profile(_profile(_CHAIN, solver_params={"dual_cycle_bonus_km": 1.0}),
                                       accounting_params={"dual_cycle_bonus_km": 1.0})
    assert dict(world.component_of) == {"a": 0, "b": 0, "c": 0}
    assert world.structure == _CHAIN
    no_edges = DispatchWorld.from_profile(_profile({**_CHAIN, "adjacency": {}}), accounting_params={})
    assert dict(no_edges.component_of) == {}, "no adjacency: every haulier partitions alone"
    t, o = _truck("ta", "a", 103.85, 1.30), _order("ob", "b", 103.90, 1.30)
    assert world.accounting_cost(t, o) == C.assignment_cost(t, o, {"dual_cycle_bonus_km": 1.0})
    assert world.own_allowed(t, _order("oa", "a", 103.9, 1.3)) and not world.own_allowed(t, o)
    assert world.partners("b") == frozenset({"a", "c"}) and world.partners("d") == frozenset()
    assert world.may_serve("a", "b") and not world.may_serve("a", "c") and world.may_serve("d", "d")


def test_tick_seed_equals_the_app_formula():
    """Verified to have teeth: swapping the ``run_id|step`` order failed this test."""
    for run_id, step in (("run_golden", 42), ("r", 0), ("run_20261021_101010", 2519)):
        assert tick_seed(run_id, step) == AssignmentApp._tick_seed(run_id, step)
    assert tick_seed("r", 1) != tick_seed("r", 2)


def test_cooperation_pairs_include_self_loops():
    """Verified to have teeth: generating only ``owner != server`` pairs failed this test."""
    world = DispatchWorld.from_profile(_profile(_CHAIN), accounting_params={})
    assert world.cooperation_pairs(["c", "a", "b", "d", "a"]) == (
        ("a", "a"), ("a", "b"), ("b", "a"), ("b", "b"), ("b", "c"),
        ("c", "b"), ("c", "c"), ("d", "d"),
    )
    solo = DispatchWorld.from_profile({}, accounting_params={})
    assert solo.cooperation_pairs(["x", "y"]) == (("x", "x"), ("y", "y"))


def test_cooperation_world_check_refuses_pools_outside_adjacency():
    """Message 14. Verified to have teeth: checking one direction only
    (``b in adjacency[a]``) let the one-sided adjacency through."""
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

    Verified to have teeth: an early ``return`` in ``validate_cooperation_world`` failed
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
