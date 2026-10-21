"""``PooledMarket``: one planner per company, cooperating through shared order pools.

Single responsibility: the pre-v2 ``pooled`` topology as an ordinary built-in dispatch
solver (plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.3, §2.6). It builds
the market's components from its typed params (``framework.components
.instantiate_component``, the ONLY instantiation path: the fail-soft legacy getters
are not used), the companies' fleets and order books, and the ``PoolMarket`` of the
active structure, and runs the market engine (``assignment.pooled_planner
.run_pooled_market``). Each company's deployment is decided by ``inner`` through
``compose.ScopeDecider`` (message 19 drops that company for that round; a crash is
message 20 and skips it, as before). Every company solve goes through
``ctx.execute`` (``ScopeDecider.run``), so ``solve_in_thread`` offloads it once, as
``Partitioned`` offloads each group (final review MAJOR-5, correcting C3 deviation 8).
Byte identity holds: with the flag off ``execute`` is a plain call, and with it on
``tpool.execute`` is synchronous, so solve order and RNG draws are unchanged.

Returns ``Plan(chains in award order, notes={"market": round provenance,
"pair_notes": {(truck_id, order_id): {pool_id, market_round}}})``. The framework
re-checks every head against the world rule, does the accounting (``audited`` tags)
and records the round provenance from ``notes["market"]``.

Randomness: the tick ``Random`` (``ctx.rng``, created here on first read) is injected
into ``inner`` with ``set_rng`` and the tick seed with ``set_tiebreak`` (through the
methods, so a patched class method still sees them), before any draw, in the pre-v2
order. Offer, claim, arbitration, company sampling and the inner all draw from it.

Policy side of the boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import pooled_planner
from apps.container_logistics.assignment.pools import PoolMarket
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.components import instantiate_component

from .. import compose
from ..composite import CompositePolicy
from ..context import DispatchContext
from ..plan import Plan

logger = logging.getLogger(__name__)

__all__ = ["PooledMarket", "MARKET_NOTE_KEYS"]

#: The keys of ``Plan.notes["market"]`` (``AssignmentApp._record_round_stats`` reads them).
MARKET_NOTE_KEYS = ("rounds_used", "converged", "candidate_pairs_round1", "candidate_pairs_total", "max_rounds")

_INNER_HELP = (
    "The per-scope solver each company's deployment is decided by (a leaf: GreedyNearest, "
    "RandomAssignment). --solver sets it."
)

Doc = dict[str, Any]


@policy(
    slot="dispatch",
    name="PooledMarket",
    version="1",
    description="One planner per company; companies offer orders into shared pools, claim and "
                "are arbitrated in rounds until the market converges. The pre-v2 'pooled' topology.",
    information_model="per_company",
    # Per-arm budgets for the battery's performance.scale check (round-2 C5 rule:
    # max(3 x worst of 2 x 5 runs, 500)). Measured 2026-10-21 on the whole-tick
    # testing.fixtures.scale() (1000 trucks x 500 orders, one company, no pools),
    # 2 x 5 runs, load avg 5.8-6.5 on 32 cores (one arm changes one param):
    #   default (OfferAll/ClaimAllPlanned/LowestCost, inner RandomAssignment):
    #                                155.8-159.0 ms -> 477 -> 500
    #   offer=OfferNone              155.9-161.3 ms -> 484 -> 500
    #   offer=OfferSpare             155.7-204.9 ms -> 615 -> 620
    #   claim=ClaimIfGainExceeds     154.8-158.2 ms -> 475 -> 500
    #   claim=ClaimNone              153.0-205.0 ms -> 615 -> 620
    #   arbitration=HighestBenefit   153.5-156.3 ms -> 469 -> 500
    #   arbitration=OwnerFirst       152.3-156.9 ms -> 471 -> 500
    #   arbitration=RandomAward      155.3-159.0 ms -> 477 -> 500
    #   inner=GreedyNearest          143.3-192.8 ms -> 578 -> 580
    # (round-1 partition-scope default measurement at load 3.3 was 148.1-177.4 -> 540.)
    perf_budget_ms=500,
    arm_budgets_ms={
        "offer=OfferNone": 500,
        "offer=OfferSpare": 620,
        "claim=ClaimIfGainExceeds": 500,
        "claim=ClaimNone": 620,
        "arbitration=HighestBenefit": 500,
        "arbitration=OwnerFirst": 500,
        "arbitration=RandomAward": 500,
        "inner=GreedyNearest": 580,
    },
)
class PooledMarket(CompositePolicy):
    """Offer, claim and arbitrate in rounds; each company's deployment decided by ``inner``."""

    @dataclass(frozen=True)
    class Params(PolicyParams):
        max_rounds: int = param(
            20, ge=1, le=100,
            help="Safety backstop on auction rounds; the auction ends on convergence. A tick "
                 "that hits it is logged and counted as truncated.",
        )
        offer: PolicyRef = param(ref("OfferAll"), kind="market.offer",
                                 help="Which own orders each company contributes to its pools.")
        claim: PolicyRef = param(ref("ClaimAllPlanned"), kind="market.claim",
                                 help="Which planned pooled pairs each company bids on.")
        arbitration: PolicyRef = param(ref("LowestCost"), kind="market.arbitration",
                                       help="Who wins a contested order.")
        inner: PolicyRef = param(ref("RandomAssignment"), kind="dispatch", leaf_only=True, help=_INNER_HELP)

    def __init__(self, params=None, **kw: Any) -> None:
        super().__init__(params, **kw)
        self._components: tuple[Any, Any, Any] | None = None

    def components(self) -> tuple[Any, Any, Any]:
        """``(offer, claim, arbitration)``, built once from the typed params (fail-loud lookup)."""
        if self._components is None:
            # self.params is the plain dict this policy was built with; re-validating it
            # resolves the component refs to typed values (names checked live).
            typed = self.Params.from_mapping(self.params, where="PooledMarket params")
            self._components = (
                instantiate_component("market.offer", typed.offer),
                instantiate_component("market.claim", typed.claim),
                instantiate_component("market.arbitration", typed.arbitration),
            )
        return self._components

    def decide(self, ctx: DispatchContext) -> Plan:
        """Run the market for ``ctx``'s trucks and orders; the plan carries the round provenance."""
        # Before any draw and in the pre-v2 order (S4): the leaf's equal-cost shuffle and
        # tie-break become reproducible for this tick.
        self.inner.set_rng(ctx.rng)
        self.inner.set_tiebreak(ctx.tick_seed)
        offer, claim, arbitration = self.components()
        max_rounds = int(self.params["max_rounds"])
        result = pooled_planner.run_pooled_market(
            trucks_by_haulier=_by_haulier(ctx.raw_trucks),
            orders_by_haulier=_by_haulier(ctx.raw_orders),
            market=PoolMarket.from_structure(ctx.world.structure),
            ctx=pooled_planner.PooledPlannerContext(
                dispatch=compose.ScopeDecider(self.inner, self.inner_node, ctx),
                offer=offer,
                claim=claim,
                arbitration=arbitration,
                max_rounds=max_rounds,
                cost=ctx.accounting_cost,
                rng=ctx.rng,
                tick_seed=ctx.tick_seed,
                max_travel_time_pickup=ctx.world.max_travel_time_pickup,
                candidate_settings=ctx.candidate_settings,
                clock=ctx.clock,
            ),
        )
        notes = {
            "market": {
                "rounds_used": result.rounds_used,
                "converged": result.converged,
                "candidate_pairs_round1": result.candidate_pairs_round1,
                "candidate_pairs_total": result.candidate_pairs_total,
                "max_rounds": max_rounds,
            },
            "pair_notes": dict(result.pair_notes),
        }
        return Plan.from_pairs(result.assignment, notes=notes)


def _by_haulier(docs: Any) -> dict[str, list[Doc]]:
    """Documents grouped by haulier in input order; a document without one is left out (fail-closed)."""
    out: dict[str, list[Doc]] = {}
    for doc in docs:
        hid = assign_constraints.haulier_of(doc)
        if hid:  # a truck/order with no haulier can never match
            out.setdefault(hid, []).append(doc)
    return out
