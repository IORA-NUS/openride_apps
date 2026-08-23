from pathlib import Path
"""I-P5 order-independence with EXACT COST TIES, and non-finite discipline.

Plan §13.4 FIX-3 (review F3) and FIX-5 (review F6 + F10).

F3: seeding the solver's shuffle gives *reproducibility for a fixed input order*,
not *independence of input order* — which is what I-P5 actually claims. ``scored``'s
pre-shuffle order is a function of the caller's list order and the following sort is
stable, so equal-cost pairs were broken differently per permutation. The reviewer
measured **10 distinct award sets over 200 permutations at one tick seed**, with the
award COUNT varying between 3 and 4.

And the tie case is the normal case, not a corner: ``assignment_cost`` returns
``max(0.0, base_km - dual_cycle_bonus_km)``, so with the shipped 5 km bonus every
dual-cycle-eligible pair within 5 km scores exactly ``0.000`` (audit P5).
"""

import json
import math
import random

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.assignment.arbitration import (
    HighestBenefitRule,
    LowestCostRule,
    OwnerFirstRule,
    RandomAwardRule,
)
from apps.container_logistics.assignment.pools import Bid, PoolMarket
from apps.container_logistics.assignment.solver import get_solver

from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app, _ids

# One facility; every truck's last drop-off is AT the pickup, so every pair is
# dual-cycle eligible with base_km ~ 0 => cost clamps to exactly 0.0.
_LON, _LAT = 103.85, 1.30


def _tied_truck(tid, haulier):
    t = _truck(tid, haulier, _LON, _LAT)
    t["profile"]["last_dropoff_loc"] = {"type": "Point", "coordinates": [_LON, _LAT]}
    return t


def _tied_fleet():
    trucks = [
        _tied_truck("t_acme1", "acme"), _tied_truck("t_acme2", "acme"),
        _tied_truck("t_borax1", "borax"), _tied_truck("t_borax2", "borax"),
    ]
    orders = [
        _order("o_acme1", "acme", _LON, _LAT), _order("o_acme2", "acme", _LON, _LAT),
        _order("o_borax1", "borax", _LON, _LAT), _order("o_borax2", "borax", _LON, _LAT),
    ]
    return trucks, orders


def test_the_tied_fixture_really_is_all_ties():
    """Guards the fixture itself: if geometry ever stops producing exact ties, the
    determinism test below would silently become vacuous."""
    trucks, orders = _tied_fleet()
    costs = {C.assignment_cost(t, o, {}) for t in trucks for o in orders}
    assert costs == {0.0}, f"fixture no longer produces exact ties: {sorted(costs)}"


def test_all_ties_fixture_gives_one_award_set_over_200_permutations():
    """I-P5 under the condition that actually breaks it (review F3).

    Verified to have teeth: with ``set_tiebreak`` neutered this fails immediately,
    producing many distinct award sets and a varying award count.
    """
    trucks, orders = _tied_fleet()
    coop = _coop([["acme", "borax"]])

    seen = set()
    counts = set()
    rnd = random.Random(20260817)
    for _ in range(200):
        t, o = list(trucks), list(orders)
        rnd.shuffle(t)
        rnd.shuffle(o)
        # Perturb the module RNG too: the pooled path must not consult it.
        random.seed(rnd.random())
        result = _app(t, o, cooperation=coop).assign("2020-01-01 08:00:00", time_step=42)
        seen.add(tuple(_ids(result)))
        counts.add(len(result))

    assert len(seen) == 1, (
        f"I-P5 VIOLATED: {len(seen)} distinct award sets over 200 permutations of the "
        f"same input at the same tick seed"
    )
    assert len(counts) == 1, f"award COUNT varied across permutations: {sorted(counts)}"


def test_tied_awards_are_pythonhashseed_independent():
    """The award set must not depend on PYTHONHASHSEED either — that is what makes
    the `sorted()` discipline in the host observable (review F9). Runs the tied
    fixture in subprocesses under several hash seeds."""
    import os
    import subprocess
    import sys

    script = (
        "import random, json, sys;"
        "sys.path.insert(0, '/home/user/openride_apps');"
        "from tests.test_assignment_pooled_determinism import _tied_fleet;"
        "from tests.test_assignment_cooperation import _coop;"
        "from tests.test_assignment_pooled import _app, _ids;"
        "t, o = _tied_fleet();"
        "r = _app(t, o, cooperation=_coop([['acme','borax']])).assign('2020-01-01 08:00:00', time_step=42);"
        "print(json.dumps(_ids(r)))"
    )
    results = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert out.returncode == 0, f"subprocess failed (PYTHONHASHSEED={seed}): {out.stderr[-2000:]}"
        results.add(out.stdout.strip())
    assert len(results) == 1, f"award set varies with PYTHONHASHSEED: {results}"


