"""Candidate-generation parity between `pooled` and `partitioned` (plan §13.4 FIX-2).

Review finding F4: `_solve_company` built a spatial index PER COMPANY, so an order
visible to ``H`` companies drew up to ``per_order × H`` candidate trucks per round,
while ``partitioned`` draws ``per_order`` total. The reviewer measured 115,506 vs
20,000 pairs — a 5.8x wider candidate set for the cooperating arm.

That matters because it is a *systematic between-arm bias correlated with the
treatment*: audit P3 measured that candidate width ALONE moves served orders from
402/500 to 500/500 and mean deadhead from 5.25 km to 7.57 km — orders of magnitude
above the 0.46 pp minimum detectable effect. Replicates cannot average it away.

**What parity means here, precisely.** Convergence termination (FIX-1) means the
pooled market runs several rounds, and each round legitimately builds candidates for
the orders still free. So the invariant is *per-round* policy equality, not equal
totals:

  1. the FIRST round builds exactly as many candidate pairs as partitioned builds
     in total — the candidate policy is now identical; and
  2. the total does not scale with the number of cooperating companies — the
     treatment-correlated amplification is gone.

This is a deliberate deviation from §13.4's literal "assert total pairs agree";
that wording predates the interaction with FIX-1's extra rounds. Asserting equal
totals would force a choice between convergence and parity, and convergence is the
more important property.
"""

import random

import pytest

from apps.container_logistics.assignment import pooled_planner as PP
from apps.container_logistics.assignment import spatial

from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app

_HAULIERS = ["acme", "borax", "cargo", "delta", "echo"]


def _fleet(companies, trucks_each, orders_each, *, chain=False, seed=7):
    r = random.Random(seed)
    hs = _HAULIERS[:companies]
    trucks, orders = [], []
    for h in hs:
        for j in range(trucks_each):
            trucks.append(_truck(f"t_{h}_{j}", h, 103.7 + r.random() * 0.3, 1.2 + r.random() * 0.2))
        for j in range(orders_each):
            orders.append(_order(f"o_{h}_{j}", h, 103.7 + r.random() * 0.3, 1.2 + r.random() * 0.2))
    if chain:
        edges = [[hs[i], hs[i + 1]] for i in range(len(hs) - 1)]
    else:
        edges = [[a, b] for i, a in enumerate(hs) for b in hs[i + 1:]]
    return trucks, orders, _coop(edges)


def _candidate_calls(topology, trucks, orders, coop):
    """Per-call candidate-pair counts, by wrapping the REAL generator."""
    original = spatial.iter_candidate_pairs
    per_call = []

    def wrapped(t, o, **kw):
        out = list(original(t, o, **kw))
        per_call.append(len(out))
        return iter(out)

    spatial.iter_candidate_pairs = wrapped
    try:
        _app(trucks, orders, cooperation=coop, topology=topology).assign("2020-01-01 08:00:00")
    finally:
        spatial.iter_candidate_pairs = original
    return per_call


def _pooled_candidates_per_round(trucks, orders, coop):
    """Total candidate pairs built in each pooled ROUND.

    Measured at the round boundary, not per call: one round legitimately makes
    several ``iter_candidate_pairs`` calls — one per eligible-carrier group — so a
    chain structure produces several calls per round while a clique produces one.
    The budget that must match ``partitioned`` is the per-ROUND total.
    """
    original = PP.build_round_candidates
    per_round = []

    def wrapped(**kw):
        out = original(**kw)
        per_round.append(sum(len(v) for v in out.values()))
        return out

    PP.build_round_candidates = wrapped
    try:
        _app(trucks, orders, cooperation=coop, topology="pooled").assign("2020-01-01 08:00:00")
    finally:
        PP.build_round_candidates = original
    return per_round


@pytest.mark.parametrize("chain", [False, True], ids=["clique", "chain"])
@pytest.mark.parametrize("companies", [2, 3, 5])
def test_pooled_and_partitioned_build_the_same_candidate_count(companies, chain):
    """The FIRST pooled round must build exactly what partitioned builds in total.

    Verified to have teeth: reverting `_solve_company` to a per-company index makes
    this fail with pooled/partitioned ratios of 1.9x (H=2) up to 4.4x (H=5).
    """
    per = max(4, 100 // companies)
    trucks, orders, coop = _fleet(companies, per, per, chain=chain)

    pooled_rounds = _pooled_candidates_per_round(trucks, orders, coop)
    partitioned = sum(_candidate_calls("partitioned", trucks, orders, coop))

    assert pooled_rounds, "pooled built no candidates at all"
    assert pooled_rounds[0] == partitioned, (
        f"candidate policy diverged: pooled round 1 built {pooled_rounds[0]} pairs, "
        f"partitioned built {partitioned} in total"
    )


@pytest.mark.parametrize("chain", [False, True], ids=["clique", "chain"])
def test_round1_candidate_parity_is_exact_at_every_company_count(chain):
    """The guarantee is EXACT round-1 parity, at every company count.

    Replaces an earlier `ratio < 2.0` / `ratios[5] < 2.0 * ratios[2]` assertion —
    the same soft-tolerance pattern §13 criticised elsewhere, and one that would
    have passed under a genuine regression. The F4 signature was that the
    pooled/partitioned ratio GREW with company count; exact round-1 equality at
    every H rules that out without a magic threshold.

    The per-tick TOTAL is recorded, not asserted against a bound: a multi-round
    auction re-indexes over the shrinking free set, so a ratio > 1 is inherent and
    (review §16.3) causally inert.
    """
    observed = {}
    for companies in (2, 3, 5):
        per = max(4, 100 // companies)
        trucks, orders, coop = _fleet(companies, per, per, chain=chain)
        pooled_rounds = _pooled_candidates_per_round(trucks, orders, coop)
        partitioned = sum(_candidate_calls("partitioned", trucks, orders, coop))
        assert pooled_rounds, f"H={companies}: pooled built no candidates"
        assert pooled_rounds[0] == partitioned, (
            f"H={companies}: round-1 parity broken — pooled {pooled_rounds[0]} vs "
            f"partitioned {partitioned}"
        )
        observed[companies] = (sum(pooled_rounds), partitioned)

    # Recorded for the run's provenance, deliberately NOT thresholded.
    print("per-tick totals (pooled, partitioned):", observed)


def test_convergence_rounds_process_a_shrinking_residual():
    """Sanity on the residual's shape: later rounds must build strictly less work,
    which is what keeps the total bounded despite convergence termination."""
    trucks, orders, coop = _fleet(5, 20, 20)
    per_round = _pooled_candidates_per_round(trucks, orders, coop)
    assert len(per_round) >= 2, "expected more than one convergence round on this fixture"
    assert per_round == sorted(per_round, reverse=True), (
        f"candidate work per round should decay monotonically, got {per_round}"
    )
    assert per_round[1] < per_round[0] / 2, "second round should be a small residual"
