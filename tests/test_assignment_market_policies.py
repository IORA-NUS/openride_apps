"""Offer/claim policies + arbitration rules (shared-pool cooperation plan,
Phase 3 — docs/shared_pool_cooperation_plan.md §1, §6.2, §6.3, §6.4, §7, §9).

Pure: none of these are wired into ``assignment/app.py`` yet. The tests lock in
(a) the three registries' fail-soft plug-and-play contract, exactly as
``test_container_logistics_solver.py`` locks ``SOLVER_REGISTRY``, and (b) the
two non-negotiable arbitration properties: no market mutation, and
order-independence (I-P5).
"""

import itertools
import math
import random

import pytest

from apps.container_logistics.assignment.arbitration import (
    ARBITRATION_REGISTRY,
    DEFAULT_ARBITRATION_RULE,
    BaseArbitrationRule,
    HighestBenefitRule,
    LowestCostRule,
    OwnerFirstRule,
    RandomAwardRule,
    bid_tiebreak,
    get_arbitration_rule,
)
from apps.container_logistics.assignment.policy.claim import (
    CLAIM_REGISTRY,
    DEFAULT_CLAIM_POLICY,
    ClaimAllPlannedPolicy,
    ClaimIfGainExceedsPolicy,
    ClaimNonePolicy,
    get_claim_policy,
)
from apps.container_logistics.assignment.policy.offer import (
    DEFAULT_OFFER_POLICY,
    OFFER_REGISTRY,
    OfferAllPolicy,
    OfferNonePolicy,
    OfferSparePolicy,
    get_offer_policy,
)
from apps.container_logistics.assignment.pools import Award, Bid, Pool, PoolMarket

TICK_SEED = 987654321


# --- fixtures / helpers -----------------------------------------------------


def _truck(tid, haulier_id):
    return {"_id": tid, "profile": {"haulier_id": haulier_id}}


def _order(oid, haulier_id):
    return {"_id": oid, "profile": {"haulier_id": haulier_id}}


def _cost_table(table):
    """``cost(truck, order)`` from a ``{(truck_id, order_id): km}`` table;
    missing pairs are infeasible (``inf``), matching the solver contract."""

    def cost(truck, order):
        return table.get((truck["_id"], order["_id"]), math.inf)

    return cost


_NO_COST = _cost_table({})


def _pool(pid, *members):
    return Pool(id=pid, members=frozenset(members))


def _market(*pools):
    return PoolMarket(pools)


def _bid(haulier_id, order_id, truck_id, cost_km, pool_id="p:acme+borax", round=1):
    return Bid(
        haulier_id=haulier_id,
        order_id=order_id,
        truck_id=truck_id,
        cost_km=cost_km,
        pool_id=pool_id,
        round=round,
    )


def _owner_of(mapping):
    return lambda order_id: mapping.get(order_id)


def _tuples(awards):
    return [
        (a.order_id, a.truck_id, a.carrier_haulier_id, a.owner_haulier_id, a.cost_km,
         a.pool_id, a.round)
        for a in awards
    ]


# --- registries: exact set + fail-soft (plan §7) ----------------------------


def test_offer_registry_contains_known_policies():
    assert set(OFFER_REGISTRY) == {"OfferAll", "OfferNone", "OfferSpare"}


def test_claim_registry_contains_known_policies():
    assert set(CLAIM_REGISTRY) == {"ClaimAllPlanned", "ClaimNone", "ClaimIfGainExceeds"}


def test_arbitration_registry_contains_known_rules():
    assert set(ARBITRATION_REGISTRY) == {
        "LowestCost",
        "OwnerFirst",
        "HighestBenefit",
        "RandomAward",
    }


def test_offer_registry_fail_soft_unknown_name(caplog):
    with caplog.at_level("WARNING"):
        policy = get_offer_policy("DoesNotExist", None)
    assert type(policy) is OFFER_REGISTRY[DEFAULT_OFFER_POLICY]
    assert "Unknown offer policy" in caplog.text
    # missing name is the same fail-soft path, without being an error
    assert type(get_offer_policy(None, None)) is OFFER_REGISTRY[DEFAULT_OFFER_POLICY]
    assert isinstance(get_offer_policy("OfferSpare", {"keep_below_km": 4.0}), OfferSparePolicy)
    assert get_offer_policy("OfferSpare", {"keep_below_km": 4.0}).params["keep_below_km"] == 4.0


