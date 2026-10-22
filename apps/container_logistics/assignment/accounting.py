"""Cross-company share accounting: one computation, rendered by the compat wire formats.

Single responsibility: given the pairs a tick commits, compute every cross-company
share's values (who owned the order, who carried it, the owner's best own
alternative, the awarded cost) and render them as the share tags ``publish()``
reads. Plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.5. The values are
SOLVER-INDEPENDENT: no solver input reaches :func:`account_shares`, so every dispatch
policy is accounted by the same function.

- :func:`account_shares` -> one :class:`ShareAccount` per cross-company pair.
- :func:`render_share_tags` -> ``{(truck_id, order_id): tag}``. A dispatcher: the two
  historical tag formats, and the routing of a solver's opaque ``solver_notes`` into
  them, live in ``policies.compat.wire`` (the only place that names old solver
  provenance). Nothing here knows a format name or a note key.

:func:`owner_reserve_km` and :func:`finite_or_none` were moved here from
``pooled_planner`` (round 2): the solver no longer accounts.

Framework side of the boundary. Import contract (round-2 §4): imports only
``assignment.constraints`` and the leaf ``policies.compat.wire`` (which imports no
``assignment`` module at top), so ``assignment.app`` may import it at module top.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Set

from apps.container_logistics.policies.compat.wire import share_tag_renderer

from . import constraints as assign_constraints

logger = logging.getLogger(__name__)

__all__ = [
    "ShareAccount",
    "account_shares",
    "finite_or_none",
    "owner_reserve_km",
    "render_share_tags",
]

Truck = dict[str, Any]
Order = dict[str, Any]
Pair = tuple[Truck, Order]

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


def _free_trucks_by_haulier(candidates: Iterable[Truck], committed: Set[str]) -> dict[str, list[Truck]]:
    """The candidates NOT in ``committed``, grouped by haulier, in input order; haulier-less trucks dropped.

    Filtering once here, instead of per cross pair inside :func:`owner_reserve_km`,
    is what keeps the reserve scan proportional to the owners' FREE fleets (review C3
    #2: on the golden pooled tick every owner truck is committed, and the per-pair
    re-filter of ~100 trucks x 165 shares was most of the accounting time).
    """
    out: dict[str, list[Truck]] = {}
    for truck in candidates:
        if str(truck.get("_id")) in committed:
            continue
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
    (:func:`owner_reserve_km`). The owner index (free trucks only) is built only when
    the first cross pair appears, so a tick without shares costs one pass over
    ``pairs``. Each ``(truck, order)`` is priced at most once per call: a reserve scan
    prices the owner's trucks for the share's order, the award prices the carrier's
    truck, and no two shares carry the same order.
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
            owner_index = _free_trucks_by_haulier(candidates, committed)
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


def render_share_tags(
    accounts: Iterable[ShareAccount],
    *,
    fmt: str,
    solver_notes: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(truck_id, order_id): tag}`` for ``accounts``, in their order.

    ``fmt`` names a share-tag format; ``solver_notes[(truck_id, order_id)]`` is the
    solver's opaque note for that pair. Both are handed to
    ``compat.wire.share_tag_renderer``, which owns the formats, routes the legacy note
    keys to their old tag keys and every other key to a ``solver_notes`` tag key (omitted
    when empty). A note can never overwrite a computed value. Raises ``ValueError`` for
    an unknown ``fmt``.
    """
    render = share_tag_renderer(fmt)
    notes = solver_notes or {}
    tags: dict[tuple[str, str], dict[str, Any]] = {}
    for acc in accounts:
        key = (acc.truck_id, acc.order_id)
        tags[key] = render(acc, notes.get(key))
    return tags
