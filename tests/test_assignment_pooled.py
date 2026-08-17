"""Pooled-topology planner host (shared-pool plan Phase 4).

Exercises the REAL ``AssignmentApp.assign`` on the ``pooled`` topology with a stub
manager, reusing the fixtures from ``test_assignment_cooperation.py`` (which must
keep passing UNMODIFIED — it is the regression guard for the legacy path).

Invariants covered here: I-P1, I-P2, I-P4, I-P5, I-P7.
"""

import logging
import random

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.assignment.policy.claim import (
    CLAIM_REGISTRY,
    ClaimAllPlannedPolicy,
)
from apps.container_logistics.assignment.pools import Bid, PoolMarket
from apps.container_logistics.assignment.solver import get_solver

from tests.test_assignment_cooperation import (
    _StubManager,
    _StubMessenger,
    _coop,
    _order,
    _truck,
)


def _app(
    trucks,
    orders,
    *,
    cooperation=None,
    topology="pooled",
    strategy="GreedyNearest",
    market=None,
):
    """A real AssignmentApp on a chosen topology (no ORSim lifecycle)."""
    app = AssignmentApp.__new__(AssignmentApp)
    planner = {"topology": topology, "timing": {"mode": "online"}}
    if market is not None:
        planner["market"] = market
    profile = {
        "strategy": strategy,
        "respect_truck_online_state": True,
        "reject_if_active_haul_trip": True,
        "use_spatial_matching": True,
        "planner": planner,
    }
    if cooperation is not None:
        profile["cooperation"] = cooperation
    app.behavior = {"profile": profile}
    app.run_id = "run_test"
    app.manager = _StubManager(trucks, orders)
    app.messenger = _StubMessenger()
    app._solver = get_solver(strategy, None)
    app._solver_params = {}
    app._warned_haulier_block = False
    app._share_tags = {}
    return app


def _ids(matches):
    return sorted((t["_id"], o["_id"]) for t, o in matches)


# Geometry: o_acme at 103.85, o_borax ~11 km east at 103.95.
def _two_company_fleet():
    trucks = [
        _truck("t_acme", "acme", 103.86, 1.30),    # ~1.1 km to o_acme, ~10 km to o_borax
        _truck("t_borax", "borax", 103.855, 1.30),  # ~0.55 km to o_acme (cheapest)
    ]
    orders = [
        _order("o_acme", "acme", 103.85, 1.30),
        _order("o_borax", "borax", 103.95, 1.30),
    ]
    return trucks, orders


# --- I-P7: zero pools == today ----------------------------------------------

def test_zero_pools_matches_partitioned_output():
    """A structure with no edges yields no pools, so pooled must reproduce the
    partitioned path exactly (I-P7). Costs are distinct, so no tie-break luck is
    involved and the comparison is meaningful."""
    trucks = [
        _truck("t_acme", "acme", 103.86, 1.30),
        _truck("t_borax", "borax", 103.955, 1.30),
        _truck("t_cargo", "cargo", 103.90, 1.31),
    ]
    orders = [
        _order("o_acme", "acme", 103.85, 1.30),
        _order("o_borax", "borax", 103.95, 1.30),
        _order("o_cargo", "cargo", 103.91, 1.31),
    ]
    no_edges = _coop([])

    random.seed(7)
    part = _app(trucks, orders, cooperation=no_edges, topology="partitioned").assign(
        "2020-01-01 08:00:00"
    )
    random.seed(7)
    pooled_app = _app(trucks, orders, cooperation=no_edges, topology="pooled")
    pooled = pooled_app.assign("2020-01-01 08:00:00")

    assert _ids(pooled) == _ids(part)
    assert len(pooled) == 3
    assert pooled_app._share_tags == {}  # no pools => never a share


