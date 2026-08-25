"""Convergence-terminated pooled market + round provenance (plan §13.4 FIX-1, FIX-7).

Guards the CRITICAL confound the review found (F1): with ``max_rounds`` used as a
termination condition rather than a safety backstop, the cooperating arm served
systematically FEWER orders than its own zero-pool control, and the deficit grew
with the number of cooperating companies. Because serving fewer orders *improves*
mean deadhead (``solver_boundary_audit.md`` P3), that handed the cooperating arm a
spurious advantage on the headline metric — a false positive whose effect size
scales with the treatment.
"""

import pytest

from apps.container_logistics.assignment import pooled_planner as PP
from apps.container_logistics.assignment.pooled_planner import (
    DEFAULT_MAX_ROUNDS,
    MAX_ROUNDS_CEILING,
    effective_cooperation_stamp,
    resolve_market_config,
)
from apps.container_logistics.datagen import hauliers as H
from apps.container_logistics.datagen.preprocess import (
    DEFAULT_MARKET,
    MARKET_MAX_ROUNDS_MAX,
    MARKET_MAX_ROUNDS_MIN,
)

from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app, _ids

_HAULIERS = ["acme", "borax", "cargo", "delta", "echo"]


def _clique_fixture(companies: int, trucks_each: int, orders_each: int):
    """N companies in a full clique, fleets and demand interleaved so every
    company's planner wants the same popular orders — which is what forces
    multiple bidding rounds (round 1 is mostly collisions)."""
    hs = _HAULIERS[:companies]
    trucks, orders = [], []
    for i, h in enumerate(hs):
        for j in range(trucks_each):
            trucks.append(_truck(f"t_{h}_{j}", h, 103.80 + 0.004 * (i * trucks_each + j), 1.30))
        for j in range(orders_each):
            orders.append(_order(f"o_{h}_{j}", h, 103.85 + 0.003 * (i * orders_each + j), 1.30))
    edges = [[a, b] for i, a in enumerate(hs) for b in hs[i + 1:]]
    return trucks, orders, _coop(edges), _coop([])


# --- THE regression guard for the F1 confound --------------------------------

@pytest.mark.parametrize("companies,trucks_each,orders_each", [(3, 3, 3), (5, 4, 4)])
def test_cooperating_arm_serves_no_fewer_than_its_zero_pool_control(
    companies, trucks_each, orders_each
):
    """The test whose absence allowed F1 to ship.

    Identical input, identical seed, DEFAULT configuration: the cooperating
    (clique) arm must award ``>=`` the zero-pool control. Cooperation may not cost
    throughput, because a throughput deficit correlated with the treatment
    silently flatters the headline deadhead metric.

    Verified to have teeth: at the old default ``max_rounds=2`` this fails with
    6 vs 9 awards at 3 companies and 12 vs 20 at 5 companies — and note the
    deficit GROWS with company count, which is the confound's signature.
    """
    trucks, orders, clique, nocoop = _clique_fixture(companies, trucks_each, orders_each)

    coop_awards = _app(trucks, orders, cooperation=clique).assign("2020-01-01 08:00:00")
    ctrl_awards = _app(trucks, orders, cooperation=nocoop).assign("2020-01-01 08:00:00")

    assert len(coop_awards) >= len(ctrl_awards), (
        f"THROUGHPUT CONFOUND: cooperating arm served {len(coop_awards)} vs "
        f"zero-pool control {len(ctrl_awards)}. A treatment-correlated throughput "
        f"deficit flatters the empty-ratio metric (audit P3)."
    )


def test_the_confound_guard_would_catch_a_truncating_backstop():
    """Negative control for the test above: pin the backstop back to the old
    default of 2 and the deficit reappears, so the guard is not vacuous."""
    trucks, orders, clique, nocoop = _clique_fixture(5, 4, 4)
    coop = _app(trucks, orders, cooperation=clique, market={"max_rounds": 2}).assign(
        "2020-01-01 08:00:00"
    )
    ctrl = _app(trucks, orders, cooperation=nocoop, market={"max_rounds": 2}).assign(
        "2020-01-01 08:00:00"
    )
    assert len(coop) < len(ctrl), (
        "expected the old max_rounds=2 to under-serve the cooperating arm; if this "
        "no longer holds the guard above has lost its teeth"
    )


