"""Typed input/output schema for the standalone solver library.

Nothing in this module touches a database, a config object, or a file path.
Every field here is either a plain Python scalar or a structure built from
them, so this module (and api.py beside it) can be copied to a different
codebase with zero changes, as long as `edge_builder.py`, `base_solver.py`,
`network_flow_solver.py`, and `fairness_solver.py` (the three siblings one
directory up) come with it. See ../README.md for the full portability note.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Order:
    """One job: a single pickup -> delivery movement.

    `pickup_location` / `delivery_location` are opaque identifiers — postal
    codes, lat/lon grid cells, simulation node IDs, anything hashable and
    consistent with the keys used in your `travel_lookup`. The solver never
    interprets them beyond using them as lookup keys.

    `trailer_occupied_after` / `trailer_released_from_prior` are optional and
    default to False. They drive trailer-swap detour costing only — leaving
    both False for every order does not break anything, it just turns that
    cost term off everywhere (equivalent to `use_trailer_cost=False`, but
    settable per order if you have partial information). These two flags
    encode multi-day trailer-continuity state that usually can't be derived
    from one order alone — see ../README.md, "Trailer-swap costing is
    optional" for what they mean and how to approximate them if you don't
    track trailers explicitly.

    `distance_km` is optional. If omitted, it's computed as
    `travel_lookup[(pickup_location, delivery_location)]["distance"] / 1000`
    the first time it's needed (fairness solver only — network-flow doesn't
    use it at all).
    """
    order_id: str
    original_haulier: str
    pickup_location: str
    delivery_location: str
    pickup_time: datetime
    delivery_time: datetime
    trailer_occupied_after: bool = False
    trailer_released_from_prior: bool = False
    distance_km: float | None = None


# A travel matrix: {(location_a, location_b): {"distance": meters, "duration": seconds}}.
# Must contain an entry for every (pickup, delivery) pair that appears in your
# orders, plus every pair your own edge-feasibility logic might ask for.
TravelLookup = dict[tuple[str, str], dict[str, float]]

# Which companies may exchange orders with which. MUST include a self-loop
# (company, company) for every company that should be able to serve its own
# orders at all — this is not automatic. A structure that forgets a
# self-loop silently makes that company's own orders infeasible.
CooperationStructure = set[tuple[str, str]] | list[tuple[str, str]]

# haulier -> number of vehicles available (a hard cap on vehicles dispatched
# from the depot for that company in this instance).
Fleet = dict[str, int]


@dataclass
class EdgeSet:
    """Output of build_edges() — the feasible-movement graph for one instance.

    Pass this into solve_network_flow()/solve_fairness() via the `edges=`
    argument to avoid rebuilding it per solve call (e.g. to try both models,
    or several fairness weight settings, on the same instance).
    """
    bad_orders: set[str]
    edges: list[tuple[str, str]]
    edge_cost: dict[tuple[str, str], float]
    edge_trailer_cost: dict[tuple[str, str], float]
    raw_edge_list: list[tuple[str, str]] = field(default_factory=list)


# solve_network_flow()/solve_fairness() return (result, stats) — both plain
# dicts, intentionally not forced into a rigid dataclass since their shape is
# the existing, already-stable NetworkFlowSolver/FairnessSolver output. See
# ../README.md's "Output shape" section for the documented key-by-key
# contract (varies slightly between the two models).
SolverResult = dict[str, Any]
SolverStats = dict[str, Any]
