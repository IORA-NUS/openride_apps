"""PoolMarket + pools_from_structure / pools_of_structure (shared-pool
cooperation plan, Phase 2 — docs/shared_pool_cooperation_plan.md §3.5, §5, §6.1,
§6.8, §9). Pure: these primitives are not yet wired into ``assignment/app.py``.
"""

import copy
import logging

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.assignment.pools import (
    DERIVED_POOL_PREFIX,
    Award,
    Pool,
    PoolMarket,
    pools_from_structure,
)


# --- pools_from_structure --------------------------------------------------


def test_from_structure_empty_when_no_edges_or_pools():
    assert pools_from_structure(None) == ()
    assert pools_from_structure("not a dict") == ()
    assert pools_from_structure({}) == ()
    assert pools_from_structure({"edges": []}) == ()
    assert pools_from_structure({"pools": []}) == ()
    assert pools_from_structure({"edges": [], "pools": []}) == ()


def test_edges_derive_one_pool_per_edge():
    structure = {"edges": [["acme", "borax"], ["borax", "cargo"]]}
    pools = pools_from_structure(structure)
    assert [p.id for p in pools] == [
        f"{DERIVED_POOL_PREFIX}acme+borax",
        f"{DERIVED_POOL_PREFIX}borax+cargo",
    ]
    assert pools[0].members == frozenset({"acme", "borax"})
    assert pools[1].members == frozenset({"borax", "cargo"})


def test_authored_pools_take_precedence_over_edges():
    structure = {
        "edges": [["acme", "borax"]],
        "pools": [{"id": "north", "members": ["acme", "borax", "cargo"]}],
    }
    pools = pools_from_structure(structure)
    assert [p.id for p in pools] == ["north"]
    assert pools[0].members == frozenset({"acme", "borax", "cargo"})


def test_pools_from_structure_never_raises_on_malformed_input():
    malformed_inputs = [
        {"pools": "not a list"},
        {"pools": [None, 42, "x", []]},
        {"pools": [{"id": None, "members": ["a", "b"]}]},
        {"pools": [{"id": "", "members": ["a", "b"]}]},
        {"pools": [{"id": "p1"}]},  # missing members
        {"pools": [{"id": "p1", "members": "ab"}]},  # not a real member list... actually a string is iterable of chars
        {"pools": [{"id": "p1", "members": [1, 2, 3]}]},  # non-string members
        {"pools": [{"id": "p1", "members": ["only-one"]}]},  # <2 distinct members
        {"pools": [{"id": "dup", "members": ["a", "b"]}, {"id": "dup", "members": ["c", "d"]}]},
        {"edges": "not a list"},
        {"edges": [None, 42, ["a"], ["a", "b", "c"]]},
        {"edges": [["a", "a"]]},  # self-loop
        {"edges": [[1, 2]]},  # non-string members
        {"edges": [["a", ""]]},
    ]
    for structure in malformed_inputs:
        result = pools_from_structure(structure)
        assert isinstance(result, tuple), structure


def test_pools_from_structure_output_ordering_is_deterministic():
    structure = {"edges": [["cargo", "borax"], ["acme", "borax"], ["delta", "acme"]]}
    first = pools_from_structure(structure)
    second = pools_from_structure(copy.deepcopy(structure))
    assert first == second
    assert [p.id for p in first] == sorted(p.id for p in first)

    # Reordering the authored pool list must not change the resulting order.
    unordered = {
        "pools": [
            {"id": "south", "members": ["cargo", "delta"]},
            {"id": "north", "members": ["acme", "borax"]},
        ]
    }
    reordered = {
        "pools": [
            {"id": "north", "members": ["acme", "borax"]},
            {"id": "south", "members": ["cargo", "delta"]},
        ]
    }
    assert pools_from_structure(unordered) == pools_from_structure(reordered)
    assert [p.id for p in pools_from_structure(unordered)] == ["north", "south"]


# --- constraints.pools_of_structure ----------------------------------------


def test_pools_of_structure_reads_defensively():
    assert C.pools_of_structure(None) == []
    assert C.pools_of_structure("nope") == []
    assert C.pools_of_structure({}) == []
    assert C.pools_of_structure({"pools": "nope"}) == []
    assert C.pools_of_structure(
        {"pools": [{"id": "north", "members": ["borax", "acme"]}, {"id": "bad"}, "junk"]}
    ) == [{"id": "north", "members": ["acme", "borax"]}]


