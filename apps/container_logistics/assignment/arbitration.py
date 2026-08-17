"""Arbitration rules — FRAMEWORK-side: *who wins when two companies claim the
same pooled order?*

Shared-pool cooperation plan §1 (the boundary), D2, §5 (I-P2, I-P5), §6.4,
§9 Phase 3. Pure: nothing here is wired into the runtime yet.

This is the invariant-critical module. Two properties are non-negotiable and
are proven by ``tests/test_assignment_market_policies.py``:

**No mutation.** ``resolve`` never calls ``market.commit`` and never writes any
market state. It *reads* ``is_order_free`` / ``is_truck_free`` to skip whatever
the host already committed this tick (e.g. direct awards on private orders).
The host commits the returned awards, so a rejected commit is still the
market's decision, not the rule's.

**Order independence (I-P5).** The returned award list is byte-identical for
any permutation of the input ``bids``. Every ranking below is therefore a
*total* order: each rank key ends with ``(bid_tiebreak(...), order_id,
truck_id, haulier_id)``, so even a blake2b collision cannot let list position
leak into the result. ``RandomAward`` gets there by sorting canonically
*before* shuffling, which makes its output a function of (bid set, rng state)
rather than of caller order.

Scope honesty (plan §5): I-P5 is a per-tick planner guarantee. It does not make
a whole run reproducible — agent scheduling is async.
"""

from __future__ import annotations

import logging
import math
import random
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Type

from .pools import Award, Bid, PoolMarket
from .tiebreak import stable_tiebreak

logger = logging.getLogger(__name__)

OwnerOf = Callable[[str], Optional[str]]


def bid_tiebreak(tick_seed: int, bid: Bid) -> int:
    """Stable 64-bit tiebreak for one bid (plan D2).

    ``blake2b(f"{tick_seed}|{order_id}|{haulier_id}|{truck_id}", digest_size=8)``
    read big-endian. Depends on no dict ordering, no list position and no solve
    order, and rotates per tick so no company gets a systematic edge from a
    fixed hash.
    """
    # One shared construction with the solver's pair tie-break so the two orderings
    # can never drift apart (plan §13.4 FIX-3).
    return stable_tiebreak(tick_seed, bid.order_id, bid.haulier_id, bid.truck_id)


def _identity_key(bid: Bid) -> Tuple[str, str, str]:
    """Final, collision-proof component of every rank key."""
    return (bid.order_id, bid.truck_id, bid.haulier_id)


def is_finite(value: Any) -> bool:
    """True only for a real, comparable number (plan §13.4 FIX-5)."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _cost_rank(bid: Bid) -> Tuple[int, float]:
    """Cost component of a rank key that stays a TOTAL order.

    A non-finite ``cost_km`` (``inf`` from a missing-geometry pair, or a ``nan``
    from a misbehaving policy) is routed to a trailing "unknown cost" class rather
    than entering the comparison. Letting it through defeated the module's own
    order-independence guarantee: a NaN in position 1 of the tuple makes every
    later component unreachable, so the winner flipped with the caller's list
    order (review F10).
    """
    return (0, float(bid.cost_km)) if is_finite(bid.cost_km) else (1, 0.0)


def _sweep(
    ranked: Sequence[Bid],
    *,
    market: PoolMarket,
    owner_of: OwnerOf,
) -> List[Award]:
    """Walk ranked bids, awarding one whose order AND truck are still free —
    free in the ``market`` (committed earlier this tick) and not yet taken by
    an award produced within this same call (I-P2).

    Never mutates ``market``.
    """
    awards: List[Award] = []
    taken_orders: Set[str] = set()
    taken_trucks: Set[str] = set()
    for bid in ranked:
        if bid.order_id in taken_orders or bid.truck_id in taken_trucks:
            continue
        if not market.is_order_free(bid.order_id) or not market.is_truck_free(bid.truck_id):
            continue
        awards.append(
            Award(
                order_id=bid.order_id,
                truck_id=bid.truck_id,
                carrier_haulier_id=bid.haulier_id,
                owner_haulier_id=owner_of(bid.order_id),
                cost_km=bid.cost_km,
                pool_id=bid.pool_id,
                round=bid.round,
            )
        )
        taken_orders.add(bid.order_id)
        taken_trucks.add(bid.truck_id)
    return awards


class BaseArbitrationRule(ABC):
    """Contract for a world decision: rank the round's bids, sweep, return awards.

    Implementations MUST NOT mutate ``market`` and MUST be order-independent
    (see the module docstring).
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self._params = params or {}

    @property
    def params(self) -> Dict[str, Any]:
        return self._params

    @abstractmethod
    def resolve(
        self,
        bids: Sequence[Bid],
        *,
        market: PoolMarket,
        tick_seed: int,
        owner_of: OwnerOf,
        rng: random.Random,
    ) -> List[Award]:
        raise NotImplementedError


