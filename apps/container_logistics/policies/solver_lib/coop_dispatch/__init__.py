"""Standalone cooperative-dispatch solver: network-flow + fairness models,
edge construction, and a typed input/output library layer (see lib/ and
README.md).

This folder is self-contained — base_solver.py, edge_builder.py,
network_flow_solver.py, fairness_solver.py, and lib/ import nothing from
outside this directory, aside from the ortools/gurobipy packages. It's the
single source of truth for this solver's math: the rest of this repository
(solver_engine.py, params_builder.py, and their callers) imports from here
rather than keeping a second copy. It's designed to be copied or
git-submoduled into another codebase wholesale — see README.md.
"""
from __future__ import annotations

from .base_solver import BaseSolver, create_solver_with_fallback
from .edge_builder import EdgeBuilder
from .fairness_solver import FairnessSolver
from .network_flow_solver import NetworkFlowSolver
from .lib import (
    CooperationStructure,
    EdgeSet,
    Fleet,
    Order,
    SolverResult,
    SolverStats,
    TravelLookup,
    build_edges,
    reconstruct_raw_routes,
    solve_fairness,
    solve_network_flow,
)

__all__ = [
    "BaseSolver",
    "create_solver_with_fallback",
    "EdgeBuilder",
    "FairnessSolver",
    "NetworkFlowSolver",
    "Order",
    "Fleet",
    "CooperationStructure",
    "TravelLookup",
    "EdgeSet",
    "SolverResult",
    "SolverStats",
    "build_edges",
    "solve_network_flow",
    "solve_fairness",
    "reconstruct_raw_routes",
]
