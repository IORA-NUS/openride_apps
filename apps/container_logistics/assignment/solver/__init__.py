"""Built-in dispatch solvers and the legacy ``get_solver`` lookup.

The solvers register themselves into the policy registry with ``@policy`` (slot
``planner``); the registry is the single source of names. :func:`get_solver` is a
fail-loud lookup through it (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.5): an
unknown name raises ``UnknownPolicyError`` listing the registered names, never a
silent fallback.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type

from apps.container_logistics.policies.dispatch.legacy import LEGACY_LEAF_DEFAULT
from apps.container_logistics.policies.framework import get_policy_spec

from .base import BaseAssignmentSolver
from .greedy_nearest import GreedyNearestSolver
from .random_assignment import RandomAssignmentSolver

SOLVER_REGISTRY: Dict[str, Type[BaseAssignmentSolver]] = {
    "RandomAssignment": RandomAssignmentSolver,
    "GreedyNearest": GreedyNearestSolver,
}
"""Frozen legacy view; read the policy registry (``list_policies("planner")``).

Kept for old importers only. New policies do NOT appear here.
"""

DEFAULT_SOLVER: str = LEGACY_LEAF_DEFAULT
"""The default per-scope (leaf) solver: a legacy selection with no solver runs it.

Not the dispatch SLOT default (``Partitioned``, a decomposition whose ``inner``
defaults to this leaf); ``get_solver`` instantiates leaves only.
"""


def get_solver(
    name: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> BaseAssignmentSolver:
    """Instantiate the registered dispatch policy ``name`` (``None``/empty -> :data:`DEFAULT_SOLVER`).

    ``params`` is passed to the class as a plain dict copy. Raises
    ``UnknownPolicyError`` for an unregistered name; there is no fallback.
    """
    spec = get_policy_spec("planner", name or DEFAULT_SOLVER)
    return spec.cls(params=dict(params or {}))


__all__ = [
    "BaseAssignmentSolver",
    "RandomAssignmentSolver",
    "GreedyNearestSolver",
    "SOLVER_REGISTRY",
    "DEFAULT_SOLVER",
    "get_solver",
]
