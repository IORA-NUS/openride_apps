"""Legacy name for the pair-solver contract.

``BaseAssignmentSolver`` is :class:`PairSolver` itself (an alias, not a subclass), so
the injection API it inherits from ``DispatchPolicy`` (``set_rng``, ``set_tiebreak``,
``set_rebate_book``) is the same class attribute that tests patch through this name.
The contract and its docstrings live in ``apps/container_logistics/policies/dispatch/``
(plan ``docs/plans/policy_slots_p0_p3.md`` §4 P1.2).
"""

from __future__ import annotations

from apps.container_logistics.policies.dispatch.base import (
    Assignment,
    Cost,
    Order,
    PairAllowed,
    Truck,
)
from apps.container_logistics.policies.dispatch.pair_solver import PairSolver

BaseAssignmentSolver = PairSolver

__all__ = [
    "BaseAssignmentSolver",
    "Truck",
    "Order",
    "PairAllowed",
    "Cost",
    "Assignment",
]
