"""Offer policies — SOLVER-side: *which of my own orders do I contribute, and
to which pools?*

Shared-pool cooperation plan §1 (the boundary), §6.2, §9 Phase 3. Pure: nothing
here is wired into the runtime yet. An offer policy models a single company's
decision, so it may only ever read that company's own data (its orders, its
trucks, the pools it belongs to) — never another company's, and never
allocation state.

The framework (``PoolMarket.contribute``) re-validates every placement and
drops invalid ones with a warning, so a policy returning a bad pool id degrades
a tick rather than aborting a run.
"""

from __future__ import annotations

import logging
import math
import random
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

from ..constraints import haulier_of
from ..pools import Pool
from ..solver.base import Cost, Order, Truck

logger = logging.getLogger(__name__)


class BaseOfferPolicy(ABC):
    """Contract for a company's contribution decision.

    ``offer`` returns ``{order_id: [pool_id, ...]}``. A missing key, or an
    empty list, means the order stays **private** (never enters a pool, so no
    other company can see it). Ownership is never transferred by contributing
    — see plan D4.
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self._params = params or {}

    @property
    def params(self) -> Dict[str, Any]:
        return self._params

    @abstractmethod
    def offer(
        self,
        *,
        haulier_id: str,
        orders: List[Order],
        trucks: List[Truck],
        pools: Tuple[Pool, ...],
        rng: random.Random,
        cost: Cost,
    ) -> Dict[str, List[str]]:
        """``order_id`` -> pool ids to contribute it to.

        ``orders``/``trucks`` are this company's own (uncommitted) orders and
        free trucks; ``pools`` is the tuple of pools this company belongs to.
        """
        raise NotImplementedError


def _own_orders(haulier_id: str, orders: Sequence[Order]) -> List[Tuple[str, Order]]:
    """``(order_id, order)`` for the orders this company may contribute.

    An order is skipped when it has no usable ``_id``, or when it carries an
    explicit haulier id that is *not* ``haulier_id``. A missing haulier id is
    treated as own (fail-open) because the host only ever passes a company its
    own order list; the explicit-mismatch check is a defensive backstop, not
    the ownership source of truth (that is ``haulier_of`` at the host).
    """
    out: List[Tuple[str, Order]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        oid = order.get("_id")
        if oid in (None, ""):
            continue
        owner = haulier_of(order)
        if owner is not None and owner != haulier_id:
            continue
        out.append((str(oid), order))
    return out


def _cheapest_feasible_cost(order: Order, trucks: Sequence[Truck], cost: Cost) -> float:
    """Cost of this company's cheapest feasible free truck for ``order``.

    ``inf`` when the company has no free truck that can serve it at all
    (``cost(...) == inf`` counts as infeasible, matching the solver contract in
    ``solver/base.py``).
    """
    best = math.inf
    for truck in trucks:
        try:
            c = float(cost(truck, order))
        except Exception:  # pragma: no cover - a cost fn must not break offering
            continue
        if c < best:
            best = c
    return best


class OfferAllPolicy(BaseOfferPolicy):
    """Default. Every own order into every own pool.

    This is the policy under which an edge-derived set of 2-member pools has
    exactly today's edge-direct eligibility (plan D1).
    """

    def offer(
        self,
        *,
        haulier_id: str,
        orders: List[Order],
        trucks: List[Truck],
        pools: Tuple[Pool, ...],
        rng: random.Random,
        cost: Cost,
    ) -> Dict[str, List[str]]:
        pool_ids = sorted(p.id for p in pools)
        if not pool_ids:
            return {}
        return {oid: list(pool_ids) for oid, _order in _own_orders(haulier_id, orders)}


class OfferNonePolicy(BaseOfferPolicy):
    """Control: contribute nothing. Reproduces no-cooperation while the pooled
    topology is still active — every order stays private, so no pooled bid can
    ever be made against it."""

    def offer(
        self,
        *,
        haulier_id: str,
        orders: List[Order],
        trucks: List[Truck],
        pools: Tuple[Pool, ...],
        rng: random.Random,
        cost: Cost,
    ) -> Dict[str, List[str]]:
        return {}


class OfferSparePolicy(BaseOfferPolicy):
    """Contribute only what the company cannot serve well itself.

    Params:
      - ``keep_below_km`` (default ``inf``): an order is offered when the
        company's cheapest feasible free truck for it costs **more than** this.
      - ``only_when_short`` (default ``True``): when the company has strictly
        fewer free trucks than orders, every own order is offered.

    An order with **no** feasible free truck (every ``cost`` is ``inf``) is
    always offered — the company demonstrably cannot serve it. This is an
    explicit reading, since ``inf > inf`` is false and the bare predicate would
    otherwise keep an unservable order private.

    Honest scope note: with the defaults this equals :class:`OfferAllPolicy`
    only while the company is *short* (``len(trucks) < len(orders)``), which is
    the normal state of a loaded sim. With ``len(trucks) >= len(orders)`` and
    the default ``keep_below_km = inf`` it offers only the orders it cannot
    serve at all. Plan §12.4 already flags this predicate as under-specified
    and "to be reviewed with the user, not settled design".
    """

    def offer(
        self,
        *,
        haulier_id: str,
        orders: List[Order],
        trucks: List[Truck],
        pools: Tuple[Pool, ...],
        rng: random.Random,
        cost: Cost,
    ) -> Dict[str, List[str]]:
        pool_ids = sorted(p.id for p in pools)
        if not pool_ids:
            return {}

        own = _own_orders(haulier_id, orders)
        keep_below_km = self._params.get("keep_below_km", math.inf)
        try:
            keep_below_km = float(keep_below_km)
        except (TypeError, ValueError):
            logger.warning(
                "OfferSpare: bad keep_below_km %r — using inf", self._params.get("keep_below_km")
            )
            keep_below_km = math.inf
        only_when_short = bool(self._params.get("only_when_short", True))
        short = only_when_short and len(trucks) < len(own)

        out: Dict[str, List[str]] = {}
        for oid, order in own:
            if short:
                out[oid] = list(pool_ids)
                continue
            best = _cheapest_feasible_cost(order, trucks, cost)
            if best == math.inf or best > keep_below_km:
                out[oid] = list(pool_ids)
        return out


# Plug-and-play registry — mirrors ``solver/__init__.py::SOLVER_REGISTRY``.
# Keep keys stable: they are persisted in scenario behaviours (planner.market).
OFFER_REGISTRY: Dict[str, Type[BaseOfferPolicy]] = {
    "OfferAll": OfferAllPolicy,
    "OfferNone": OfferNonePolicy,
    "OfferSpare": OfferSparePolicy,
}

DEFAULT_OFFER_POLICY = "OfferAll"


def get_offer_policy(
    name: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> BaseOfferPolicy:
    """Instantiate an offer policy by name, falling back to the default.

    Fail-soft (plan §7): an unknown/missing name logs and runs the default
    rather than crashing the assignment agent mid-run.
    """
    key = name or DEFAULT_OFFER_POLICY
    policy_cls = OFFER_REGISTRY.get(key)
    if policy_cls is None:
        logger.warning(
            "Unknown offer policy %r — falling back to %s. Known: %s",
            name,
            DEFAULT_OFFER_POLICY,
            ", ".join(sorted(OFFER_REGISTRY)),
        )
        policy_cls = OFFER_REGISTRY[DEFAULT_OFFER_POLICY]
    return policy_cls(params=params or {})


__all__ = [
    "BaseOfferPolicy",
    "OfferAllPolicy",
    "OfferNonePolicy",
    "OfferSparePolicy",
    "OFFER_REGISTRY",
    "DEFAULT_OFFER_POLICY",
    "get_offer_policy",
]