def test_claim_registry_fail_soft_unknown_name(caplog):
    with caplog.at_level("WARNING"):
        policy = get_claim_policy("Nope", None)
    assert type(policy) is CLAIM_REGISTRY[DEFAULT_CLAIM_POLICY]
    assert "Unknown claim policy" in caplog.text
    assert type(get_claim_policy(None, None)) is CLAIM_REGISTRY[DEFAULT_CLAIM_POLICY]
    assert isinstance(get_claim_policy("ClaimNone", {}), ClaimNonePolicy)
    assert get_claim_policy("ClaimIfGainExceeds", {"min_gain_km": 7}).params["min_gain_km"] == 7


def test_arbitration_registry_fail_soft_unknown_name(caplog):
    with caplog.at_level("WARNING"):
        rule = get_arbitration_rule("Nope", None)
    assert type(rule) is ARBITRATION_REGISTRY[DEFAULT_ARBITRATION_RULE]
    assert "Unknown arbitration rule" in caplog.text
    assert type(get_arbitration_rule(None, None)) is ARBITRATION_REGISTRY[DEFAULT_ARBITRATION_RULE]
    assert isinstance(get_arbitration_rule("OwnerFirst", {}), OwnerFirstRule)


def test_registry_defaults_are_the_documented_names():
    assert DEFAULT_OFFER_POLICY == "OfferAll"
    assert DEFAULT_CLAIM_POLICY == "ClaimAllPlanned"
    assert DEFAULT_ARBITRATION_RULE == "LowestCost"


# --- offer policies ---------------------------------------------------------


def test_offer_all_contributes_every_own_order_to_every_own_pool():
    pools = (_pool("p:acme+borax", "acme", "borax"), _pool("p:acme+cargo", "acme", "cargo"))
    orders = [_order("o1", "acme"), _order("o2", "acme"), _order("foreign", "borax")]
    out = OfferAllPolicy().offer(
        haulier_id="acme",
        orders=orders,
        trucks=[_truck("t1", "acme")],
        pools=pools,
        rng=random.Random(0),
        cost=_NO_COST,
    )
    assert out == {
        "o1": ["p:acme+borax", "p:acme+cargo"],
        "o2": ["p:acme+borax", "p:acme+cargo"],
    }
    # a company in no pool contributes nothing, by construction
    assert OfferAllPolicy().offer(
        haulier_id="delta", orders=[_order("o9", "delta")], trucks=[], pools=(),
        rng=random.Random(0), cost=_NO_COST,
    ) == {}


def test_offer_none_is_equivalent_to_no_cooperation():
    pools = (_pool("p:acme+borax", "acme", "borax"),)
    orders = [_order("o1", "acme"), _order("o2", "acme")]

    assert OfferNonePolicy().offer(
        haulier_id="acme", orders=orders, trucks=[_truck("t1", "acme")], pools=pools,
        rng=random.Random(0), cost=_NO_COST,
    ) == {}

    # nothing contributed => the partner sees nothing, i.e. no cooperation.
    market = _market(*pools)
    for oid, pool_ids in OfferNonePolicy().offer(
        haulier_id="acme", orders=orders, trucks=[], pools=pools,
        rng=random.Random(0), cost=_NO_COST,
    ).items():
        market.contribute(oid, "acme", pool_ids)
    assert market.visible_pooled_order_ids("borax") == frozenset()

    # contrast: OfferAll on the same input does make them visible
    coop = _market(*pools)
    for oid, pool_ids in OfferAllPolicy().offer(
        haulier_id="acme", orders=orders, trucks=[], pools=pools,
        rng=random.Random(0), cost=_NO_COST,
    ).items():
        coop.contribute(oid, "acme", pool_ids)
    assert coop.visible_pooled_order_ids("borax") == frozenset({"o1", "o2"})


def test_offer_spare_keeps_cheap_orders_private():
    pools = (_pool("p:acme+borax", "acme", "borax"),)
    cheap = _order("cheap", "acme")
    pricey = _order("pricey", "acme")
    unservable = _order("unservable", "acme")
    trucks = [_truck("t1", "acme"), _truck("t2", "acme")]
    cost = _cost_table({
        ("t1", "cheap"): 2.0,
        ("t2", "cheap"): 40.0,
        ("t1", "pricey"): 50.0,
        ("t2", "pricey"): 60.0,
        # 'unservable' has no feasible truck at all
    })

    out = OfferSparePolicy({"keep_below_km": 10.0, "only_when_short": False}).offer(
        haulier_id="acme",
        orders=[cheap, pricey, unservable],
        trucks=trucks,
        pools=pools,
        rng=random.Random(0),
        cost=cost,
    )
    assert "cheap" not in out
    assert out == {"pricey": ["p:acme+borax"], "unservable": ["p:acme+borax"]}


