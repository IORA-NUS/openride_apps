"""Standalone network-flow / fairness solver library.

Typed-input wrapper around the sibling `edge_builder.py`, `network_flow_solver.py`,
and `fairness_solver.py` modules (one directory up) — none of those files are
modified by this package; this is purely an additive, parallel entry point.
See ../README.md for the full contract and a worked example.
"""
from __future__ import annotations

from .api import build_edges, reconstruct_raw_routes, solve_fairness, solve_network_flow
from .schema import CooperationStructure, EdgeSet, Fleet, Order, SolverResult, SolverStats, TravelLookup

__all__ = [
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
