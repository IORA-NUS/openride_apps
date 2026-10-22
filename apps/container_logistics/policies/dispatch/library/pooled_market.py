"""``PooledMarket``: one planner per company, cooperating through shared order pools.

Single responsibility: the pre-v2 ``pooled`` topology as an ordinary built-in planner
solver (plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.3, §2.6; contract
``plan_contract_final.md`` §2). At run start ``bind`` resolves the scenario's active
cooperation arm, validates it (message 14) and, at the top, declares the arm's
cross-company pairs (a pool only pairs adjacent companies, so the market's foreign
awards are exactly those). Each tick it builds the market's components from its typed
params (``framework.components.instantiate_component``, the ONLY instantiation path:
the fail-soft legacy getters are not used), the companies' fleets and order books, and
the ``PoolMarket`` of the active arm, and runs the market engine
(``assignment.pooled_planner.run_pooled_market``). Each company's deployment is a
sub-tick decided by ``inner`` through ``tick.call`` (:class:`_CompanyDecider`, the
duck-typed ``dispatch`` object of ``PooledPlannerContext``): message 19 drops that
company for that round; a crash is message 20 and skips it, as before. The framework
offloads every company solve when ``solve_in_thread`` is on (final review MAJOR-5).
Byte identity holds: with the flag off ``execute`` is a plain call, and with it on
``tpool.execute`` is synchronous, so solve order and RNG draws are unchanged.

Company feasibility (``CompanyRule``, moved here from the framework): own orders keep
the strict rule, pooled foreign orders the physical rule only. The company sub-tick
keeps the framework's ``permitted`` (``narrow=None``): a company sub-tick holds only
that carrier's trucks, so the two agree whenever every pool pairs adjacent companies,
which message 14 guarantees at run start. ``CompanyRule`` still builds the round's
prebuilt spatial candidates (``pooled_planner.build_round_candidates``).

Returns ``Result(pairs in award order, solver_notes={(truck_id, order_id): {pool_id,
market_round}}, tick_notes=<round provenance, MARKET_NOTE_KEYS>)``. The framework
re-checks every pair and does the accounting (``audited`` tags); the round provenance is
folded by the run-stats aggregator the top ``bind`` declares
(``_market_stats.RoundStatsAggregator``, ``run_config.meta.market``).

Bid cost (plan ``plan_contract_final.md`` G2, WS4a): the market prices offers, claims,
arbitration and award ``cost_km`` with its OWN cost, built from its inner's dual-cycle
params when the inner declares them (GreedyNearest) and the constraint defaults
otherwise; it no longer borrows the framework's accounting measurement. Identical
whenever the live dials equal the inner's (the one-dial mirror) or are at the defaults
(every shipped bundle and golden case). The boundary: pooled + a cost-blind inner
(RandomAssignment) + non-default ``solver_params`` now bids with the constraint defaults.
The company sub-ticks keep the framework's measurement.

Randomness: the tick ``Random`` (``tick.rng``, created here on first read) is injected
into ``inner`` with ``set_rng`` and the tick seed with ``set_tiebreak`` (through the
methods, so a patched class method still sees them), before any draw, in the pre-v2
order. Offer, claim, arbitration, company sampling and the inner all draw from it.

Policy side of the boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import pooled_planner
from apps.container_logistics.assignment.pools import PoolMarket
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.components import instantiate_component
from apps.container_logistics.policies.solver_lib import cooperation, objective
from apps.container_logistics.policies.solver_lib.candidates import CandidateSettings

from ..bind import Declaration
from ..composite import CompositePolicy
from ..result import Result
from ._market_stats import RoundStatsAggregator

logger = logging.getLogger(__name__)

__all__ = ["CompanyRule", "PooledMarket", "MARKET_NOTE_KEYS"]

#: The keys of ``Result.tick_notes`` (``_market_stats.RoundStatsAggregator.fold`` reads them).
MARKET_NOTE_KEYS = ("rounds_used", "converged", "candidate_pairs_round1", "candidate_pairs_total", "max_rounds")

_INNER_HELP = (
    "The per-scope solver each company's deployment is decided by (a leaf: GreedyNearest, "
    "RandomAssignment). --solver sets it."
)

Doc = dict[str, Any]


@policy(
    slot="planner",
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
        inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True, help=_INNER_HELP)

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

    def bind(self, params: Any, world: Mapping[str, Any], *, top: bool) -> Declaration:
        """Validate the active arm (message 14); at the top declare its cross-company pairs and run stats.

        Reads every world key ``solve_tick`` reads: ``cooperation``,
        ``max_travel_time_pickup`` (the company rule) and the four candidate knobs (the
        round prebuild).
        """
        cooperation.validate_arm(world)
        arm = cooperation.resolve_arm(world)
        world.get("max_travel_time_pickup")
        CandidateSettings.from_world(world)
        if not top:
            return Declaration()
        return Declaration(cross_company=cooperation.directed_pairs(arm), run_stats=RoundStatsAggregator)

    def solve_tick(self, tick: Any, params: Any) -> Result:
        """Run the market for ``tick``'s trucks and orders; the result carries the round provenance."""
        # Before any draw and in the pre-v2 order (S4): the leaf's equal-cost shuffle and
        # tie-break become reproducible for this tick.
        self.inner.set_rng(tick.rng)
        self.inner.set_tiebreak(tick.seed)
        offer, claim, arbitration = self.components()
        max_rounds = int(self.params["max_rounds"])
        arm = cooperation.resolve_arm(tick.world)
        result = pooled_planner.run_pooled_market(
            trucks_by_haulier=_by_haulier(tick.raw_trucks),
            orders_by_haulier=_by_haulier(tick.raw_orders),
            market=PoolMarket.from_structure(arm.structure if arm is not None else None),
            ctx=pooled_planner.PooledPlannerContext(
                dispatch=_CompanyDecider(tick, params.inner),
                offer=offer,
                claim=claim,
                arbitration=arbitration,
                max_rounds=max_rounds,
                cost=_bid_cost(tick.callee(params.inner).params),
                rng=tick.rng,
                tick_seed=tick.seed,
                max_travel_time_pickup=tick.world.get("max_travel_time_pickup"),
                candidate_settings=CandidateSettings.from_world(tick.world),
                clock=tick.clock,
            ),
        )
        market = {
            "rounds_used": result.rounds_used,
            "converged": result.converged,
            "candidate_pairs_round1": result.candidate_pairs_round1,
            "candidate_pairs_total": result.candidate_pairs_total,
            "max_rounds": max_rounds,
        }
        return Result.from_pairs(result.assignment, solver_notes=dict(result.pair_notes), tick_notes=market)