def test_partitioned_tiebreak_unset_matches_module_shuffle():
    """The regression guard that the LEGACY path did not move: with no tiebreak
    seed set, the solver still uses the module RNG shuffle exactly as before."""
    solver = get_solver("GreedyNearest", None)
    assert solver.tiebreak_seed is None
    assert solver.rng is random

    # Same module seed => same result, which is the pre-existing contract.
    trucks, orders = _tied_fleet()
    coop = _coop([])
    random.seed(11)
    a = _ids(_app(trucks, orders, cooperation=coop, topology="partitioned").assign("2020-01-01 08:00:00"))
    random.seed(11)
    b = _ids(_app(trucks, orders, cooperation=coop, topology="partitioned").assign("2020-01-01 08:00:00"))
    assert a == b


# --- FIX-5: no non-finite number crosses a framework boundary ----------------

def test_non_finite_cost_never_reaches_the_wire():
    """The reviewer's F6 setup, promoted to a test: an unknown-cost (inf) award
    must not emit ``Infinity``, which is not valid RFC 8259 and is a SyntaxError
    for every JS consumer (KafkaJS, the Next.js SSE hub). Python's json.loads
    tolerates it, which is exactly why this stayed invisible."""
    truck = _truck("t_borax", "borax", 103.86, 1.30)
    order = _order("o_acme", "acme", 103.85, 1.30)
    order["pickup_loc"] = None          # no geometry => assignment_cost == inf
    order["profile"].pop("pickup_loc", None)

    app = _app([truck], [order], cooperation=_coop([["acme", "borax"]]))
    app.behavior["profile"]["use_spatial_matching"] = False   # supported profile knob
    matches = app.assign("2020-01-01 08:00:00")
    assert matches, "expected the unknown-cost pair to still be assigned"

    tag = app._share_tags[("t_borax", "o_acme")]
    assert tag["awarded_cost_km"] is None, f"non-finite leaked into the tag: {tag}"

    app.publish(matches)
    published = app.messenger.published[0]
    raw = json.dumps(published["payload"])
    assert "Infinity" not in raw and "NaN" not in raw

    def _reject(constant):
        raise AssertionError(f"non-RFC-8259 constant on the wire: {constant}")

    json.loads(raw, parse_constant=_reject)   # strict RFC 8259


def _bid(order_id, haulier, truck_id, cost):
    return Bid(haulier_id=haulier, order_id=order_id, truck_id=truck_id,
               cost_km=cost, pool_id="p:a+b", round=1)


@pytest.mark.parametrize(
    "rule", [LowestCostRule(), OwnerFirstRule(), HighestBenefitRule(), RandomAwardRule()],
    ids=["LowestCost", "OwnerFirst", "HighestBenefit", "RandomAward"],
)
def test_arbitration_is_a_total_order_with_infinite_costs(rule):
    """F10: ``-(inf - inf)`` is NaN, and a NaN in position 1 of a rank tuple makes
    every later component unreachable — so the winner flipped with the caller's
    list order, defeating the module's own headline guarantee."""
    market = PoolMarket([])
    owners = {"o1": "a"}
    bids = [_bid("o1", "a", "t_a", math.inf), _bid("o1", "b", "t_b", math.inf)]

    results = set()
    for permutation in ([bids[0], bids[1]], [bids[1], bids[0]]):
        awards = rule.resolve(
            permutation, market=market, tick_seed=99,
            owner_of=lambda oid: owners.get(oid), rng=random.Random(5),
        )
        results.add(tuple((a.order_id, a.carrier_haulier_id) for a in awards))
    assert len(results) == 1, f"{type(rule).__name__} lost its total order on inf bids: {results}"


def _resolve_both_orders(rule, bids, owners):
    out = []
    for permutation in ([bids[0], bids[1]], [bids[1], bids[0]]):
        awards = rule.resolve(
            permutation, market=PoolMarket([]), tick_seed=99,
            owner_of=lambda oid: owners.get(oid), rng=random.Random(5),
        )
        out.append([a.truck_id for a in awards])
    return out


@pytest.mark.parametrize(
    "rule", [LowestCostRule(), HighestBenefitRule()], ids=["LowestCost", "HighestBenefit"],
)
def test_finite_bids_outrank_non_finite_ones_on_cost_ranked_rules(rule):
    """For the cost-ranked rules an unknown-cost bid must never beat a real one:
    ``inf`` means "geometry missing", and the whole point of routing it to the
    trailing unknown-cost class is that it stops competing on price."""
    bids = [_bid("o1", "a", "t_a", math.inf), _bid("o1", "b", "t_b", 3.0)]
    for winners in _resolve_both_orders(rule, bids, {"o1": "a"}):
        assert winners == ["t_b"], (
            f"{type(rule).__name__} let an inf-cost bid win over a finite one"
        )