# --- convergence -------------------------------------------------------------

def test_award_set_is_stable_past_convergence():
    """Once converged, raising the backstop changes nothing — which is what makes
    it a backstop rather than a tuning knob."""
    trucks, orders, clique, _ = _clique_fixture(3, 3, 3)

    app_k = _app(trucks, orders, cooperation=clique, market={"max_rounds": 5})
    at_k = _ids(app_k.assign("2020-01-01 08:00:00"))
    app_k5 = _app(trucks, orders, cooperation=clique, market={"max_rounds": 10})
    at_k5 = _ids(app_k5.assign("2020-01-01 08:00:00"))

    assert at_k == at_k5
    assert app_k.round_stats()["ticks_truncated_by_backstop"] == 0
    assert app_k5.round_stats()["ticks_truncated_by_backstop"] == 0
    # and this fixture genuinely needs more than the old default of 2
    assert app_k.round_stats()["rounds_max"] >= 3


def test_backstop_binding_is_recorded():
    """If the backstop ever binds, the run must say so itself."""
    trucks, orders, clique, _ = _clique_fixture(3, 3, 3)
    app = _app(trucks, orders, cooperation=clique, market={"max_rounds": 1})
    app.assign("2020-01-01 08:00:00")

    stats = app.round_stats()
    assert stats["ticks_truncated_by_backstop"] == 1
    assert stats["rounds_max"] == 1

    converged = _app(trucks, orders, cooperation=clique, market={"max_rounds": 20})
    converged.assign("2020-01-01 08:00:00")
    assert converged.round_stats()["ticks_truncated_by_backstop"] == 0


def test_round_stats_record_the_configured_backstop():
    """The run must record WHICH backstop was in force, not just whether it bound —
    otherwise `ticks_truncated_by_backstop == 0` is uninterpretable (0 because the
    market converged, or 0 because the cap was enormous?)."""
    trucks, orders, clique, _ = _clique_fixture(3, 3, 3)
    app = _app(trucks, orders, cooperation=clique, market={"max_rounds": 7})
    app.assign("2020-01-01 08:00:00")
    assert app.round_stats()["max_rounds_backstop"] == 7

    default_app = _app(trucks, orders, cooperation=clique)
    default_app.assign("2020-01-01 08:00:00")
    assert default_app.round_stats()["max_rounds_backstop"] == DEFAULT_MAX_ROUNDS


def test_round_stats_accumulate_across_ticks():
    trucks, orders, clique, _ = _clique_fixture(3, 3, 3)
    app = _app(trucks, orders, cooperation=clique)
    for step in range(3):
        app.assign("2020-01-01 08:00:00", time_step=step)
    stats = app.round_stats()
    assert stats["ticks"] == 3
    assert stats["rounds_min"] is not None and stats["rounds_max"] is not None
    assert stats["rounds_min"] <= stats["rounds_mean"] <= stats["rounds_max"]
    assert stats["ticks_truncated_by_backstop"] == 0


def test_zero_pool_path_converges_in_one_round():
    """No pools => every order is private => all direct awards in round 1, and the
    market converges immediately. Guards against the backstop being spent on the
    control arm."""
    trucks, orders, _, nocoop = _clique_fixture(3, 3, 3)
    app = _app(trucks, orders, cooperation=nocoop)
    app.assign("2020-01-01 08:00:00")
    assert app.round_stats()["rounds_max"] == 1
    assert app.round_stats()["ticks_truncated_by_backstop"] == 0


# --- the constant is a backstop, and the two modules agree -------------------

def test_max_rounds_default_and_ceiling_are_in_sync_across_modules():
    """datagen cannot import assignment, so the constant exists twice. They must
    never drift — a drift would silently re-create the F1 confound."""
    assert DEFAULT_MARKET["max_rounds"] == DEFAULT_MAX_ROUNDS == 20
    assert MARKET_MAX_ROUNDS_MAX == MAX_ROUNDS_CEILING == 100
    assert MARKET_MAX_ROUNDS_MIN == 1


def test_runtime_clamps_into_the_backstop_range():
    assert resolve_market_config({"market": {"max_rounds": 0}})["max_rounds"] == 1
    assert resolve_market_config({"market": {"max_rounds": 10_000}})["max_rounds"] == 100
    assert resolve_market_config({"market": {"max_rounds": "nope"}})["max_rounds"] == 20
    assert resolve_market_config(None)["max_rounds"] == 20