def test_no_cooperation_block_at_all_runs_pooled_as_own_haulier_only():
    """A profile with no cooperation block is zero-pool by construction."""
    trucks = [_truck("t_acme", "acme", 103.86, 1.30), _truck("t_cargo", "cargo", 103.8505, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders)  # cooperation=None
    assert _ids(app.assign("2020-01-01 08:00:00")) == [("t_acme", "o_acme")]
    assert app._share_tags == {}


# --- contested pooled orders -------------------------------------------------

def test_partner_truck_wins_contested_pooled_order():
    """Pooled analogue of test_partner_truck_wins_on_cost_while_owner_truck_free:
    the cheaper partner truck carries the owner's job even though the owner has a
    free feasible truck."""
    trucks = [
        _truck("t_acme", "acme", 103.85 + 0.10, 1.30),    # ~11 km
        _truck("t_borax", "borax", 103.85 + 0.01, 1.30),  # ~1.1 km, cheapest
        _truck("t_cargo", "cargo", 103.85 + 0.005, 1.30),  # nearer still, NO pool
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    assert _ids(matches) == [("t_borax", "o_acme")]
    tag = app._share_tags[("t_borax", "o_acme")]
    assert tag["shared"] is True
    assert tag["owner_haulier_id"] == "acme" and tag["carrier_haulier_id"] == "borax"
    assert tag["pool_id"] == "p:acme+borax"
    assert tag["market_round"] == 1
    assert tag["benefit_km"] == pytest.approx(10.0, abs=0.5)


def test_owner_can_claim_its_own_contributed_order_and_is_not_tagged_shared():
    """A self-claim is NOT a share: the owner wins its own contributed order, so
    the gains ledger stays identical to today (plan §D6)."""
    trucks = [
        _truck("t_acme", "acme", 103.851, 1.30),   # own truck nearest
        _truck("t_borax", "borax", 103.95, 1.30),  # partner far
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    assert _ids(matches) == [("t_acme", "o_acme")]
    assert app._share_tags == {}


def test_chain_pools_are_not_transitive():
    """Pooled analogue of the edge-direct test: pools {A,B} and {B,C} mean A and C
    never see each other, so cargo's nearer truck can never take acme's order."""
    trucks = [
        _truck("t_acme", "acme", 103.85 + 0.10, 1.30),
        _truck("t_cargo", "cargo", 103.85 + 0.005, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"], ["borax", "cargo"]]))
    matches = app.assign("2020-01-01 08:00:00")

    assert _ids(matches) == [("t_acme", "o_acme")]
    assert app._share_tags == {}


# --- I-P1 / I-P4: nothing lost, nothing invisible awarded --------------------

def test_unclaimed_pooled_order_stays_unassigned_and_owned():
    """ClaimNone: every order is contributed but nobody bids. Nothing is awarded,
    nothing is lost, and ownership is untouched (I-P1, I-P3, I-P4)."""
    trucks, orders = _two_company_fleet()
    before = [dict(o) for o in orders]
    app = _app(
        trucks,
        orders,
        cooperation=_coop([["acme", "borax"]]),
        market={"claim": {"type": "ClaimNone", "params": {}}, "max_rounds": 2},
    )
    matches = app.assign("2020-01-01 08:00:00")

    assert matches == []
    assert app._share_tags == {}
    # ownership immutable, order docs untouched
    assert [dict(o) for o in orders] == before
    assert orders[0]["haulier_id"] == "acme" and orders[1]["haulier_id"] == "borax"


class _FabricatingClaimPolicy(ClaimAllPlannedPolicy):
    """A deliberately MISBEHAVING claim policy, used only by the test below.

    Behaves exactly like the default for everyone, except that ``cargo`` also
    fabricates a bid for ``o_acme`` — an order owned by ``acme``, which ``cargo``
    shares no pool with and therefore cannot see. It bids at 0.0 km so that a
    cost-ranked arbitration rule is guaranteed to hand it the award, which is what
    forces the commit-time guard to be the thing that stops it.
    """

    FABRICATED_ORDER_ID = "o_acme"
    FABRICATED_TRUCK_ID = "t_cargo2"

    def bids(self, *, haulier_id, pooled_pairs, own_pairs, cost, market, rng, round):
        out = super().bids(
            haulier_id=haulier_id, pooled_pairs=pooled_pairs, own_pairs=own_pairs,
            cost=cost, market=market, rng=rng, round=round,
        )
        if haulier_id == "cargo":
            out.append(
                Bid(
                    haulier_id="cargo",
                    order_id=self.FABRICATED_ORDER_ID,
                    truck_id=self.FABRICATED_TRUCK_ID,
                    cost_km=0.0,
                    # A pool cargo is NOT a member of. The host must not trust this
                    # field — it re-derives visibility from the market itself.
                    pool_id="p:acme+borax",
                    round=round,
                )
            )
        return out


def test_commit_time_ip4_guard_blocks_a_fabricated_bid(caplog):
    """I-P4 is re-asserted AT COMMIT, not only at bid-collection time (plan §13.4
    FIX-10 / review F12).

    Mutating the ``if pool_id is None:`` guard in ``run_pooled_market`` to
    ``if False:`` SURVIVED the whole suite: no test ever drove an award for an
    order the carrier cannot see, so the defensive backstop against a misbehaving
    custom claim policy was dead code as far as the suite was concerned.

    Chain ``acme-borax`` + ``borax-cargo`` gives two pools that never put acme and
    cargo together, so a cargo bid on ``o_acme`` is unambiguously invisible-order
    theft. The policy is registered into the REAL ``CLAIM_REGISTRY`` and selected
    through the ordinary ``planner.market.claim`` config, so the whole path from
    config to commit is the production one.
    """
    trucks = [
        _truck("t_acme", "acme", 103.86, 1.30),
        _truck("t_borax", "borax", 103.99, 1.30),
        _truck("t_cargo1", "cargo", 103.70, 1.30),
        _truck("t_cargo2", "cargo", 103.701, 1.30),  # the truck the fake bid names
    ]
    orders = [
        _order("o_acme", "acme", 103.85, 1.30),
        _order("o_cargo", "cargo", 103.70, 1.30),  # gives cargo real pooled pairs,
    ]                                              # so its claim policy is invoked

    coop = _coop([["acme", "borax"], ["borax", "cargo"]])
    # Fixture guard: acme and cargo must genuinely share no pool, or the "cannot
    # see" premise evaporates and the test proves nothing.
    market_probe = PoolMarket.from_structure(
        assign_constraints.active_cooperation_structure({"cooperation": coop})
    )
    acme_pools = {p.id for p in market_probe.pools_for("acme")}
    cargo_pools = {p.id for p in market_probe.pools_for("cargo")}
    assert acme_pools and cargo_pools and not (acme_pools & cargo_pools), (
        f"fixture broken: acme={acme_pools} cargo={cargo_pools} overlap"
    )

    CLAIM_REGISTRY["_TestFabricatingClaim"] = _FabricatingClaimPolicy
    try:
        app = _app(
            trucks,
            orders,
            cooperation=coop,
            market={"claim": {"type": "_TestFabricatingClaim", "params": {}}},
        )
        with caplog.at_level(logging.ERROR, logger="apps.container_logistics.assignment.pooled_planner"):
            matches = app.assign("2020-01-01 08:00:00")
    finally:
        CLAIM_REGISTRY.pop("_TestFabricatingClaim", None)

    by_order = {o["_id"]: t["_id"] for t, o in matches}

    # THE assertion: the fabricated award is never committed.
    assert by_order.get("o_acme") != "t_cargo2", "I-P4 violated: cargo carried an invisible order"
    assert not str(by_order.get("o_acme", "")).startswith("t_cargo"), (
        f"I-P4 violated: a cargo truck carried o_acme ({by_order})"
    )
    for (truck_id, order_id), tag in app._share_tags.items():
        assert not (order_id == "o_acme" and tag["carrier_haulier_id"] == "cargo"), (
            f"I-P4 violated: share tag records cargo as carrier of o_acme ({tag})"
        )

    # The guard fired, and said so — a silent drop would be as bad as the leak.
    assert any(
        "I-P4 violation blocked" in r.getMessage() and r.levelno >= logging.ERROR
        for r in caplog.records
    ), f"the commit-time I-P4 guard did not fire: {[r.getMessage() for r in caplog.records]}"

    # ...and blocking the theft did not cost the order its legitimate service.
    assert by_order.get("o_acme") == "t_acme"
    assert by_order.get("o_cargo") in {"t_cargo1", "t_cargo2"}


def test_awarded_orders_are_always_a_subset_of_the_input_orders():
    """I-P1: the planner can never invent an order."""
    trucks, orders = _two_company_fleet()
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    assert {o["_id"] for _t, o in matches} <= {o["_id"] for o in orders}


# --- I-P2: no double assignment ---------------------------------------------

def test_no_order_or_truck_assigned_twice_across_rounds():
    trucks, orders = _two_company_fleet()
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    truck_ids = [t["_id"] for t, _o in matches]
    order_ids = [o["_id"] for _t, o in matches]
    assert len(set(truck_ids)) == len(truck_ids)
    assert len(set(order_ids)) == len(order_ids)


# --- rounds ------------------------------------------------------------------

def test_loser_truck_is_reused_in_round_two():
    """Round 2 exists to re-employ a truck freed by a LOST bid. Both companies bid
    their only truck on o_acme in round 1; borax is cheaper and wins, so acme's
    truck is idle and must pick up o_borax in round 2."""
    trucks, orders = _two_company_fleet()
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]), market={"max_rounds": 2})
    matches = app.assign("2020-01-01 08:00:00")

    assert _ids(matches) == [("t_acme", "o_borax"), ("t_borax", "o_acme")]
    # t_acme lost round 1 and was re-employed in round 2
    tag = app._share_tags[("t_acme", "o_borax")]
    assert tag["market_round"] == 2
    assert tag["owner_haulier_id"] == "borax" and tag["carrier_haulier_id"] == "acme"


def test_max_rounds_one_terminates_and_loses_nothing():
    """With max_rounds=1 the loser's truck is not re-employed this tick, but the
    unserved order is NOT lost — it is simply still unassigned and retried next
    tick (the loop is online)."""
    trucks, orders = _two_company_fleet()
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]), market={"max_rounds": 1})
    matches = app.assign("2020-01-01 08:00:00")

    assert _ids(matches) == [("t_borax", "o_acme")]
    awarded = {o["_id"] for _t, o in matches}
    assert "o_borax" not in awarded
    # still owned, still present, untouched
    assert orders[1]["haulier_id"] == "borax"


