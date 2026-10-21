"""
Shared-pool cooperation: the pooled market ENGINE (solver side).

Implements steps 4-6 of ``docs/shared_pool_cooperation_plan.md`` §4.1: contribution,
bounded bidding rounds, arbitration and commit. It is run by the ``PooledMarket``
dispatch solver (``policies/dispatch/library/pooled_market.py``, round-2 §2.3); the
share ACCOUNTING (owner reserve, ``benefit_km``) is no longer here: the framework
computes it for every solver in ``assignment/accounting.py``, and step 6 only returns
the awarded pairs plus their market provenance (``pair_notes``).

**The boundary** (plan §1) is load-bearing in this file:

- Everything here is a MARKET decision -- who wins a contested order, how many rounds
  happen, what is visible to whom, what is committed. It must never depend on dict
  iteration order: companies are always iterated ``sorted()``, and the arbitration
  rule is required to be order-independent.
- Every COMPANY decision is delegated: what to contribute (``ctx.offer``), what to
  bid on (``ctx.claim``), and which truck serves which job (``ctx.dispatch``, the
  ``compose.ScopeDecider`` adapter; this module never imports ``policies`` itself).

``PoolMarket`` is the only object allowed to mutate allocation state; this module
asks it to ``commit`` and never bookkeeps allocation on the side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from . import constraints as assign_constraints
from . import spatial
from .arbitration import DEFAULT_ARBITRATION_RULE, BaseArbitrationRule
from .policy.claim import DEFAULT_CLAIM_POLICY, BaseClaimPolicy
from .policy.offer import DEFAULT_OFFER_POLICY, BaseOfferPolicy
from .pools import Award, PoolMarket
from .solver.base import Cost, Order, PairAllowed, Truck

__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "MAX_ROUNDS_CEILING",
    "PooledMarketResult",
    "PooledPlannerContext",
    "build_round_candidates",
    "effective_cooperation_stamp",
    "resolve_market_config",
    "run_pooled_market",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# max_rounds is a SAFETY BACKSTOP, not a tuning knob (plan §13.4 FIX-1).
#
# Termination is CONVERGENCE-based: the round loop ends when a round commits no
# award (``if not committed_any: break``). That is a property of the workload,
# not a number a plan author gets to pick. The backstop only exists to bound a
# pathological case, exactly like ``POST_HORIZON_DRAIN_MAX_STEPS`` bounds the
# horizon drain behind its real drain condition (CLAUDE.md §6.5).
#
# The previous default of 2 truncated the auction *before* convergence, and did
# so in proportion to how much cooperation was configured — withholding
# throughput as a function of the treatment. Because serving fewer orders
# improves mean deadhead (solver_boundary_audit.md P3), that handed the
# cooperating arm a SPURIOUS DEADHEAD ADVANTAGE that grew with the number of
# cooperating companies: a false positive on the headline metric. If the
# backstop ever binds, the run is suspect and must say so itself.
DEFAULT_MAX_ROUNDS = 20
MAX_ROUNDS_CEILING = 100


def resolve_market_config(planner: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The EFFECTIVE market block: authored values with defaults filled in.

    Single source of truth for "what will actually run", used both by
    :meth:`AssignmentApp._market_components` and by the run stamp, so a run can
    never record a market block that differs from the one it executed (F7).
    Pure and defensive — it never raises on a malformed block.
    """
    market = ((planner or {}).get("market") or {}) if isinstance(planner, dict) else {}
    if not isinstance(market, dict):
        market = {}

    def _role(name: str, default_type: str) -> Dict[str, Any]:
        raw = market.get(name)
        if isinstance(raw, dict):
            rtype = raw.get("type")
            params = raw.get("params")
            return {
                "type": str(rtype) if rtype else default_type,
                "params": dict(params) if isinstance(params, dict) else {},
            }
        return {"type": default_type, "params": {}}

    try:
        max_rounds = int(market.get("max_rounds", DEFAULT_MAX_ROUNDS))
    except (TypeError, ValueError):
        max_rounds = DEFAULT_MAX_ROUNDS
    max_rounds = max(1, min(MAX_ROUNDS_CEILING, max_rounds))

    return {
        "offer": _role("offer", DEFAULT_OFFER_POLICY),
        "claim": _role("claim", DEFAULT_CLAIM_POLICY),
        "arbitration": _role("arbitration", DEFAULT_ARBITRATION_RULE),
        "max_rounds": max_rounds,
    }


