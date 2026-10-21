"""Cross-company share accounting: one computation, two historical wire formats.

Single responsibility: given the pairs a tick commits, compute every cross-company
share's values (who owned the order, who carried it, the owner's best own
alternative, the awarded cost) and render them as the share tags ``publish()``
reads. Plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.5. The values are
SOLVER-INDEPENDENT: no solver input reaches :func:`account_shares`, so every dispatch
policy is accounted by the same function.

- :func:`account_shares` -> one :class:`ShareAccount` per cross-company pair.
- :func:`render_share_tags` -> ``{(truck_id, order_id): tag}`` in one of two formats:
  ``v1`` (4 keys, the pre-v2 partitioned format, kept for byte identity and reserved
  to ``Partitioned`` at registration) and ``audited`` (the 8-key pooled format whose
  ``benefit_km == owner_reserve_km - awarded_cost_km`` is checkable).

:func:`owner_reserve_km` and :func:`finite_or_none` were moved here from
``pooled_planner`` (round 2): the market engine no longer accounts.

Framework side of the boundary. Import contract (round-2 §4): imports only
``assignment.constraints``, so ``assignment.app`` may import it at module top.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping, Optional, Sequence, Set

from . import constraints as assign_constraints

logger = logging.getLogger(__name__)

__all__ = [
    "PAIR_NOTE_KEYS",
    "ShareAccount",
    "account_shares",
    "finite_or_none",
    "owner_reserve_km",
    "render_share_tags",
]

Truck = dict[str, Any]
Order = dict[str, Any]
Pair = tuple[Truck, Order]

#: The only ``pair_notes`` keys a solver may contribute to an ``audited`` tag (market
#: provenance). Everything else in a tag is computed here.
PAIR_NOTE_KEYS = ("pool_id", "market_round")


@dataclass(frozen=True)
class ShareAccount:
    """One cross-company share: ``carrier_haulier_id``'s truck serves ``owner_haulier_id``'s order.

    ``owner_reserve_km`` is the owner's cheapest own truck still free after the tick's
    commits (``None``: it had none); ``awarded_cost_km`` is ``float(cost(truck,
    order))`` with a raising cost read as ``inf``. Both are raw: rendering decides
    rounding and the non-finite rule.
    """

    truck_id: str
    order_id: str
    owner_haulier_id: str
    carrier_haulier_id: str
    owner_reserve_km: float | None
    awarded_cost_km: float


def owner_reserve_km(
    order: Order,
    owner_trucks: Sequence[Truck],
    committed_truck_ids: Set[str],
    *,
    pair_allowed: Callable[[Truck, Order], bool],
    cost: Callable[[Truck, Order], float],
) -> Optional[float]:
    """The owner's cheapest OWN truck still free after this tick's final commit.

    This is the *shadow price* half of ``benefit_km`` (plan §D6) and deliberately
    keeps the pre-pool definition so the metric stays comparable across policies and
    across pre-pool runs: it is the realizable alternative the owner still had, NOT
    the owner's declared bid (which would be policy-dependent — a company running
    ``ClaimNone`` would report zero benefit for jobs that genuinely saved it
    distance).

    Scans the owner's FULL fleet, not the capped spatial candidate list, so a truck
    crowded out of the candidate cap cannot fabricate a phantom "unserveable"
    (regression F2). ``None`` means the owner truly had no free feasible truck.
    """
    best: Optional[float] = None
    for truck in owner_trucks:
        if str(truck.get("_id")) in committed_truck_ids:
            continue
        if not pair_allowed(truck, order):
            continue
        c = cost(truck, order)
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c == float("inf") or c != c:  # inf / nan
            continue
        if best is None or c < best:
            best = c
    return best


def finite_or_none(value: Any) -> Optional[float]:
    """A real number, or ``None``. THE rule at the tag boundary (plan §13.4 FIX-5).

    ``GreedyNearestSolver`` deliberately assigns unknown-cost pairs rather than
    crashing, so ``inf`` reaching an award is designed behaviour — but
    ``json.dumps`` renders it as the bare token ``Infinity``, which is not valid
    RFC 8259 and is a ``SyntaxError`` for every JS consumer (the KafkaJS consumer
    and the Next.js SSE hub). Python's ``json.loads`` tolerates it, so the truck
    agent survived and the defect was invisible from the Python side (review F6).
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _awarded_cost_km(cost: Callable[[Truck, Order], float], truck: Truck, order: Order) -> float:
    """``float(cost(truck, order))``; a raising cost is ``inf`` (as ``claim._cost_km`` bids it)."""
    try:
        return float(cost(truck, order))
    except Exception:  # a cost fn must not break the accounting of a committed pair
        logger.exception("share accounting: cost raised for truck %r / order %r; awarded cost is inf",
                         truck.get("_id"), order.get("_id"))
        return float("inf")