def test_offer_spare_defaults_behave_like_offer_all_when_short():
    """With ``keep_below_km=inf`` and ``only_when_short=True`` (the defaults)
    and fewer free trucks than orders, OfferSpare == OfferAll."""
    pools = (_pool("p:acme+borax", "acme", "borax"),)
    orders = [_order("o1", "acme"), _order("o2", "acme"), _order("o3", "acme")]
    trucks = [_truck("t1", "acme")]
    cost = _cost_table({("t1", "o1"): 1.0, ("t1", "o2"): 2.0, ("t1", "o3"): 3.0})
    kwargs = dict(
        haulier_id="acme", orders=orders, trucks=trucks, pools=pools,
        rng=random.Random(0), cost=cost,
    )
    assert OfferSparePolicy().offer(**kwargs) == OfferAllPolicy().offer(**kwargs)


def test_offer_spare_not_short_with_defaults_offers_only_unservable_orders():
    """Documented consequence of the literal predicate: with the default
    ``keep_below_km=inf`` and enough trucks, only orders it cannot serve at all
    are offered. (Plan §12.4 flags this predicate as under-specified.)"""
    pools = (_pool("p:acme+borax", "acme", "borax"),)
    orders = [_order("o1", "acme"), _order("o2", "acme")]
    trucks = [_truck("t1", "acme"), _truck("t2", "acme")]
    cost = _cost_table({("t1", "o1"): 1.0, ("t2", "o1"): 2.0})  # o2 unservable
    out = OfferSparePolicy().offer(
        haulier_id="acme", orders=orders, trucks=trucks, pools=pools,
        rng=random.Random(0), cost=cost,
    )
    assert out == {"o2": ["p:acme+borax"]}


# --- claim policies ---------------------------------------------------------


def _claim_market():
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    market.contribute("pooled", "borax", ["p:acme+borax"])
    return market


def test_claim_all_planned_bids_every_pooled_pair():
    market = _claim_market()
    truck = _truck("t1", "acme")
    pooled = _order("pooled", "borax")
    cost = _cost_table({("t1", "pooled"): 12.5})
    bids = ClaimAllPlannedPolicy().bids(
        haulier_id="acme",
        pooled_pairs=[(truck, pooled)],
        own_pairs=[],
        cost=cost,
        market=market,
        rng=random.Random(0),
        round=2,
    )
    assert bids == [
        Bid(haulier_id="acme", order_id="pooled", truck_id="t1", cost_km=12.5,
            pool_id="p:acme+borax", round=2)
    ]


def test_claim_none_returns_no_bids():
    market = _claim_market()
    assert ClaimNonePolicy().bids(
        haulier_id="acme",
        pooled_pairs=[(_truck("t1", "acme"), _order("pooled", "borax"))],
        own_pairs=[(_truck("t2", "acme"), _order("own", "acme"))],
        cost=_NO_COST,
        market=market,
        rng=random.Random(0),
        round=1,
    ) == []


def test_claim_if_gain_exceeds_respects_min_gain_km():
    """Pooled pair costs 10; the same truck's best own alternative costs 13 =>
    gain 3 km. ``min_gain_km=5`` must suppress it; 0 and 3 must not."""
    market = _claim_market()
    truck = _truck("t1", "acme")
    pooled = _order("pooled", "borax")
    own = _order("own", "acme")
    cost = _cost_table({("t1", "pooled"): 10.0, ("t1", "own"): 13.0, ("t2", "own"): 1.0})
    kwargs = dict(
        haulier_id="acme",
        pooled_pairs=[(truck, pooled)],
        own_pairs=[(_truck("t2", "acme"), own)],
        cost=cost,
        market=market,
        rng=random.Random(0),
        round=1,
    )

    assert ClaimIfGainExceedsPolicy({"min_gain_km": 5.0}).bids(**kwargs) == []

    for min_gain in (0.0, 3.0):
        bids = ClaimIfGainExceedsPolicy({"min_gain_km": min_gain}).bids(**kwargs)
        assert [b.order_id for b in bids] == ["pooled"]
        assert bids[0].cost_km == 10.0
    # default params == min_gain_km 0.0
    assert [b.order_id for b in ClaimIfGainExceedsPolicy().bids(**kwargs)] == ["pooled"]