# --- PoolMarket: membership -------------------------------------------------


def _chain_market():
    """A—B, B—C: pools p:acme+borax and p:borax+cargo."""
    return PoolMarket.from_structure(
        {"edges": [["acme", "borax"], ["borax", "cargo"]]}
    )


def test_pool_market_from_structure_pools_for_and_member_of():
    market = _chain_market()
    assert not market.is_empty
    assert [p.id for p in market.pools_for("borax")] == [
        f"{DERIVED_POOL_PREFIX}acme+borax",
        f"{DERIVED_POOL_PREFIX}borax+cargo",
    ]
    assert [p.id for p in market.pools_for("acme")] == [f"{DERIVED_POOL_PREFIX}acme+borax"]
    assert market.pools_for("delta") == ()
    assert market.member_of("acme", f"{DERIVED_POOL_PREFIX}acme+borax") is True
    assert market.member_of("cargo", f"{DERIVED_POOL_PREFIX}acme+borax") is False
    assert market.member_of("acme", "no-such-pool") is False


def test_pool_market_empty_when_no_pools():
    assert PoolMarket.from_structure(None).is_empty
    assert PoolMarket.from_structure({"edges": []}).is_empty
    assert PoolMarket(()).is_empty


# --- visibility --------------------------------------------------------


def test_visible_pooled_order_ids_respects_membership():
    market = _chain_market()
    p_ab = f"{DERIVED_POOL_PREFIX}acme+borax"
    p_bc = f"{DERIVED_POOL_PREFIX}borax+cargo"

    assert market.contribute("o_acme", "acme", [p_ab]) == 1
    assert market.contribute("o_cargo", "cargo", [p_bc]) == 1

    # An owner never sees its own contributed order via this method (that is
    # the planner host's job, from the company's own order list) — so acme's
    # visible set here is empty (its only contribution is its own order) and,
    # crucially, A must NEVER see C's contribution and vice versa.
    assert market.visible_pooled_order_ids("acme") == frozenset()
    assert "o_cargo" not in market.visible_pooled_order_ids("acme")
    assert market.visible_pooled_order_ids("cargo") == frozenset()
    assert "o_acme" not in market.visible_pooled_order_ids("cargo")

    # B is a member of both pools, owns neither contribution, and sees both.
    assert market.visible_pooled_order_ids("borax") == frozenset({"o_acme", "o_cargo"})


def test_pool_of_order_is_deterministic_lowest_id_and_none_when_not_visible():
    market = PoolMarket.from_structure(
        {
            "pools": [
                {"id": "z_pool", "members": ["acme", "borax"]},
                {"id": "a_pool", "members": ["acme", "borax"]},
            ]
        }
    )
    market.contribute("o1", "acme", ["z_pool", "a_pool"])
    assert market.pool_of_order("o1", "borax") == "a_pool"
    assert market.pool_of_order("o1", "delta") is None
    assert market.pool_of_order("no-such-order", "acme") is None


def test_is_contributed():
    market = _chain_market()
    p_ab = f"{DERIVED_POOL_PREFIX}acme+borax"
    assert market.is_contributed("o_acme") is False
    market.contribute("o_acme", "acme", [p_ab])
    assert market.is_contributed("o_acme") is True


# --- contribution validation -------------------------------------------


def test_contribute_rejects_non_owner_and_non_member(caplog):
    market = _chain_market()
    p_ab = f"{DERIVED_POOL_PREFIX}acme+borax"
    p_bc = f"{DERIVED_POOL_PREFIX}borax+cargo"

    with caplog.at_level(logging.WARNING):
        # Non-member: delta is not in p:acme+borax.
        accepted = market.contribute("o_delta", "delta", [p_ab])
    assert accepted == 0
    assert market.is_contributed("o_delta") is False
    assert any("member" in rec.message for rec in caplog.records)

    caplog.clear()
    # Legitimate contribution first, establishing acme as the recorded owner.
    accepted = market.contribute("o_acme", "acme", [p_ab])
    assert accepted == 1

    with caplog.at_level(logging.WARNING):
        # Non-owner: borax IS a member of p_ab, but is not the recorded
        # owner of o_acme -> the whole call must be rejected (not raised).
        accepted = market.contribute("o_acme", "borax", [p_ab])
    assert accepted == 0
    assert any("owner" in rec.message for rec in caplog.records)
    # The original acme contribution is untouched.
    assert market.pool_of_order("o_acme", "acme") == p_ab

    with caplog.at_level(logging.WARNING):
        # Unknown pool id.
        accepted = market.contribute("o_cargo", "cargo", ["no-such-pool", p_bc])
    assert accepted == 1  # only p_bc accepted
    assert any("unknown pool" in rec.message.lower() for rec in caplog.records)

    # Never raises even with a nonsense pool_ids sequence.
    assert market.contribute("o_x", "acme", []) == 0