def effective_cooperation_stamp(
    profile: Optional[Dict[str, Any]], active_structure: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """What the run will ACTUALLY do, for the run record (plan §13.4 FIX-7).

    Built from the same functions the runtime uses, so the stamp cannot drift
    from reality. This exists because the previous stamp wrote ``pools: []`` for
    every shipped bundle — none carries a compiled ``pools`` key — while the
    runtime derived a full market from ``edges``, i.e. it recorded the OPPOSITE
    of what ran, on exactly the workflow §8 promises.
    """
    from .pools import pools_from_structure  # local: keeps import graph acyclic

    active = active_structure if isinstance(active_structure, dict) else {}
    planner = (profile or {}).get("planner")
    topology = str((planner or {}).get("topology") or "partitioned")
    if topology == "two-stage":
        topology = "pooled"
    return {
        "structure_id": active.get("id"),
        "components": active.get("components") or [],
        "adjacency": active.get("adjacency") or {},
        "pools": [
            {"id": p.id, "members": sorted(p.members)}
            for p in pools_from_structure(active)
        ],
        "topology": topology,
        "market": resolve_market_config(planner),
    }


@dataclass
class PooledMarketResult:
    """What one pooled tick produced, including its own termination provenance.

    ``rounds_used`` / ``converged`` exist so the run can describe its own
    truncation. ``converged is False`` means the safety backstop bound before the
    auction stopped allocating — the tick under-served, and any throughput or
    deadhead number derived from it is suspect (plan §13.4 FIX-1). ``pair_notes``
    carries each cross-company award's market provenance (``pool_id``,
    ``market_round``) for the framework's audited share tags.
    """

    assignment: List[Tuple[Truck, Order]]
    rounds_used: int
    converged: bool
    #: Candidate pairs built in round 1 (the figure that must equal partitioned's
    #: total) and across all rounds of this tick (the inherent, inert excess).
    candidate_pairs_round1: int = 0
    candidate_pairs_total: int = 0
    pair_notes: Dict[Tuple[str, str], Dict[str, Any]] = field(default_factory=dict)


@dataclass
class PooledPlannerContext:
    """Everything the engine needs for one tick, injected by the ``PooledMarket`` solver.

    ``dispatch`` is the ``compose.ScopeDecider`` (``candidate_kind``,
    ``company_rule``, ``company_context``, ``run``, ``report_crash``); it is typed
    ``Any`` so this module keeps no import of the policies package.
    ``candidate_settings`` is its ``CandidateSettings``; ``clock`` its
    ``DispatchClock``. ``cost`` is the framework's accounting cost (bids are priced
    with it, and every award's ``cost_km`` equals it).
    """

    dispatch: Any
    offer: BaseOfferPolicy
    claim: BaseClaimPolicy
    arbitration: BaseArbitrationRule
    max_rounds: int
    cost: Cost
    rng: Any
    tick_seed: int
    max_travel_time_pickup: Any = None
    candidate_settings: Any = None
    clock: Any = None


def _entity_id(entity: Dict[str, Any]) -> str:
    return str(entity.get("_id"))


def build_round_candidates(
    *,
    free_trucks_by_haulier: Dict[str, List[Truck]],
    free_orders: List[Order],
    market: PoolMarket,
    owner_by_order_id: Dict[str, str],
    pair_allowed_for: Callable[[str], PairAllowed],
    spatial_params: Dict[str, Any],
) -> Dict[str, List[Tuple[Truck, Order]]]:
    """One candidate set per order over the UNION of fleets eligible to serve it;
    the resulting pairs bucketed by carrier haulier.

    **Why this exists (plan §13.4 FIX-2 / review F4).** Building candidates
    *per company* meant an order visible to ``H`` companies drew up to
    ``per_order × H`` candidate trucks in a SINGLE round, while ``partitioned``
    draws ``per_order`` total — a treatment-correlated difference in candidate
    width, and audit P3 measured that candidate width alone moves mean deadhead
    from 5.25 km to 7.57 km.

    **What this guarantees, precisely (plan §14.9 errata).** ROUND-1 parity, exactly:
    the first round builds the same number of candidate pairs as ``partitioned``
    builds in total. It does **not** give per-tick parity, and the earlier claim that
    "a topology A/B must hold candidate generation fixed" overclaimed. A multi-round
    auction re-indexes over the shrinking free set, so the per-tick total exceeds
    partitioned's by a ratio > 1; that is inherent, and it is **causally inert** —
    review §16.3 shows arm A's cost equals a joint greedy over the POOLED arm's own
    candidate union, exactly, on every seed, so enlarging the candidate set does not
    change what a good allocator achieves. The pooled arm draws *more* candidates and
    still performs worse, so the disparity cannot be manufacturing the effect.
    Freezing the index per tick would make rounds 2+ match against stale free-sets —
    degrading the simulation to flatter a metric.

    Orders are grouped by their eligible-carrier set, and each group gets one
    index over the union of those fleets — exactly mirroring ``partitioned``'s
    per-order-haulier grouping over its eligible truck set.

    Named and shaped for adoption by the audit's ``CANDIDATE_SOURCE`` registry
    (``solver_boundary_audit.md`` P3/P4). This does NOT build that registry.
    """
    by_carrier: Dict[str, List[Tuple[Truck, Order]]] = {}
    if not free_orders:
        return by_carrier

    # Group orders by the exact set of hauliers allowed to carry them.
    groups: Dict[FrozenSet[str], List[Order]] = {}
    for order in free_orders:
        oid = _entity_id(order)
        owner = owner_by_order_id.get(oid)
        if not owner:
            continue
        carriers = market.eligible_carriers(oid, owner)
        groups.setdefault(carriers, []).append(order)

    sp = spatial_params or {}
    # sorted() over a deterministic key: group iteration must not depend on set or
    # dict ordering (I-P5).
    for carriers in sorted(groups, key=lambda c: tuple(sorted(c))):
        group_orders = groups[carriers]
        union_trucks: List[Truck] = []
        for hid in sorted(carriers):
            union_trucks.extend(free_trucks_by_haulier.get(hid) or ())
        if not union_trucks:
            continue

        def _allowed(t: Truck, o: Order) -> bool:
            carrier = assign_constraints.haulier_of(t)
            if not carrier:
                return False
            return pair_allowed_for(carrier)(t, o)

        for truck, order in spatial.iter_candidate_pairs(
            union_trucks,
            group_orders,
            pair_allowed=_allowed,
            cell_deg=sp.get("cell_deg", spatial.DEFAULT_CELL_DEG),
            per_order=sp.get("per_order", spatial.DEFAULT_PER_ORDER_CANDIDATES),
            max_rings=sp.get("max_rings", spatial.DEFAULT_MAX_RINGS),
        ):
            carrier = assign_constraints.haulier_of(truck)
            if carrier:
                by_carrier.setdefault(carrier, []).append((truck, order))
    return by_carrier


def _solve_company(
    trucks: List[Truck],
    orders: List[Order],
    *,
    haulier_id: str,
    rnd: int,
    ctx: PooledPlannerContext,
    prebuilt_pairs: Optional[List[Tuple[Truck, Order]]] = None,
) -> List[Tuple[Truck, Order]]:
    """One company's deployment solve over its free trucks x its visible orders.

    Identical machinery to the legacy path's partition solve — candidates then one
    dispatch decision — just scoped to ONE company instead of a connected component.
    Because the scope and the eligibility set are now the same object, there is no
    need for the legacy ``orders_by_haulier`` regrouping that existed only to stop
    share-ineligible trucks burning the candidate cap (F1).

    The early returns stay AHEAD of building a context and in today's order: the
    sampled path draws from the tick RNG, so deciding on an empty scope would shift
    every later draw (plan ``docs/plans/policy_slots_p0_p3.md`` §7 risk 1).
    """
    if ctx.dispatch.candidate_kind == "spatial":
        # Candidates are built ONCE PER ROUND over the union of eligible fleets
        # (build_round_candidates) and sliced per company, so the pooled arm draws
        # the same candidate width as partitioned (plan §13.4 FIX-2).
        if not prebuilt_pairs:
            return []
    elif not trucks or not orders:
        return []
    company_ctx = ctx.dispatch.company_context(
        trucks=trucks,
        orders=orders,
        haulier_id=haulier_id,
        round=rnd,
        rule=_company_rule(ctx, haulier_id),
        rng=ctx.rng,
        settings=ctx.candidate_settings,
        accounting_cost=ctx.cost,
        clock=ctx.clock,
        prebuilt_pairs=prebuilt_pairs,
    )
    return list(ctx.dispatch.run(company_ctx))


def _company_rule(ctx: PooledPlannerContext, carrier: str):
    """The ``CompanyRule`` for ``carrier``: built by the runner's package, not imported here."""
    return ctx.dispatch.company_rule(carrier, ctx.max_travel_time_pickup)


def run_pooled_market(
    *,
    trucks_by_haulier: Dict[str, List[Truck]],
    orders_by_haulier: Dict[str, List[Order]],
    market: PoolMarket,
    ctx: PooledPlannerContext,
) -> PooledMarketResult:
    """Run steps 4-6 of plan §4.1 for one assignment tick.

    Terminates on CONVERGENCE — when a round commits no award — with
    ``ctx.max_rounds`` as a safety backstop only (plan §13.4 FIX-1). Returns a
    :class:`PooledMarketResult` carrying the assignment, each share's market
    provenance and the round provenance needed to tell whether the backstop bound.
    """
    # Company iteration is ALWAYS sorted: I-P5 forbids any dependence on dict order.
    company_ids = sorted(
        {h for h in trucks_by_haulier if h} | {h for h in orders_by_haulier if h}
    )

    orders_by_id: Dict[str, Order] = {}
    owner_by_order_id: Dict[str, str] = {}
    for hid in sorted(orders_by_haulier):
        if not hid:
            continue
        for order in orders_by_haulier[hid]:
            oid = _entity_id(order)
            orders_by_id[oid] = order
            owner_by_order_id[oid] = hid
    trucks_by_id: Dict[str, Truck] = {
        _entity_id(t): t
        for hid in sorted(trucks_by_haulier)
        if hid
        for t in trucks_by_haulier[hid]
    }
    input_order_ids = frozenset(orders_by_id)

    def _owner_of(order_id: str) -> Optional[str]:
        return owner_by_order_id.get(order_id)

    # --- step 4: contribution (once, before any claiming) --------------------
    if not market.is_empty:
        for hid in company_ids:
            pools = market.pools_for(hid)
            if not pools:
                continue  # a company in no pool contributes nothing, by construction
            own_orders = list(orders_by_haulier.get(hid) or ())
            own_trucks = list(trucks_by_haulier.get(hid) or ())
            if not own_orders:
                continue
            try:
                contribution = ctx.offer.offer(
                    haulier_id=hid,
                    orders=own_orders,
                    trucks=own_trucks,
                    pools=pools,
                    rng=ctx.rng,
                    cost=ctx.cost,
                )
            except Exception:
                # A company policy must never be able to abort the tick.
                logger.exception("Offer policy failed for haulier %r — contributing nothing.", hid)
                continue
            if not isinstance(contribution, dict):
                logger.warning(
                    "Offer policy for %r returned %s, expected a dict — ignoring.",
                    hid, type(contribution).__name__,
                )
                continue
            for order_id in sorted(contribution):
                pool_ids = contribution.get(order_id) or []
                # FRAMEWORK validation (plan §4.1 step 4): the caller must OWN the
                # order. Pool existence + membership are validated by PoolMarket.
                # Violations are dropped with a warning — never raised mid-run.
                if owner_by_order_id.get(str(order_id)) != hid:
                    logger.warning(
                        "Dropping contribution of order %r by %r: not its owner (owner=%r).",
                        order_id, hid, owner_by_order_id.get(str(order_id)),
                    )
                    continue
                market.contribute(str(order_id), hid, [str(p) for p in pool_ids])

    # --- step 5: CONVERGENCE-terminated bidding rounds -----------------------
    # The loop runs until a round commits nothing (convergence) or every order is
    # taken. ``max_rounds`` is a safety backstop, NOT the termination condition
    # (plan §13.4 FIX-1) — see DEFAULT_MAX_ROUNDS above for why the old cap of 2
    # was a treatment-correlated confound.
    max_rounds = max(1, min(MAX_ROUNDS_CEILING, int(ctx.max_rounds or DEFAULT_MAX_ROUNDS)))
    rounds_used = 0
    converged = False
    candidate_pairs_round1 = 0
    candidate_pairs_total = 0
    rules_by_carrier: Dict[str, Any] = {}
    for rnd in range(1, max_rounds + 1):
        if not any(market.is_order_free(oid) for oid in input_order_ids):
            converged = True  # nothing left to allocate: a legitimate convergence
            break
        rounds_used = rnd

        # ONE candidate build per ROUND over the union of eligible fleets, then
        # sliced per company (plan §13.4 FIX-2). Per-company index building was a
        # per_order x H amplification of the candidate set for the cooperating arm.
        free_trucks_by_haulier = {
            h: [t for t in (ts or ()) if market.is_truck_free(_entity_id(t))]
            for h, ts in trucks_by_haulier.items()
            if h
        }
        free_orders = [
            orders_by_id[oid] for oid in sorted(input_order_ids)
            if market.is_order_free(oid)
        ]

        def _pair_allowed_for(carrier: str) -> PairAllowed:
            # Own orders keep the STRICT rule; pooled foreign orders drop the
            # haulier-equality test because visibility already proved eligibility
            # by construction (the coherence fix, plan §0). See CompanyRule.
            # Cached: build_round_candidates asks once per candidate pair.
            rule = rules_by_carrier.get(carrier)
            if rule is None:
                rule = rules_by_carrier[carrier] = _company_rule(ctx, carrier)
            return rule.allowed

        candidates_by_haulier: Dict[str, List[Tuple[Truck, Order]]] = {}
        if ctx.dispatch.candidate_kind == "spatial":
            candidates_by_haulier = build_round_candidates(
                free_trucks_by_haulier=free_trucks_by_haulier,
                free_orders=free_orders,
                market=market,
                owner_by_order_id=owner_by_order_id,
                pair_allowed_for=_pair_allowed_for,
                spatial_params=ctx.candidate_settings.spatial_params(),
            )
            _round_pairs = sum(len(v) for v in candidates_by_haulier.values())
            candidate_pairs_total += _round_pairs
            if rnd == 1:
                candidate_pairs_round1 = _round_pairs

        round_bids = []
        # Private commits COUNT as progress. Missing this was the premature-
        # convergence bug (plan §14.5 R3-7): §4.1 step 5.5 said "break if no bids
        # were produced", which ignores the private-commit path introduced two steps
        # earlier in the same section. A round that commits privately but bids
        # nothing was declared converged, ending the auction early. Latent under the
        # default OfferAll (which contributes everything, so nothing is private) but
        # live under OfferSpare — the configuration experiment E6 is built on.
        private_commits_this_round = 0
        for hid in company_ids:
            own_trucks = free_trucks_by_haulier.get(hid) or []
            if not own_trucks:
                continue

            own_orders = [
                o for o in (orders_by_haulier.get(hid) or ())
                if market.is_order_free(_entity_id(o))
            ]
            # ``visible_pooled_order_ids`` deliberately EXCLUDES this company's own
            # contributions, so own orders are added separately here — a company
            # never loses sight of its own work by contributing it.
            foreign_ids = sorted(market.visible_pooled_order_ids(hid))
            foreign_orders = [
                orders_by_id[oid] for oid in foreign_ids
                if oid in orders_by_id and market.is_order_free(oid)
            ]
            candidate_orders = own_orders + foreign_orders
            if not candidate_orders:
                continue

            try:
                result = _solve_company(
                    own_trucks,
                    candidate_orders,
                    haulier_id=hid,
                    rnd=rnd,
                    ctx=ctx,
                    prebuilt_pairs=candidates_by_haulier.get(hid),
                )
            except Exception:
                # Message 20 with the traceback: that company commits nothing this
                # round (pre-v2 behaviour), counted in the runner's stats.
                ctx.dispatch.report_crash(haulier_id=hid, round=rnd)
                continue

            # THREE buckets, because "is this pair contested?" and "is this pair my
            # own work?" are DIFFERENT questions (plan §13.4 FIX-4 / review F5).
            # Conflating them meant that under the default OfferAll — where every
            # own order is contributed — `own_pairs` was ALWAYS empty, so
            # ClaimIfGainExceeds had no opportunity-cost baseline and `min_gain_km`
            # (the knob driving experiment E6) silently did nothing.
            own_private_pairs: List[Tuple[Truck, Order]] = []      # uncontested by construction
            own_contributed_pairs: List[Tuple[Truck, Order]] = []  # mine, but contestable
            foreign_pairs: List[Tuple[Truck, Order]] = []
            for truck, order in result:
                oid = _entity_id(order)
                if owner_by_order_id.get(oid) == hid:
                    if market.is_contributed(oid):
                        own_contributed_pairs.append((truck, order))
                    else:
                        own_private_pairs.append((truck, order))
                else:
                    foreign_pairs.append((truck, order))
            # Award routing is UNCHANGED: an owner must still bid for its own
            # contributed orders, or a partner takes them by default.
            pooled_pairs = own_contributed_pairs + foreign_pairs

            # Own PRIVATE orders are invisible to every other company, so they are
            # uncontested by construction -> commit immediately, no bid needed.
            #
            # PRECONDITION (plan §13.2, the S3 equivalence proof): direct-committing
            # here is observationally identical to routing these through the bid
            # path ONLY because (1) a company's bids are truck-disjoint, (2) a
            # private order is invisible to everyone else, (3) trucks are
            # company-private, and (4) _sweep awards any bid whose order and truck
            # are free. If a future claim policy ever emits NON-truck-disjoint bids
            # for one company — a ranked-preference policy would — property (1)
            # fails and the two paths stop being equivalent. Revisit this then.
            for truck, order in own_private_pairs:
                private_commits_this_round += market.commit(
                    Award(
                        order_id=_entity_id(order),
                        truck_id=_entity_id(truck),
                        carrier_haulier_id=hid,
                        owner_haulier_id=hid,
                        cost_km=float(ctx.cost(truck, order)),
                        pool_id=None,
                        round=rnd,
                    )
                )

            if not pooled_pairs:
                continue
            try:
                bids = ctx.claim.bids(
                    haulier_id=hid,
                    pooled_pairs=pooled_pairs,
                    # THE FIX: "my own work" — the opportunity-cost baseline — is
                    # every pair on an order I own, contributed or not. The host
                    # supplies the INFORMATION; the policy still decides what it
                    # means (min_gain_km stays entirely inside the policy), so no
                    # policy logic moves into the framework.
                    own_pairs=own_private_pairs + own_contributed_pairs,
                    cost=ctx.cost,
                    market=market,
                    rng=ctx.rng,
                    round=rnd,
                )
            except Exception:
                logger.exception("Claim policy failed for haulier %r — bidding nothing.", hid)
                continue
            round_bids.extend(bids or ())

        if not round_bids and not private_commits_this_round:
            # Converged only when the round committed NOTHING AT ALL.
            converged = True
            break

        awards = ctx.arbitration.resolve(
            round_bids,
            market=market,
            tick_seed=ctx.tick_seed,
            owner_of=_owner_of,
            rng=ctx.rng,
        )
        committed_any = bool(private_commits_this_round)
        for award in awards or ():
            # I-P4 re-asserted at commit: a carrier may only be awarded an order it
            # owns, or one contributed to a pool it belongs to.
            if award.carrier_haulier_id != award.owner_haulier_id:
                pool_id = market.pool_of_order(award.order_id, award.carrier_haulier_id)
                if pool_id is None:
                    logger.error(
                        "I-P4 violation blocked: %r awarded order %r it cannot see.",
                        award.carrier_haulier_id, award.order_id,
                    )
                    continue
            if market.commit(award):
                committed_any = True
        if not committed_any:
            # THE REAL TERMINATION CONDITION: the auction has stopped allocating.
            converged = True
            break

    # --- step 6: the awarded pairs and their market provenance ---------------
    # The share ACCOUNTING is the framework's (assignment/accounting.py, round-2
    # §2.5): it is computed for every solver from the committed pairs alone.
    awards = market.awards
    assignment: List[Tuple[Truck, Order]] = []
    pair_notes: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for award in awards:
        truck = trucks_by_id.get(award.truck_id)
        order = orders_by_id.get(award.order_id)
        if truck is None or order is None:  # pragma: no cover - defensive
            logger.error("Award references unknown truck/order: %r", award)
            continue
        assignment.append((truck, order))
        # A SELF-CLAIM (owner wins its own contributed order) is NOT a share (§D6).
        if award.carrier_haulier_id != award.owner_haulier_id:
            pair_notes[(award.truck_id, award.order_id)] = {
                "pool_id": award.pool_id, "market_round": award.round,
            }

    # --- step 7: invariants (plan §5) ----------------------------------------
    awarded_order_ids = [a.order_id for a in awards]
    awarded_truck_ids = [a.truck_id for a in awards]
    # I-P1: no order invented; anything unawarded is simply still unassigned.
    assert frozenset(awarded_order_ids) <= input_order_ids, "I-P1: awarded an unknown order"
    # I-P2: no order and no truck assigned twice this tick.
    assert len(set(awarded_order_ids)) == len(awarded_order_ids), "I-P2: order awarded twice"
    assert len(set(awarded_truck_ids)) == len(awarded_truck_ids), "I-P2: truck awarded twice"
    # I-P3: ownership is read-only here — every award's owner is the one we read
    # from the input orders, never anything the market or a policy could change.
    for a in awards:
        assert a.owner_haulier_id == owner_by_order_id.get(a.order_id), (
            "I-P3: award owner diverged from the order's owner"
        )

    if not converged:
        # The backstop bound before the auction stopped allocating. This tick
        # under-served, and because serving fewer orders IMPROVES mean deadhead
        # (audit P3) the resulting numbers are biased in favour of whichever arm
        # truncated more. Say so loudly rather than silently truncating.
        logger.warning(
            "Pooled market hit the max_rounds SAFETY BACKSTOP (%d) without converging: "
            "%d order(s) still free. This tick under-served and its throughput/deadhead "
            "numbers are suspect — raise planner.market.max_rounds.",
            max_rounds,
            sum(1 for oid in input_order_ids if market.is_order_free(oid)),
        )

    return PooledMarketResult(
        assignment=assignment,
        rounds_used=rounds_used,
        converged=converged,
        candidate_pairs_round1=candidate_pairs_round1,
        candidate_pairs_total=candidate_pairs_total,
        pair_notes=pair_notes,
    )