# --- FIX-7: the run stamp describes what actually runs -----------------------

def test_run_stamp_records_effective_pools_for_an_edges_only_bundle():
    """F7: every shipped bundle is edges-only with no compiled 'pools' key, yet the
    runtime derives a full market from those edges. The stamp must record what RAN,
    not the absent key — the old code stamped [] for a run that had two pools."""
    coop = H.normalize_cooperation(
        {"active": "chain", "structures": [{"id": "chain", "edges": [["acme", "borax"], ["borax", "cargo"]]}]},
        ["acme", "borax", "cargo"],
    )
    active = [s for s in coop["structures"] if s["id"] == "chain"][0]
    # Simulate an OLD compiled bundle: no 'pools' key at all, no 'market' block.
    legacy_active = {k: v for k, v in active.items() if k != "pools"}
    assert "pools" not in legacy_active

    stamp = effective_cooperation_stamp({"planner": {"topology": "pooled"}}, legacy_active)

    assert stamp["pools"] == [
        {"id": "p:acme+borax", "members": ["acme", "borax"]},
        {"id": "p:borax+cargo", "members": ["borax", "cargo"]},
    ], "stamp must record the EFFECTIVE derived pools, not the absent compiled key"
    assert stamp["topology"] == "pooled"
    assert stamp["structure_id"] == "chain"
    assert stamp["adjacency"] == active["adjacency"]


def test_run_stamp_records_the_resolved_market_block_with_defaults_applied():
    """No compiled bundle carries a market block, so run_config could not record
    which rules ran. The stamp resolves the defaults explicitly."""
    stamp = effective_cooperation_stamp({"planner": {"topology": "pooled"}}, {"id": "x"})
    market = stamp["market"]
    assert market["offer"]["type"] == "OfferAll"
    assert market["claim"]["type"] == "ClaimAllPlanned"
    assert market["arbitration"]["type"] == "LowestCost"
    assert market["max_rounds"] == DEFAULT_MAX_ROUNDS


def test_run_stamp_reflects_an_override_rather_than_the_compiled_default():
    stamp = effective_cooperation_stamp(
        {"planner": {"topology": "two-stage",
                     "market": {"arbitration": {"type": "OwnerFirst"}, "max_rounds": 7}}},
        {"id": "x"},
    )
    assert stamp["topology"] == "pooled"  # deprecated alias resolved
    assert stamp["market"]["arbitration"]["type"] == "OwnerFirst"
    assert stamp["market"]["max_rounds"] == 7


def test_stamp_market_matches_what_the_app_actually_instantiates():
    """The stamp and the runtime resolve through the SAME function, so they cannot
    disagree. This asserts the wiring, not just the helper."""
    profile = {"planner": {"topology": "pooled",
                           "market": {"offer": {"type": "OfferNone"}, "max_rounds": 6}}}
    app = _app([], [], cooperation=_coop([]))
    offer, _claim, _arb, max_rounds = app._market_components(profile)
    stamp = effective_cooperation_stamp(profile, {"id": "x"})
    assert max_rounds == stamp["market"]["max_rounds"] == 6
    assert type(offer).__name__.startswith("OfferNone")
    assert stamp["market"]["offer"]["type"] == "OfferNone"


# --- R3-7: private commits count as progress (plan §14.5, review MEDIUM-10) ---

def _all_private_market(**extra):
    """OfferSpare with `only_when_short=False` and the default `keep_below_km=inf`
    keeps every servable order PRIVATE — the regime experiment E6 runs in."""
    return {"offer": {"type": "OfferSpare", "params": {"only_when_short": False}},
            "max_rounds": 20, **extra}


def _private_fixture():
    trucks = [_truck("t_a1", "acme", 103.851, 1.30), _truck("t_a2", "acme", 103.852, 1.30),
              _truck("t_b1", "borax", 103.951, 1.30), _truck("t_b2", "borax", 103.952, 1.30)]
    orders = [_order("o_a1", "acme", 103.850, 1.30), _order("o_a2", "acme", 103.853, 1.30),
              _order("o_b1", "borax", 103.950, 1.30), _order("o_b2", "borax", 103.953, 1.30)]
    return trucks, orders, _coop([["acme", "borax"]])