def test_contribute_never_raises_on_bad_input():
    market = _chain_market()
    # Unknown haulier id, unknown pool id, empty pool list -- all fail-soft.
    assert market.contribute("o1", "nobody", ["p:acme+borax"]) == 0
    assert market.contribute("o2", "acme", []) == 0
    assert market.contribute("o3", "acme", ["totally-bogus"]) == 0


# --- commit / allocation state ------------------------------------------


def _award(order_id="o1", truck_id="t1", carrier="borax", owner="acme", round_=1, pool_id=None):
    return Award(
        order_id=order_id,
        truck_id=truck_id,
        carrier_haulier_id=carrier,
        owner_haulier_id=owner,
        cost_km=1.5,
        pool_id=pool_id,
        round=round_,
    )


def test_commit_is_idempotent_and_rejects_double_order_or_truck():
    market = _chain_market()
    award1 = _award()
    assert market.is_order_free("o1") is True
    assert market.is_truck_free("t1") is True

    assert market.commit(award1) is True
    assert market.is_order_free("o1") is False
    assert market.is_truck_free("t1") is False
    assert market.awards == (award1,)

    # Committing the exact same award twice returns False the second time.
    assert market.commit(award1) is False
    assert market.awards == (award1,)

    # Same order id, different truck -> rejected (order already committed).
    dup_order = _award(order_id="o1", truck_id="t2")
    assert market.commit(dup_order) is False
    assert market.awards == (award1,)

    # Same truck id, different order -> rejected (truck already committed).
    dup_truck = _award(order_id="o2", truck_id="t1")
    assert market.commit(dup_truck) is False
    assert market.awards == (award1,)

    # A genuinely new order+truck pair still commits fine.
    award2 = _award(order_id="o2", truck_id="t2")
    assert market.commit(award2) is True
    assert market.awards == (award1, award2)
    assert [a.order_id for a in market.awards] == ["o1", "o2"]  # commit order preserved


def test_is_order_free_and_is_truck_free_default_true():
    market = _chain_market()
    assert market.is_order_free("never-seen") is True
    assert market.is_truck_free("never-seen") is True


# --- ownership immutability (I-P3) --------------------------------------


def _order_doc(oid, haulier):
    return {
        "_id": oid,
        "state": "unassigned",
        "haulier_id": haulier,
        "profile": {"haulier_id": haulier},
    }


def test_ownership_never_mutated_by_contribution():
    market = _chain_market()
    p_ab = f"{DERIVED_POOL_PREFIX}acme+borax"
    p_bc = f"{DERIVED_POOL_PREFIX}borax+cargo"

    order_acme = _order_doc("o_acme", "acme")
    order_cargo = _order_doc("o_cargo", "cargo")
    order_delta = _order_doc("o_delta", "delta")  # will be rejected (non-member)
    before = copy.deepcopy([order_acme, order_cargo, order_delta])

    market.contribute(order_acme["_id"], C.haulier_of(order_acme), [p_ab])
    market.contribute(order_cargo["_id"], C.haulier_of(order_cargo), [p_bc])
    market.contribute(order_delta["_id"], C.haulier_of(order_delta), [p_ab])  # rejected

    after = [order_acme, order_cargo, order_delta]
    assert after == before, "contribute() must never mutate the order documents it is told about"
    # And haulier_of (the ownership accessor) is unaffected either way.
    assert C.haulier_of(order_acme) == "acme"
    assert C.haulier_of(order_cargo) == "cargo"
    assert C.haulier_of(order_delta) == "delta"


def test_pool_and_bid_and_award_are_frozen_dataclasses():
    pool = Pool(id="p:acme+borax", members=frozenset({"acme", "borax"}))
    with pytest.raises(Exception):
        pool.id = "changed"  # type: ignore[misc]
    award = _award()
    with pytest.raises(Exception):
        award.cost_km = 99.0  # type: ignore[misc]
