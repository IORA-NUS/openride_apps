"""The dispatch objective as policy helpers: deadhead km and the dual-cycle cost.

Thin delegations to ``assignment.constraints`` so the arithmetic exists once and
byte-identity holds by construction (plan ``docs/plans/policy_slots_p0_p3.md``
§1.2 item 6, §4 P1.3). The framework's accounting (``benefit_km``, owner reserve,
claim bids, arbitration) uses the same ``assignment_cost``.

Policy helper. Not a leaf module: imports ``assignment.constraints`` at top (plan
§2.7; safe while ``assignment/app.py`` imports ``policies`` lazily).
"""

from __future__ import annotations

import logging
from typing import Any

from apps.container_logistics.assignment import constraints as assign_constraints

logger = logging.getLogger(__name__)

__all__ = ["deadhead_km", "is_dual_cycle", "dual_cycle_cost"]


def deadhead_km(truck: dict[str, Any], order: dict[str, Any]) -> float:
    """Haversine km from the truck's anchor to the order pickup; ``inf`` if unknown."""
    anchor = assign_constraints.truck_anchor_loc(truck)
    pickup = assign_constraints.order_pickup_loc(order)
    if anchor is None or pickup is None:
        return float("inf")
    km = assign_constraints._haversine_km(anchor, pickup)
    return float("inf") if km is None else km


def is_dual_cycle(truck: dict[str, Any], order: dict[str, Any], *, radius_km: float) -> bool:
    """The exact dual-cycle predicate ``assignment_cost`` applies."""
    pickup = assign_constraints.order_pickup_loc(order)
    return assign_constraints._is_dual_cycle_pair(truck, order, pickup, float(radius_km))


def dual_cycle_cost(
    truck: dict[str, Any],
    order: dict[str, Any],
    *,
    bonus_km: float,
    radius_km: float,
) -> float:
    """``assignment_cost`` with these two knobs: deadhead km minus the dual-cycle bonus, clamped at 0."""
    return assign_constraints.assignment_cost(
        truck, order, {"dual_cycle_bonus_km": bonus_km, "dual_cycle_radius_km": radius_km}
    )