# --- I-P5: determinism -------------------------------------------------------

def test_same_tick_seed_gives_identical_awards_under_input_shuffle():
    """I-P5: identical tick inputs + identical tick_seed => identical award list,
    regardless of the order the trucks/orders arrive in.

    SCOPE HONESTY: this is a per-tick guarantee about the PLANNER only. It does not
    make a whole run reproducible — agent scheduling is async.
    """
    trucks, orders = _two_company_fleet()
    trucks += [
        _truck("t_acme2", "acme", 103.87, 1.302),
        _truck("t_borax2", "borax", 103.94, 1.298),
    ]
    orders += [
        _order("o_acme2", "acme", 103.88, 1.301),
        _order("o_borax2", "borax", 103.93, 1.299),
    ]
    coop = _coop([["acme", "borax"]])

    baseline = None
    rnd = random.Random(1234)
    for _ in range(25):
        t = list(trucks)
        o = list(orders)
        rnd.shuffle(t)
        rnd.shuffle(o)
        # Deliberately perturb the module RNG too: the pooled path must not depend
        # on it at all (it injects its own tick-seeded Random).
        random.seed(rnd.random())
        app = _app(t, o, cooperation=coop)
        result = _ids(app.assign("2020-01-01 08:00:00", time_step=42))
        if baseline is None:
            baseline = result
        assert result == baseline, "I-P5 violated: award set depends on input order"