class LowestCostRule(BaseArbitrationRule):
    """Default. Rank by ``(cost_km asc, tiebreak asc, identity)`` and sweep.

    Anonymous (no company identity in the rule) and welfare-maximising given
    the declared costs — which is exactly the allocation the research question
    asks us to measure (plan D2).
    """

    def resolve(
        self,
        bids: Sequence[Bid],
        *,
        market: PoolMarket,
        tick_seed: int,
        owner_of: OwnerOf,
        rng: random.Random,
    ) -> List[Award]:
        ranked = sorted(
            bids,
            key=lambda b: _cost_rank(b) + (bid_tiebreak(tick_seed, b),) + _identity_key(b),
        )
        return _sweep(ranked, market=market, owner_of=owner_of)


class OwnerFirstRule(BaseArbitrationRule):
    """The order owner's own bid beats any partner bid; otherwise LowestCost.

    Rank key: ``(0 if bid.haulier_id == owner_of(bid.order_id) else 1, cost_km,
    tiebreak, identity)``.

    Note the key makes owner-preference **global**, not per-order: every
    owner-bid outranks every partner-bid across the whole round, so an owner
    bid can also win a truck that a cheaper partner bid on for a *different*
    order. For the semantics the plan states — a single contested order — this
    is identical, and it is the implementation the phase brief specifies.
    Models a company that will not cede a job it can do itself.
    """

    def resolve(
        self,
        bids: Sequence[Bid],
        *,
        market: PoolMarket,
        tick_seed: int,
        owner_of: OwnerOf,
        rng: random.Random,
    ) -> List[Award]:
        def key(b: Bid):
            is_owner = 0 if b.haulier_id == owner_of(b.order_id) else 1
            return (is_owner,) + _cost_rank(b) + (bid_tiebreak(tick_seed, b),) + _identity_key(b)

        return _sweep(sorted(bids, key=key), market=market, owner_of=owner_of)