@pytest.mark.parametrize(
    "rule", [LowestCostRule(), OwnerFirstRule(), HighestBenefitRule(), RandomAwardRule()],
    ids=["LowestCost", "OwnerFirst", "HighestBenefit", "RandomAward"],
)
def test_nan_cost_bids_do_not_destroy_the_total_order(rule):
    """``inf`` is comparable, so it alone cannot break an ordering — **NaN** is the
    value that does, because every comparison with it is False and a NaN in an
    early tuple position makes all later components unreachable.

    The reviewer could not reach NaN through the shipped cost function and recorded
    it as a robustness gap; FIX-5's rule is that no non-finite value enters ordering
    arithmetic at all, so this is the test that actually pins that rule down.
    """
    nan = float("nan")
    bids = [_bid("o1", "a", "t_a", nan), _bid("o1", "b", "t_b", nan),
            _bid("o1", "c", "t_c", 2.0)]
    owners = {"o1": "a"}
    results = set()
    for permutation in ([bids[0], bids[1], bids[2]], [bids[2], bids[1], bids[0]],
                        [bids[1], bids[2], bids[0]]):
        awards = rule.resolve(
            permutation, market=PoolMarket([]), tick_seed=99,
            owner_of=lambda oid: owners.get(oid), rng=random.Random(5),
        )
        results.add(tuple((a.order_id, a.carrier_haulier_id) for a in awards))
    assert len(results) == 1, (
        f"{type(rule).__name__} lost its total order on NaN bids: {results}"
    )


def test_highest_benefit_key_stays_finite_when_the_reserve_is_real():
    """The specific F10 arithmetic: a DEFINED (finite) owner reserve against a
    non-finite partner cost must not produce a NaN or inf sort key."""
    bids = [_bid("o1", "a", "t_a", 4.0),              # owner, finite -> reserve = 4.0
            _bid("o1", "b", "t_b", math.inf)]         # partner, unknown cost
    results = _resolve_both_orders(HighestBenefitRule(), bids, {"o1": "a"})
    assert results[0] == results[1], f"order-dependent: {results}"
    assert results[0] == ["t_a"], "the finite, defined-benefit bid must win"


def test_owner_first_prefers_the_owner_even_at_unknown_cost_by_design():
    """OwnerFirst deliberately ranks owner-preference ABOVE cost — it models a
    company that will not cede a job it can do. So the owner still wins with an
    unknown (inf) cost, and that is the contract, not a leak of the F10 defect.
    What FIX-5 guarantees here is only that the ordering stays TOTAL (asserted in
    test_arbitration_is_a_total_order_with_infinite_costs)."""
    bids = [_bid("o1", "a", "t_a", math.inf), _bid("o1", "b", "t_b", 3.0)]
    results = _resolve_both_orders(OwnerFirstRule(), bids, {"o1": "a"})
    assert results[0] == results[1] == ["t_a"]
    # ...and with no owner bid in play it falls back to cost, finite first.
    partner_only = [_bid("o1", "b", "t_b", math.inf), _bid("o1", "c", "t_c", 3.0)]
    for winners in _resolve_both_orders(OwnerFirstRule(), partner_only, {"o1": "a"}):
        assert winners == ["t_c"]


# --- FIX-9 addition: the NON-SPATIAL fallback path ---------------------------
#
# Everything above drives the SPATIAL path (`use_spatial_matching: true`, the
# shipped default), which reaches the solver via `solve_pairs` over a prebuilt
# candidate index. Plan §13.5 note 2: after FIX-3 removed the shuffle, the host's
# `sorted()` discipline may have become structurally redundant in the spatial
# path — but it is still load-bearing in the NON-SPATIAL fallback
# (`use_spatial_matching: false`), which routes through `ctx.rng.sample` /
# `solver.solve` instead. That path had no I-P5 coverage at all.
#
# Both tests below are appended, not edits: nothing above this line changed.


def _non_spatial_app(trucks, orders, coop):
    """`_app` with the supported profile knob that selects the fallback path."""
    app = _app(trucks, orders, cooperation=coop)
    app.behavior["profile"]["use_spatial_matching"] = False
    return app