def test_different_time_step_rotates_the_tiebreak():
    """The tick seed rotates per tick so no company gets a systematic edge from a
    fixed hash. Same inputs, different time_step => a legitimately different seed."""
    assert AssignmentApp._tick_seed("run_x", 1) != AssignmentApp._tick_seed("run_x", 2)
    assert AssignmentApp._tick_seed("run_x", 1) == AssignmentApp._tick_seed("run_x", 1)
    assert AssignmentApp._tick_seed("run_y", 1) != AssignmentApp._tick_seed("run_x", 1)


# --- legacy path guards ------------------------------------------------------

def test_solver_rng_defaults_to_module_random():
    """Guards that the partitioned path is unchanged: with no set_rng call the
    solver's rng IS the random module, so behaviour is byte-identical to before."""
    solver = get_solver("GreedyNearest", None)
    assert solver.rng is random
    solver.set_rng(random.Random(1))
    assert solver.rng is not random
    solver.set_rng(None)
    assert solver.rng is random


def test_isolated_company_in_a_pooled_run_still_serves_its_own_orders():
    """MIXED structure: acme+borax share a pool, cargo is in none. Cargo's orders
    are PRIVATE by construction (it belongs to no pool, so it contributes nowhere),
    which means they take the direct-award path and must still be served — a company
    outside the market must never be starved by it."""
    trucks = [
        _truck("t_acme", "acme", 103.86, 1.30),
        _truck("t_borax", "borax", 103.855, 1.30),
        _truck("t_cargo", "cargo", 103.70, 1.30),
    ]
    orders = [
        _order("o_acme", "acme", 103.85, 1.30),
        _order("o_cargo", "cargo", 103.71, 1.30),
    ]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    by_order = {o["_id"]: t["_id"] for t, o in matches}
    assert by_order["o_cargo"] == "t_cargo", "isolated company was starved by the market"
    assert "o_acme" in by_order
    # cargo never appears in a share tag: it is in no pool
    for tag in app._share_tags.values():
        assert "cargo" not in (tag["owner_haulier_id"], tag["carrier_haulier_id"])