def _app_private(trucks, orders, coop):
    """One truck per company per round (the non-spatial sample cap), so round 1
    cannot finish the work and a premature break is observable as lost awards."""
    app = _app(trucks, orders, cooperation=coop, market=_all_private_market())
    app.behavior["profile"]["use_spatial_matching"] = False
    app.behavior["profile"]["max_trucks_per_haulier"] = 1
    return app


def test_offer_spare_mixed_round_does_not_declare_convergence():
    """A round that commits PRIVATELY but produces no bids has made progress and
    must not end the auction.

    §4.1 step 5.5 said "break if no bids were produced this round", which ignores
    the private-commit path defined two steps earlier — so the auction stopped while
    work remained. Latent under the default `OfferAll` (nothing stays private), live
    under `OfferSpare`.

    Verified to have teeth: restoring `if not round_bids:` drops this to 2 awards.
    """
    trucks, orders, coop = _private_fixture()
    app = _app_private(trucks, orders, coop)
    matches = app.assign("2020-01-01 08:00:00")

    assert len(matches) == 4, (
        f"premature convergence: {len(matches)} awards, expected 4 — a round that "
        f"committed privately but bid nothing ended the auction"
    )
    assert app.round_stats()["rounds_max"] >= 2, "the fixture no longer needs a second round"
    assert app.round_stats()["ticks_truncated_by_backstop"] == 0


def test_a_round_that_commits_nothing_at_all_still_converges():
    """The terminator must still fire — R3-7 must not turn convergence into a
    max_rounds spin."""
    trucks, orders, coop = _private_fixture()
    app = _app_private(trucks, orders, coop)
    app.assign("2020-01-01 08:00:00")
    stats = app.round_stats()
    assert stats["ticks_truncated_by_backstop"] == 0, "the auction ran to the backstop"
    assert stats["rounds_max"] < 20


# --- R3-5 / R3-6: the provenance stamp must be complete and undiluted ---------

def test_rounds_mean_excludes_idle_ticks():
    """A tick where the market opened with nothing free to allocate is not an
    auction of depth zero — averaging it in diluted the figure quoted as auction
    depth (review MEDIUM-9)."""
    trucks, orders, clique, _ = _clique_fixture(3, 3, 3)
    app = _app(trucks, orders, cooperation=clique)
    app.assign("2020-01-01 08:00:00", time_step=0)          # real auction
    # Every order is now committed in the market's eyes only within a tick, so a
    # second tick over an EMPTY order list is the idle case.
    app.manager._orders = []
    app.assign("2020-01-01 08:00:00", time_step=1)          # idle

    stats = app.round_stats()
    assert stats["ticks"] == 2
    assert stats["ticks_idle"] == 1
    assert stats["auction_ticks"] == 1
    assert stats["rounds_mean"] == stats["rounds_max"], (
        f"idle tick diluted rounds_mean: {stats}"
    )


def test_ticks_are_split_into_horizon_and_drain():
    """`ticks` must sit on the same horizon as the KPI block beside it (R3-5)."""
    trucks, orders, clique, _ = _clique_fixture(3, 3, 3)
    app = _app(trucks, orders, cooperation=clique)
    app._sim_horizon_steps = 10
    app.assign("2020-01-01 08:00:00", time_step=5)    # in horizon
    app.assign("2020-01-01 08:00:00", time_step=12)   # post-horizon drain

    stats = app.round_stats()
    assert stats["ticks"] == 2
    assert stats["ticks_in_horizon"] == 1
    assert stats["ticks_drain"] == 1


def test_market_stamp_is_flushed_at_close():
    """Without a final flush the stamp is whatever the last scheduled push caught —
    the review measured `ticks: 15` against a true 17."""
    trucks, orders, clique, _ = _clique_fixture(3, 3, 3)
    app = _app(trucks, orders, cooperation=clique)
    app.assign("2020-01-01 08:00:00", time_step=0)

    flushed = []
    app._patch_round_stats_to_run_config = lambda force=False: flushed.append(force) or True
    app.close("2020-01-01 09:00:00")
    assert flushed == [True], "close() did not force a final provenance flush"
