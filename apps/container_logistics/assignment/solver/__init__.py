from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

from .base import BaseAssignmentSolver
from .greedy_nearest import GreedyNearestSolver
from .random_assignment import RandomAssignmentSolver

logger = logging.getLogger(__name__)

# Plug-and-play registry: add a solver class here (and a file in this package) and
# it becomes selectable by name via ``assignment_settings["profile"]["strategy"]``.
# Keep keys stable — they are persisted in scenario behaviors and the frontend.
SOLVER_REGISTRY: Dict[str, Type[BaseAssignmentSolver]] = {
    "RandomAssignment": RandomAssignmentSolver,
    "GreedyNearest": GreedyNearestSolver,
}

DEFAULT_SOLVER = "RandomAssignment"


def get_solver(
    name: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> BaseAssignmentSolver:
    """Instantiate a solver by name, falling back to the default if unknown.

    Fail-soft: an unknown/missing strategy logs once and runs the default rather
    than crashing the assignment agent mid-run.
    """
    key = name or DEFAULT_SOLVER
    solver_cls = SOLVER_REGISTRY.get(key)
    if solver_cls is None:
        logger.warning(
            "Unknown assignment strategy %r — falling back to %s. Known: %s",
            name,
            DEFAULT_SOLVER,
            ", ".join(sorted(SOLVER_REGISTRY)),
        )
        solver_cls = SOLVER_REGISTRY[DEFAULT_SOLVER]
    return solver_cls(params=params or {})


__all__ = [
    "BaseAssignmentSolver",
    "RandomAssignmentSolver",
    "GreedyNearestSolver",
    "SOLVER_REGISTRY",
    "DEFAULT_SOLVER",
    "get_solver",
]