class HighestBenefitRule(BaseArbitrationRule):
    """Rank by ``owner_reserve_km - cost_km`` **descending** — "share only where
    it helps the owner".

    **Interpretation (the plan does not specify this; it is an explicit choice
    made here).** Plan D6 defines ``owner_reserve_km`` as a shadow price over
    the owner's still-free trucks, which is computed by the pooled planner host
    *after* the final commit. Arbitration sees only bids, so it cannot compute
    that. Here, therefore:

    - ``owner_reserve_km(order)`` = the **minimum ``cost_km`` among the bids
      submitted by that order's owner for that order in this call**;
    - when the owner submitted no bid for the order, the benefit is
      **undefined**; such bids rank AFTER every defined-benefit bid, among
      themselves by ``(cost_km asc, tiebreak asc)``.

    Consequence to be aware of when reading results: this rule's notion of
    benefit is *revealed* (what the owner actually bid), while the
    ``benefit_km`` recorded for analytics stays the policy-independent shadow
    price of D6. The two numbers can disagree, deliberately.
    """

    def resolve(
        self,
        bids: Sequence[Bid],
        *,
        market: PoolMarket,
        tick_seed: int,
        owner_of: OwnerOf,
        rng: random.Random,
    ) -> List[Award]:
        reserve: Dict[str, float] = {}
        for b in bids:
            if b.haulier_id == owner_of(b.order_id) and is_finite(b.cost_km):
                current = reserve.get(b.order_id)
                if current is None or b.cost_km < current:
                    reserve[b.order_id] = b.cost_km

        def key(b: Bid):
            own_reserve = reserve.get(b.order_id)
            benefit = None
            if own_reserve is not None and is_finite(b.cost_km):
                candidate = own_reserve - float(b.cost_km)
                if is_finite(candidate):
                    benefit = candidate
            if benefit is None:
                # Undefined benefit (owner never bid, or a non-finite cost would
                # make the key NaN and destroy the total order — F10): rank after
                # every defined-benefit bid, cost asc.
                return (1,) + _cost_rank(b) + (bid_tiebreak(tick_seed, b),) + _identity_key(b)
            # Defined: benefit desc == negated benefit asc.
            return (0, -benefit, 0.0, bid_tiebreak(tick_seed, b)) + _identity_key(b)

        return _sweep(sorted(bids, key=key), market=market, owner_of=owner_of)


class RandomAwardRule(BaseArbitrationRule):
    """Null control: an rng-shuffled sweep, isolating the arbitration effect.

    To stay order-independent (I-P5) the bids are first sorted **canonically**
    by ``(tiebreak, order_id, truck_id, haulier_id)`` and only then shuffled, so
    the outcome is a function of the bid *set* plus the rng state — never of
    the caller's list order.
    """

    def resolve(
        self,
        bids: Sequence[Bid],
        *,
        market: PoolMarket,
        tick_seed: int,
        owner_of: OwnerOf,
        rng: random.Random,
    ) -> List[Award]:
        canonical = sorted(bids, key=lambda b: (bid_tiebreak(tick_seed, b),) + _identity_key(b))
        rng.shuffle(canonical)
        return _sweep(canonical, market=market, owner_of=owner_of)


# Plug-and-play registry — mirrors ``solver/__init__.py::SOLVER_REGISTRY``.
# Keep keys stable: they are persisted in scenario behaviours (planner.market).
ARBITRATION_REGISTRY: Dict[str, Type[BaseArbitrationRule]] = {
    "LowestCost": LowestCostRule,
    "OwnerFirst": OwnerFirstRule,
    "HighestBenefit": HighestBenefitRule,
    "RandomAward": RandomAwardRule,
}

DEFAULT_ARBITRATION_RULE = "LowestCost"


def get_arbitration_rule(
    name: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> BaseArbitrationRule:
    """Instantiate an arbitration rule by name, falling back to the default.

    Fail-soft (plan §7): an unknown/missing name logs and runs the default
    rather than crashing the assignment agent mid-run.
    """
    key = name or DEFAULT_ARBITRATION_RULE
    rule_cls = ARBITRATION_REGISTRY.get(key)
    if rule_cls is None:
        logger.warning(
            "Unknown arbitration rule %r — falling back to %s. Known: %s",
            name,
            DEFAULT_ARBITRATION_RULE,
            ", ".join(sorted(ARBITRATION_REGISTRY)),
        )
        rule_cls = ARBITRATION_REGISTRY[DEFAULT_ARBITRATION_RULE]
    return rule_cls(params=params or {})


__all__ = [
    "bid_tiebreak",
    "BaseArbitrationRule",
    "LowestCostRule",
    "OwnerFirstRule",
    "HighestBenefitRule",
    "RandomAwardRule",
    "ARBITRATION_REGISTRY",
    "DEFAULT_ARBITRATION_RULE",
    "get_arbitration_rule",
]