def _trucks_by_haulier(candidates: Iterable[Truck]) -> dict[str, list[Truck]]:
    """The candidates grouped by haulier, in input order; haulier-less trucks dropped."""
    out: dict[str, list[Truck]] = {}
    for truck in candidates:
        hid = assign_constraints.haulier_of(truck)
        if hid:
            out.setdefault(hid, []).append(truck)
    return out


def account_shares(
    pairs: Sequence[Pair],
    *,
    candidates: Iterable[Truck],
    cost: Callable[[Truck, Order], float],
    own_allowed: Callable[[Truck, Order], bool],
) -> list[ShareAccount]:
    """One :class:`ShareAccount` per committed cross-company pair, in ``pairs`` order.

    A pair is cross-company when both hauliers are known and differ (a self-serve is
    not a share). The owner reserve scans the owner's trucks in ``candidates`` that are
    not committed by ANY pair of the tick and pass ``own_allowed``, priced by ``cost``
    (:func:`owner_reserve_km`). The owner index is built only when the first cross
    pair appears, so a tick without shares costs one pass over ``pairs``.
    """
    committed = {str(truck.get("_id")) for truck, _order in pairs}
    owner_index: dict[str, list[Truck]] | None = None
    accounts: list[ShareAccount] = []
    for truck, order in pairs:
        carrier = assign_constraints.haulier_of(truck)
        owner = assign_constraints.haulier_of(order)
        if not (carrier and owner and carrier != owner):
            continue
        if owner_index is None:
            owner_index = _trucks_by_haulier(candidates)
        reserve = owner_reserve_km(order, owner_index.get(owner, ()), committed,
                                   pair_allowed=own_allowed, cost=cost)
        accounts.append(ShareAccount(
            truck_id=str(truck.get("_id")),
            order_id=str(order.get("_id")),
            owner_haulier_id=owner,
            carrier_haulier_id=carrier,
            owner_reserve_km=reserve,
            awarded_cost_km=_awarded_cost_km(cost, truck, order),
        ))
    return accounts


def _v1_tag(acc: ShareAccount) -> dict[str, Any]:
    """The pre-v2 partitioned tag: 4 keys; a non-finite benefit stays +-inf (FIX-5 defect, kept)."""
    reserve = acc.owner_reserve_km
    return {
        "shared": True,
        "owner_haulier_id": acc.owner_haulier_id,
        "carrier_haulier_id": acc.carrier_haulier_id,
        "benefit_km": round(reserve - acc.awarded_cost_km, 3) if reserve is not None else None,
    }


def _audited_tag(acc: ShareAccount, note: Mapping[str, Any]) -> dict[str, Any]:
    """The pooled tag: 8 keys, no non-finite number, ``benefit = reserve - awarded``."""
    reserve = acc.owner_reserve_km
    awarded = finite_or_none(acc.awarded_cost_km)
    benefit = None
    if reserve is not None and awarded is not None:
        benefit = finite_or_none(reserve - awarded)
    return {
        "shared": True,
        "owner_haulier_id": acc.owner_haulier_id,
        "carrier_haulier_id": acc.carrier_haulier_id,
        # SIGNED on purpose (F3): a cost-blind or cap-limited solver can genuinely pick
        # a worse-than-own partner truck. None = no free feasible own truck.
        "benefit_km": round(benefit, 3) if benefit is not None else None,
        "pool_id": note.get("pool_id"),
        "awarded_cost_km": round(awarded, 3) if awarded is not None else None,
        "owner_reserve_km": round(reserve, 3) if reserve is not None else None,
        "market_round": note.get("market_round"),
    }


def render_share_tags(
    accounts: Iterable[ShareAccount],
    *,
    fmt: Literal["v1", "audited"],
    pair_notes: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(truck_id, order_id): tag}`` for ``accounts``, in their order.

    ``fmt="v1"``: ``{shared, owner_haulier_id, carrier_haulier_id, benefit_km}``.
    ``fmt="audited"``: those plus ``pool_id``, ``awarded_cost_km``,
    ``owner_reserve_km`` and ``market_round``; ``pool_id``/``market_round`` come only
    from the :data:`PAIR_NOTE_KEYS` of ``pair_notes[(truck_id, order_id)]`` (``None``
    when absent). ``pair_notes`` is ignored for ``v1``. Raises ``ValueError`` for
    another ``fmt``.
    """
    if fmt not in ("v1", "audited"):
        raise ValueError(f"share tag format must be 'v1' or 'audited' (got {fmt!r}).")
    notes = pair_notes or {}
    tags: dict[tuple[str, str], dict[str, Any]] = {}
    for acc in accounts:
        key = (acc.truck_id, acc.order_id)
        if fmt == "v1":
            tags[key] = _v1_tag(acc)
        else:
            raw = notes.get(key) or {}
            tags[key] = _audited_tag(acc, {k: raw[k] for k in PAIR_NOTE_KEYS if k in raw})
    return tags
