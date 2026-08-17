"""Claim policies — SOLVER-side: *which visible pooled orders do I actually bid
on, and at what declared cost?*

Shared-pool cooperation plan §1 (the boundary), §6.3, §9 Phase 3. Pure: nothing
here is wired into the runtime yet.

The company planner has already run for this round and produced a truck-disjoint
tentative allocation; the host splits it into ``own_pairs`` (pairs on the
company's own private orders, which are awarded directly and are never
contested) and ``pooled_pairs`` (pairs on pooled orders — foreign, or own but
contributed). A claim policy turns ``pooled_pairs`` into the subset it wants to
bid on. It is a *company* decision, so it may only read that company's own
data: its own pairs, its own cost function, and pool membership.
"""

from __future__ import annotations

import logging
import math
import random
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

from ..pools import Bid, PoolMarket
from ..solver.base import Cost, Order, Truck

logger = logging.getLogger(__name__)


class BaseClaimPolicy(ABC):
    """Contract for a company's bidding decision.

    Returns a list of :class:`~apps.container_logistics.assignment.pools.Bid`.
    Bids are declared at the company's own cost for the pair; arbitration
    (framework) decides who wins.
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self._params = params or {}

    @property
    def params(self) -> Dict[str, Any]:
        return self._params

    @abstractmethod
    def bids(
        self,
        *,
        haulier_id: str,
        pooled_pairs: List[Tuple[Truck, Order]],
        own_pairs: List[Tuple[Truck, Order]],
        cost: Cost,
        market: PoolMarket,
        rng: random.Random,
        round: int,
    ) -> List[Bid]:
        raise NotImplementedError


def _ids(truck: Truck, order: Order) -> Optional[Tuple[str, str]]:
    """``(truck_id, order_id)``, or ``None`` when either id is unusable."""
    tid = truck.get("_id") if isinstance(truck, dict) else None
    oid = order.get("_id") if isinstance(order, dict) else None
    if tid in (None, "") or oid in (None, ""):
        return None
    return str(tid), str(oid)


def _cost_km(cost: Cost, truck: Truck, order: Order) -> float:
    try:
        return float(cost(truck, order))
    except Exception:  # pragma: no cover - a cost fn must not break bidding
        return math.inf


class ClaimAllPlannedPolicy(BaseClaimPolicy):
    """Default. One bid per pooled pair the planner chose, at the company's own
    ``cost(truck, order)``. The pool id is looked up as the pool through which
    *this* company can see the order (``market.pool_of_order``)."""

    def bids(
        self,
        *,
        haulier_id: str,
        pooled_pairs: List[Tuple[Truck, Order]],
        own_pairs: List[Tuple[Truck, Order]],
        cost: Cost,
        market: PoolMarket,
        rng: random.Random,
        round: int,
    ) -> List[Bid]:
        out: List[Bid] = []
        for truck, order in pooled_pairs:
            ids = _ids(truck, order)
            if ids is None:
                continue
            truck_id, order_id = ids
            out.append(
                Bid(
                    haulier_id=haulier_id,
                    order_id=order_id,
                    truck_id=truck_id,
                    cost_km=_cost_km(cost, truck, order),
                    pool_id=market.pool_of_order(order_id, haulier_id),
                    round=round,
                )
            )
        return out


class ClaimNonePolicy(BaseClaimPolicy):
    """Control: never claim a pooled order (own contributed orders included).
    A company running this still serves its own *private* orders — those are
    direct awards and never reach a claim policy."""

    def bids(
        self,
        *,
        haulier_id: str,
        pooled_pairs: List[Tuple[Truck, Order]],
        own_pairs: List[Tuple[Truck, Order]],
        cost: Cost,
        market: PoolMarket,
        rng: random.Random,
        round: int,
    ) -> List[Bid]:
        return []


class ClaimIfGainExceedsPolicy(BaseClaimPolicy):
    """Bid on a pooled pair only when it beats the best OWN order the company
    could give that same truck by at least ``params['min_gain_km']``
    (default ``0.0``). Drives experiment E6.

    Locally computable by construction — it reads only this company's own
    planned pairs and its own cost function, never another company's data and
    never allocation state.

    **Interpretation (the plan does not pin this down; this is an explicit
    choice made here):**

    - "best own alternative for truck ``T``" is
      ``min(cost(T, o) for (_t, o) in own_pairs)`` — i.e. the pooled pair's own
      truck ``T`` re-costed against every own order the planner scheduled this
      round, *ignoring* which truck the planner actually gave those orders to.
      It answers "what else could I do with this truck right now?".
    - the gain of a pooled pair ``(T, p)`` is
      ``best_own_alternative(T) - cost(T, p)``; the pair is bid when
      ``gain >= min_gain_km``.
    - when ``own_pairs`` is **empty** there is no own alternative, so the gain
      is unbounded (``+inf``) and the pair is **always** bid.
    """

    def bids(
        self,
        *,
        haulier_id: str,
        pooled_pairs: List[Tuple[Truck, Order]],
        own_pairs: List[Tuple[Truck, Order]],
        cost: Cost,
        market: PoolMarket,
        rng: random.Random,
        round: int,
    ) -> List[Bid]:
        min_gain_km = self._params.get("min_gain_km", 0.0)
        try:
            min_gain_km = float(min_gain_km)
        except (TypeError, ValueError):
            logger.warning(
                "ClaimIfGainExceeds: bad min_gain_km %r — using 0.0",
                self._params.get("min_gain_km"),
            )
            min_gain_km = 0.0

        own_orders: List[Order] = [order for _truck, order in own_pairs]

        out: List[Bid] = []
        for truck, order in pooled_pairs:
            ids = _ids(truck, order)
            if ids is None:
                continue
            truck_id, order_id = ids
            pooled_cost = _cost_km(cost, truck, order)

            if own_orders:
                best_own = math.inf
                for own_order in own_orders:
                    c = _cost_km(cost, truck, own_order)
                    if c < best_own:
                        best_own = c
                gain = best_own - pooled_cost
            else:
                gain = math.inf

            if gain < min_gain_km:
                continue
            out.append(
                Bid(
                    haulier_id=haulier_id,
                    order_id=order_id,
                    truck_id=truck_id,
                    cost_km=pooled_cost,
                    pool_id=market.pool_of_order(order_id, haulier_id),
                    round=round,
                )
            )
        return out


# Plug-and-play registry — mirrors ``solver/__init__.py::SOLVER_REGISTRY``.
# Keep keys stable: they are persisted in scenario behaviours (planner.market).
CLAIM_REGISTRY: Dict[str, Type[BaseClaimPolicy]] = {
    "ClaimAllPlanned": ClaimAllPlannedPolicy,
    "ClaimNone": ClaimNonePolicy,
    "ClaimIfGainExceeds": ClaimIfGainExceedsPolicy,
}

DEFAULT_CLAIM_POLICY = "ClaimAllPlanned"


def get_claim_policy(
    name: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> BaseClaimPolicy:
    """Instantiate a claim policy by name, falling back to the default.

    Fail-soft (plan §7): an unknown/missing name logs and runs the default
    rather than crashing the assignment agent mid-run.
    """
    key = name or DEFAULT_CLAIM_POLICY
    policy_cls = CLAIM_REGISTRY.get(key)
    if policy_cls is None:
        logger.warning(
            "Unknown claim policy %r — falling back to %s. Known: %s",
            name,
            DEFAULT_CLAIM_POLICY,
            ", ".join(sorted(CLAIM_REGISTRY)),
        )
        policy_cls = CLAIM_REGISTRY[DEFAULT_CLAIM_POLICY]
    return policy_cls(params=params or {})


__all__ = [
    "BaseClaimPolicy",
    "ClaimAllPlannedPolicy",
    "ClaimNonePolicy",
    "ClaimIfGainExceedsPolicy",
    "CLAIM_REGISTRY",
    "DEFAULT_CLAIM_POLICY",
    "get_claim_policy",
]