def test_claim_if_gain_exceeds_always_bids_when_there_is_no_own_alternative():
    """Interpretation lock (documented in the class docstring): empty
    ``own_pairs`` => unbounded gain => always bid, even at a high min_gain_km."""
    market = _claim_market()
    bids = ClaimIfGainExceedsPolicy({"min_gain_km": 1000.0}).bids(
        haulier_id="acme",
        pooled_pairs=[(_truck("t1", "acme"), _order("pooled", "borax"))],
        own_pairs=[],
        cost=_cost_table({("t1", "pooled"): 99.0}),
        market=market,
        rng=random.Random(0),
        round=1,
    )
    assert [b.order_id for b in bids] == ["pooled"]


# --- arbitration: behaviour -------------------------------------------------


def test_lowest_cost_awards_cheapest_bid():
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    owner_of = _owner_of({"o1": "acme"})
    bids = [
        _bid("acme", "o1", "t_acme", 30.0),
        _bid("borax", "o1", "t_borax", 5.0),
        _bid("borax", "o1", "t_borax2", 12.0),
    ]
    awards = LowestCostRule().resolve(
        bids, market=market, tick_seed=TICK_SEED, owner_of=owner_of, rng=random.Random(0)
    )
    assert len(awards) == 1
    award = awards[0]
    assert (award.order_id, award.truck_id, award.carrier_haulier_id) == ("o1", "t_borax", "borax")
    assert award.owner_haulier_id == "acme"
    assert award.cost_km == 5.0
    assert award.pool_id == "p:acme+borax"
    assert award.round == 1


def test_owner_first_beats_cheaper_partner():
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    owner_of = _owner_of({"o1": "acme"})
    bids = [
        _bid("acme", "o1", "t_acme", 30.0),   # owner, expensive
        _bid("borax", "o1", "t_borax", 5.0),  # partner, cheap
    ]
    lowest = LowestCostRule().resolve(
        bids, market=market, tick_seed=TICK_SEED, owner_of=owner_of, rng=random.Random(0)
    )
    owner_first = OwnerFirstRule().resolve(
        bids, market=market, tick_seed=TICK_SEED, owner_of=owner_of, rng=random.Random(0)
    )
    assert [a.carrier_haulier_id for a in lowest] == ["borax"]
    assert [a.carrier_haulier_id for a in owner_first] == ["acme"]
    assert owner_first[0].cost_km == 30.0


def test_owner_first_falls_back_to_lowest_cost_among_partners():
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    owner_of = _owner_of({"o1": "acme"})  # owner submits nothing
    bids = [
        _bid("borax", "o1", "t_b1", 9.0),
        _bid("cargo", "o1", "t_c1", 4.0),
    ]
    awards = OwnerFirstRule().resolve(
        bids, market=market, tick_seed=TICK_SEED, owner_of=owner_of, rng=random.Random(0)
    )
    assert [(a.carrier_haulier_id, a.cost_km) for a in awards] == [("cargo", 4.0)]


def test_highest_benefit_prefers_the_largest_owner_saving():
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    owner_of = _owner_of({"o1": "acme", "o2": "borax"})
    bids = [
        # o1: owner reserve 40, partner at 10 => benefit 30
        _bid("acme", "o1", "t_a1", 40.0),
        _bid("borax", "o1", "t_b1", 10.0),
        # o2: owner reserve 12, partner at 9 => benefit 3 (cheaper, smaller gain)
        _bid("borax", "o2", "t_b2", 12.0),
        _bid("acme", "o2", "t_a2", 9.0),
    ]
    awards = HighestBenefitRule().resolve(
        bids, market=market, tick_seed=TICK_SEED, owner_of=owner_of, rng=random.Random(0)
    )
    # the 30 km-benefit award must be ranked first, before the 3 km one
    assert [(a.order_id, a.carrier_haulier_id) for a in awards] == [
        ("o1", "borax"),
        ("o2", "acme"),
    ]