def test_tied_awards_order_independent_on_the_non_spatial_path():
    """I-P5 on the `use_spatial_matching: false` fallback, same tied fixture.

    FINDING (honest report): the property DOES hold here — 1 distinct award set
    and a constant award count over 200 permutations. It is not vacuous: with the
    pooled tie-break neutered (`set_tiebreak` made a no-op) this fails immediately,
    see `test_the_non_spatial_tie_break_is_what_makes_the_above_true`.
    """
    trucks, orders = _tied_fleet()
    coop = _coop([["acme", "borax"]])

    seen = set()
    counts = set()
    rnd = random.Random(20260817)
    for _ in range(200):
        t, o = list(trucks), list(orders)
        rnd.shuffle(t)
        rnd.shuffle(o)
        # Perturb the module RNG too: the pooled path must not consult it.
        random.seed(rnd.random())
        result = _non_spatial_app(t, o, coop).assign("2020-01-01 08:00:00", time_step=42)
        seen.add(tuple(_ids(result)))
        counts.add(len(result))

    assert len(seen) == 1, (
        f"I-P5 VIOLATED on the non-spatial path: {len(seen)} distinct award sets over "
        f"200 permutations of the same input at the same tick seed: {sorted(seen)}"
    )
    assert len(counts) == 1, f"award COUNT varied across permutations: {sorted(counts)}"


def test_the_non_spatial_tie_break_is_what_makes_the_above_true():
    """Teeth guard for the test above: prove the fixture can still expose a
    violation, so a future regression cannot pass it vacuously.

    Neutering `set_tiebreak` puts the solver back on its shuffle, which is exactly
    the F3 defect — reproducibility for a fixed input order, not independence OF
    input order. If this stops producing multiple award sets, the fixture has lost
    its ties and the test above is no longer meaningful.
    """
    from apps.container_logistics.assignment.solver.base import BaseAssignmentSolver

    trucks, orders = _tied_fleet()
    coop = _coop([["acme", "borax"]])
    original = BaseAssignmentSolver.set_tiebreak
    seen = set()
    rnd = random.Random(20260817)
    try:
        BaseAssignmentSolver.set_tiebreak = lambda self, seed: None  # neutered
        for _ in range(200):
            t, o = list(trucks), list(orders)
            rnd.shuffle(t)
            rnd.shuffle(o)
            random.seed(rnd.random())
            result = _non_spatial_app(t, o, coop).assign("2020-01-01 08:00:00", time_step=42)
            seen.add(tuple(_ids(result)))
    finally:
        BaseAssignmentSolver.set_tiebreak = original

    assert len(seen) > 1, (
        "the non-spatial tied fixture no longer exposes an I-P5 violation even with "
        "the tie-break neutered — the test above has gone vacuous"
    )


def test_non_spatial_tied_awards_are_pythonhashseed_independent():
    """The `sorted()` discipline in the host (review F9 mutants M3/M5) is observable
    only across PROCESSES, because set-of-str iteration order is stable within one.

    Mirrors `test_tied_awards_are_pythonhashseed_independent` for the non-spatial
    fallback, which is where plan §13.5 note 2 says that discipline is still
    load-bearing after FIX-3.

    HONEST LIMITATION — this test does NOT kill the F9 mutants. Measured: with
    `company_ids = sorted(...)` -> `list(...)` (M3) and with
    `foreign_ids = sorted(...)` -> `list(...)` (M5), this test still PASSES, on
    the non-spatial path as well as the spatial one. Reading the post-FIX-3 code,
    both look structurally equivalent now rather than merely unexercised:

    - M5's `foreign_ids` order only ever reaches `solver.solve`/`solve_pairs`,
      whose FIX-3 sort key `(cost, missing, pair_tiebreak)` is a TOTAL order, so
      the input list order cannot survive it.
    - M3's `company_ids` order would matter only if one company's mid-round direct
      commits could disturb another's. They cannot: direct awards are restricted to
      a company's own PRIVATE orders using its own trucks, `free_trucks_by_haulier`
      is snapshotted once per round, and arbitration runs after the whole company
      loop under an order-independent rule.

    So plan §13.5 note 2's expectation that `sorted()` stays load-bearing in the
    non-spatial fallback does not appear to hold — the fallback is protected by the
    same total order as the spatial path. That is the §13.5-note-2 "pass" case
    (the property now holds structurally, not by discipline), not a test gap; keep
    the `sorted()` calls as defence in depth. What this test DOES pin down is the
    end-to-end property itself: the award set must not vary with PYTHONHASHSEED.
    """
    import os
    import subprocess
    import sys

    script = (
        "import random, json, sys;"
        "sys.path.insert(0, '/home/user/openride_apps');"
        "from tests.test_assignment_pooled_determinism import _tied_fleet, _non_spatial_app;"
        "from tests.test_assignment_cooperation import _coop;"
        "from tests.test_assignment_pooled import _ids;"
        "t, o = _tied_fleet();"
        "app = _non_spatial_app(t, o, _coop([['acme','borax']]));"
        "print(json.dumps(_ids(app.assign('2020-01-01 08:00:00', time_step=42))))"
    )
    results = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert out.returncode == 0, f"subprocess failed (PYTHONHASHSEED={seed}): {out.stderr[-2000:]}"
        results.add(out.stdout.strip())
    assert len(results) == 1, (
        f"non-spatial award set varies with PYTHONHASHSEED: {results}"
    )