def _bid_cost(inner_params: Any) -> Any:
    """The market's own bid cost: its inner's dual-cycle params when declared, else the constraint defaults."""
    bonus = getattr(inner_params, "dual_cycle_bonus_km", None)
    radius = getattr(inner_params, "dual_cycle_radius_km", None)
    return objective.dual_cycle_cost_fn(
        bonus_km=assign_constraints._DEFAULT_DUAL_CYCLE_BONUS_KM if bonus is None else bonus,
        radius_km=assign_constraints._DEFAULT_DUAL_CYCLE_RADIUS_KM if radius is None else radius,
    )


@dataclass(frozen=True)
class CompanyRule:
    """Pooled-market feasibility for one carrier company (moved verbatim from ``dispatch/allowed.py``).

    Own orders keep the STRICT rule (``constraints.pair_allowed``); pooled foreign
    orders drop the haulier-equality test because pool visibility already proved
    eligibility by construction (``pair_allowed_ignoring_haulier``).
    """

    haulier_id: str
    max_travel_time_pickup: Any

    def allowed(self, t: dict[str, Any], o: dict[str, Any]) -> bool:
        """``pooled_planner._pair_allowed_for(haulier_id)``, verbatim."""
        if assign_constraints.haulier_of(o) == self.haulier_id:
            return assign_constraints.pair_allowed(
                t, o, max_travel_time_pickup=self.max_travel_time_pickup
            )
        return assign_constraints.pair_allowed_ignoring_haulier(
            t, o, max_travel_time_pickup=self.max_travel_time_pickup
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (diagnostics)."""
        return {
            "kind": "company",
            "haulier_id": self.haulier_id,
            "max_travel_time_pickup": self.max_travel_time_pickup,
        }


class _CompanyDecider:
    """Decides one pooled-market company per call: the object ``PooledPlannerContext.dispatch`` holds.

    The market engine (``assignment.pooled_planner``) calls ``candidate_kind``,
    ``company_rule``, ``company_context``, ``run`` and ``report_crash``; each maps onto
    the market's tick (``callee``, ``restrict``, ``call``, ``docs``, ``note_incident``).
    Moved from the old framework ``compose.ScopeDecider``.
    """

    def __init__(self, tick: Any, inner_ref: Any) -> None:
        self.tick = tick
        self.inner_ref = inner_ref
        self.node = tick.callee(inner_ref)
        self.candidate_kind = self.node.declaration.candidate_hint or "sampled"

    @staticmethod
    def company_rule(haulier_id: str, max_travel_time_pickup: Any) -> CompanyRule:
        """The pooled feasibility rule for ``haulier_id`` (builds the round's prebuilt candidates)."""
        return CompanyRule(haulier_id=haulier_id, max_travel_time_pickup=max_travel_time_pickup)

    def company_context(self, *, trucks: list[Doc], orders: list[Doc], haulier_id: str, round: int | None,
                        rule: CompanyRule, rng: Any, settings: Any, accounting_cost: Any, clock: Any,
                        prebuilt_pairs: Any = None) -> Any:
        """The company's sub-tick: its trucks and visible orders; the round's prebuilt pairs on the spatial kind.

        ``rng`` (the market's tick ``Random``) is the cap sample's RNG. ``rule``,
        ``settings``, ``accounting_cost`` and ``clock`` are the market's own and need no
        handing down (the sub-tick inherits the tick's ``permitted``, world, measure and
        clock); ``round`` is not recorded.
        """
        return self.tick.restrict(
            trucks=trucks,
            orders=orders,
            scope=f"company:{haulier_id}",
            sample_rng=rng,
            candidates=list(prebuilt_pairs or ()) if self.candidate_kind == "spatial" else None,
        )

    def run(self, sub: Any) -> list[tuple[Doc, Doc]]:
        """The pairs ``inner`` commits for company sub-tick ``sub`` (``[]`` when its result is discarded)."""
        result = self.tick.call(self.inner_ref, sub)
        return [] if result is None else sub.docs(result)

    def report_crash(self, *, haulier_id: str, round: int) -> None:
        """Log message 20 with the traceback (call it inside ``except``) and count it."""
        self.tick.note_incident()
        logger.exception(
            "PLANNER POLICY CRASHED: inner %s raised for company %r in round %d at step %d; "
            "that company commits nothing this round (pooled-market behaviour, unchanged). "
            "Traceback follows. (celery journal, not sim.log)",
            self.node.name, haulier_id, round, self.tick.clock.time_step,
        )


def _by_haulier(docs: Any) -> dict[str, list[Doc]]:
    """Documents grouped by haulier in input order; a document without one is left out (fail-closed)."""
    out: dict[str, list[Doc]] = {}
    for doc in docs:
        hid = assign_constraints.haulier_of(doc)
        if hid:  # a truck/order with no haulier can never match
            out.setdefault(hid, []).append(doc)
    return out