def test_highest_benefit_ranks_bids_without_an_owner_bid_last():
    """Interpretation lock: an order whose owner submitted no bid has an
    undefined benefit, so its bids rank after every defined-benefit bid even
    when they are cheaper."""
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    owner_of = _owner_of({"o1": "acme", "o2": "cargo"})
    bids = [
        # o1 has an owner bid => reserve 20; the partner bid at 19 has benefit 1
        _bid("acme", "o1", "t_x", 20.0),
        _bid("borax", "o1", "t_shared", 19.0),
        # o2's owner submitted nothing => undefined benefit, despite costing 0.5
        _bid("borax", "o2", "t_shared", 0.5),
    ]
    kwargs = dict(market=market, tick_seed=TICK_SEED, owner_of=owner_of, rng=random.Random(0))

    awards = HighestBenefitRule().resolve(bids, **kwargs)
    # the defined-benefit bid takes t_shared; the undefined one is swept last
    # and finds its truck gone, and o1's own owner bid finds o1 gone.
    assert [(a.order_id, a.truck_id, a.carrier_haulier_id) for a in awards] == [
        ("o1", "t_shared", "borax")
    ]

    # contrast: LowestCost gives t_shared to the 0.5 km bid instead.
    lowest = LowestCostRule().resolve(bids, **kwargs)
    assert [(a.order_id, a.truck_id) for a in lowest] == [("o2", "t_shared"), ("o1", "t_x")]


# --- arbitration: the two non-negotiable properties -------------------------


def _contested_bids():
    return [
        _bid("acme", "o1", "t_a1", 12.0),
        _bid("borax", "o1", "t_b1", 12.0),      # exact cost tie with the owner
        _bid("cargo", "o1", "t_c1", 7.0),
        _bid("acme", "o2", "t_a1", 3.0),        # same truck as an o1 bid
        _bid("borax", "o2", "t_b1", 3.0),
        _bid("cargo", "o3", "t_c1", 20.0),
        _bid("acme", "o3", "t_a2", 20.0),
        _bid("borax", "o4", "t_b2", 1.0),
    ]


_OWNERS = {"o1": "acme", "o2": "borax", "o3": "cargo", "o4": "borax"}


@pytest.mark.parametrize("rule_name", sorted(ARBITRATION_REGISTRY))
def test_arbitration_is_independent_of_bid_list_order(rule_name):
    """I-P5. Shuffle the bid list 100x; exactly ONE distinct award list may
    result. RandomAward gets a freshly re-seeded rng per permutation, so its
    output is a function of (bid set, rng state) and not of caller order."""
    rule = get_arbitration_rule(rule_name, {})
    bids = _contested_bids()
    owner_of = _owner_of(_OWNERS)

    seen = set()
    shuffler = random.Random(2024)
    for _ in range(100):
        perm = list(bids)
        shuffler.shuffle(perm)
        awards = rule.resolve(
            perm,
            market=_market(_pool("p:acme+borax", "acme", "borax")),
            tick_seed=TICK_SEED,
            owner_of=owner_of,
            rng=random.Random(4242),  # re-seeded per permutation
        )
        seen.add(tuple(_tuples(awards)))
    assert len(seen) == 1, f"{rule_name} produced {len(seen)} distinct award lists"
    # and it actually did something worth being deterministic about
    assert len(next(iter(seen))) > 0


@pytest.mark.parametrize("rule_name", sorted(ARBITRATION_REGISTRY))
def test_arbitration_is_independent_of_bid_list_order_exhaustively(rule_name):
    """Same property, but over EVERY permutation of a small contested set —
    100 random shuffles can miss a rare position dependence."""
    rule = get_arbitration_rule(rule_name, {})
    bids = [
        _bid("acme", "o1", "t_a", 5.0),
        _bid("borax", "o1", "t_b", 5.0),  # exact tie => only the tiebreak decides
        _bid("cargo", "o2", "t_c", 5.0),
    ]
    owner_of = _owner_of({"o1": "acme", "o2": "cargo"})
    seen = {
        tuple(
            _tuples(
                rule.resolve(
                    list(perm),
                    market=_market(_pool("p:acme+borax", "acme", "borax")),
                    tick_seed=TICK_SEED,
                    owner_of=owner_of,
                    rng=random.Random(7),
                )
            )
        )
        for perm in itertools.permutations(bids)
    }
    assert len(seen) == 1