def test_a_truck_taken_by_a_direct_award_cannot_also_win_a_pooled_bid():
    """I-P2 across the two award paths: direct awards commit immediately, so the
    arbitration sweep must see that truck as already taken."""
    trucks = [
        _truck("t_acme", "acme", 103.86, 1.30),
        _truck("t_borax", "borax", 103.855, 1.30),
        _truck("t_cargo", "cargo", 103.70, 1.30),
    ]
    orders = [
        _order("o_acme", "acme", 103.85, 1.30),
        _order("o_borax", "borax", 103.95, 1.30),
        _order("o_cargo", "cargo", 103.71, 1.30),
    ]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    truck_ids = [t["_id"] for t, _o in matches]
    order_ids = [o["_id"] for _t, o in matches]
    assert len(set(truck_ids)) == len(truck_ids), "a truck was awarded twice"
    assert len(set(order_ids)) == len(order_ids), "an order was awarded twice"


def test_offer_none_reproduces_no_cooperation_under_pooled():
    """The OfferNone control: nothing is contributed, so every order is private and
    the pooled path degenerates to own-haulier-only — the same allocation the
    zero-pool fast path produces."""
    trucks = [
        _truck("t_acme", "acme", 103.85 + 0.10, 1.30),
        _truck("t_borax", "borax", 103.85 + 0.01, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(
        trucks,
        orders,
        cooperation=_coop([["acme", "borax"]]),
        market={"offer": {"type": "OfferNone", "params": {}}},
    )
    matches = app.assign("2020-01-01 08:00:00")
    assert _ids(matches) == [("t_acme", "o_acme")]
    assert app._share_tags == {}


def test_unknown_policy_names_are_fail_soft_and_still_assign():
    """A typo in a policy name must DEGRADE a run (fall back to the default), never
    abort the assignment agent mid-run (plan §7)."""
    trucks = [
        _truck("t_acme", "acme", 103.85 + 0.10, 1.30),
        _truck("t_borax", "borax", 103.85 + 0.01, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(
        trucks,
        orders,
        cooperation=_coop([["acme", "borax"]]),
        market={
            "offer": {"type": "NopePolicy", "params": {}},
            "claim": {"type": "AlsoNope", "params": {}},
            "arbitration": {"type": "StillNope", "params": {}},
        },
    )
    matches = app.assign("2020-01-01 08:00:00")
    assert _ids(matches) == [("t_borax", "o_acme")]  # defaults applied


def test_two_stage_topology_is_an_alias_for_pooled():
    prof = {"planner": {"topology": "two-stage"}}
    assert AssignmentApp._resolved_topology(prof) == "pooled"


def test_unknown_or_missing_topology_falls_back_to_partitioned():
    assert AssignmentApp._resolved_topology({}) == "partitioned"
    assert AssignmentApp._resolved_topology({"planner": {}}) == "partitioned"
    assert AssignmentApp._resolved_topology({"planner": {"topology": "nonsense"}}) == "partitioned"
