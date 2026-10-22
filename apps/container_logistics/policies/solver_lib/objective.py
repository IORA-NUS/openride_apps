"""The dispatch objective as policy helpers: deadhead km and the dual-cycle cost.

Thin delegations to ``assignment.constraints`` so the arithmetic exists once and
byte-identity holds by construction (plan ``docs/plans/policy_slots_p0_p3.md``
§1.2 item 6, §4 P1.3). The framework's accounting (``benefit_km``, owner reserve,
claim bids, arbitration) uses the same ``assignment_cost``.

Solver helper (``policies/solver_lib``). Not a leaf module: imports ``assignment.constraints`` at top (plan
§2.7; safe while ``assignment/app.py`` imports ``policies`` lazily).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from apps.container_logistics.assignment import constraints as assign_constraints

logger = logging.getLogger(__name__)

__all__ = ["deadhead_km", "is_dual_cycle", "dual_cycle_cost", "dual_cycle_cost_fn"]


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


def dual_cycle_cost_fn(*, bonus_km: float, radius_km: float) -> Callable[[dict[str, Any], dict[str, Any]], float]:
    """:func:`dual_cycle_cost` with the knobs bound once: ``cost(truck, order) -> float``.

    The hot-loop form, for a solver that scores every candidate pair (gate 6,
    round-2 performance): the knob dict is built ONCE instead of per pair, and the
    call is one Python frame above ``assignment_cost`` (the pre-policy-slots depth of
    ``AssignmentApp``'s ``_cost``). Same function, same two floats, so the value is
    bit-identical to :func:`dual_cycle_cost`. Raw documents only (no view unwrapping):
    ``ctx.candidate_pairs`` / ``ctx.sampled_trucks`` / ``ctx.raw_orders`` are raw.
    """
    params = {"dual_cycle_bonus_km": bonus_km, "dual_cycle_radius_km": radius_km}
    assignment_cost = assign_constraints.assignment_cost  # read now: a test may patch the module

    def cost(truck: dict[str, Any], order: dict[str, Any]) -> float:
        return assignment_cost(truck, order, params)

    return cost