@pytest.mark.parametrize("rule_name", sorted(ARBITRATION_REGISTRY))
def test_arbitration_never_awards_a_committed_truck_or_order(rule_name):
    """I-P2. Anything the host already committed this tick is invisible to the
    sweep: neither its order nor its truck may be awarded again."""
    rule = get_arbitration_rule(rule_name, {})
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    assert market.commit(
        Award(
            order_id="o_taken",
            truck_id="t_taken",
            carrier_haulier_id="acme",
            owner_haulier_id="acme",
            cost_km=1.0,
            pool_id=None,
            round=1,
        )
    )
    bids = [
        _bid("borax", "o_taken", "t_free", 0.1),   # order already committed
        _bid("borax", "o_free", "t_taken", 0.1),   # truck already committed
        _bid("borax", "o_free", "t_free", 9.0),    # the only awardable bid
    ]
    awards = rule.resolve(
        bids,
        market=market,
        tick_seed=TICK_SEED,
        owner_of=_owner_of({"o_taken": "acme", "o_free": "acme"}),
        rng=random.Random(1),
    )
    assert [(a.order_id, a.truck_id) for a in awards] == [("o_free", "t_free")]


@pytest.mark.parametrize("rule_name", sorted(ARBITRATION_REGISTRY))
def test_arbitration_does_not_mutate_the_market(rule_name):
    """``resolve`` reads allocation state but never commits — the host does."""
    rule = get_arbitration_rule(rule_name, {})
    market = _market(_pool("p:acme+borax", "acme", "borax"))
    market.contribute("o1", "acme", ["p:acme+borax"])

    awards = rule.resolve(
        _contested_bids(),
        market=market,
        tick_seed=TICK_SEED,
        owner_of=_owner_of(_OWNERS),
        rng=random.Random(3),
    )
    assert awards  # the rule did produce awards...
    assert market.awards == ()  # ...but committed none of them
    for order_id in ("o1", "o2", "o3", "o4"):
        assert market.is_order_free(order_id)
    for truck_id in ("t_a1", "t_a2", "t_b1", "t_b2", "t_c1"):
        assert market.is_truck_free(truck_id)
    # contribution state is untouched too
    assert market.is_contributed("o1")
    assert market.visible_pooled_order_ids("borax") == frozenset({"o1"})


@pytest.mark.parametrize("rule_name", sorted(ARBITRATION_REGISTRY))
def test_arbitration_awards_each_order_and_truck_at_most_once(rule_name):
    """I-P2, within a single resolve call."""
    rule = get_arbitration_rule(rule_name, {})
    awards = rule.resolve(
        _contested_bids(),
        market=_market(_pool("p:acme+borax", "acme", "borax")),
        tick_seed=TICK_SEED,
        owner_of=_owner_of(_OWNERS),
        rng=random.Random(5),
    )
    order_ids = [a.order_id for a in awards]
    truck_ids = [a.truck_id for a in awards]
    assert len(set(order_ids)) == len(order_ids)
    assert len(set(truck_ids)) == len(truck_ids)
    # every award traces back to a submitted bid
    submitted = {(b.order_id, b.truck_id, b.haulier_id) for b in _contested_bids()}
    assert all((a.order_id, a.truck_id, a.carrier_haulier_id) in submitted for a in awards)


def test_all_rules_subclass_the_base_and_accept_params():
    for name, cls in ARBITRATION_REGISTRY.items():
        assert issubclass(cls, BaseArbitrationRule), name
        assert cls({"x": 1}).params == {"x": 1}


# --- bid_tiebreak ------------------------------------------------------------


def test_bid_tiebreak_is_stable_and_seed_dependent():
    bid = _bid("acme", "o1", "t1", 5.0)
    assert bid_tiebreak(1, bid) == bid_tiebreak(1, bid)
    assert 0 <= bid_tiebreak(1, bid) < 2 ** 64
    assert bid_tiebreak(1, bid) != bid_tiebreak(2, bid)
    # independent of cost_km / pool_id / round — identity only
    other = Bid(haulier_id="acme", order_id="o1", truck_id="t1", cost_km=99.0,
                pool_id="other", round=7)
    assert bid_tiebreak(1, bid) == bid_tiebreak(1, other)
    # and it separates the bidders
    assert bid_tiebreak(1, bid) != bid_tiebreak(1, _bid("borax", "o1", "t1", 5.0))
