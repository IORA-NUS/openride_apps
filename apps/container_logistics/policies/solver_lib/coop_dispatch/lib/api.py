"""Standalone entry points: typed orders/fleet in, solver results out.

This module is the only place that converts the typed `Order`/`Fleet`/
`CooperationStructure` schema (schema.py) into the exact dict shapes that
`EdgeBuilder`, `NetworkFlowSolver`, and `FairnessSolver` (one directory up)
already expect. Those three classes are untouched by this extraction — every
behavior here is a thin, explicit wiring layer over code that already works,
not a reimplementation.

Typical use:

    from compute_core.solver.standalone.lib import Order, build_edges, solve_fairness

    orders = [Order(order_id="o1", original_haulier="A", ...), ...]
    fleet = {"A": 12, "B": 8}
    cooperation = {("A", "A"), ("B", "B"), ("A", "B"), ("B", "A")}
    travel_lookup = {...}  # see schema.TravelLookup

    result, stats = solve_fairness(orders, fleet, cooperation, travel_lookup)

See ../README.md for the full field-by-field contract and worked example.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..edge_builder import EdgeBuilder
from ..fairness_solver import FairnessSolver
from ..network_flow_solver import NetworkFlowSolver
from .schema import CooperationStructure, EdgeSet, Fleet, Order, SolverResult, SolverStats, TravelLookup

__all__ = [
    "Order",
    "build_edges",
    "solve_network_flow",
    "solve_fairness",
    "reconstruct_raw_routes",
]


def _solve(solver: Any, params: dict[str, Any]) -> tuple[SolverResult, SolverStats]:
    """NetworkFlowSolver/FairnessSolver.solve() returns (result, stats) on
    every path except one: if no solver backend is available at all (neither
    gurobipy nor OR-Tools SCIP/CBC), it returns a bare {"status": -1, ...}
    dict instead of a 2-tuple. Unpacking that directly as `result, stats =
    solver.solve(...)` silently iterates the dict's keys instead of raising —
    a latent defect in those two (otherwise unmodified) files. Normalized
    here rather than there, to keep this library's return contract honest
    without touching code outside this directory.
    """
    outcome = solver.solve(params)
    if isinstance(outcome, dict):
        return outcome, outcome
    return outcome


def _validate_orders(orders: list[Order]) -> None:
    seen: set[str] = set()
    for o in orders:
        if o.order_id in seen:
            raise ValueError(f"Duplicate order_id: {o.order_id!r} — order_id must be unique per instance.")
        seen.add(o.order_id)
        if o.delivery_time < o.pickup_time:
            raise ValueError(
                f"Order {o.order_id!r}: delivery_time ({o.delivery_time}) is before "
                f"pickup_time ({o.pickup_time})."
            )


def _order_info_dict(orders: list[Order]) -> dict[str, dict[str, Any]]:
    """Build the exact dict shape EdgeBuilder.build_edges_with_bad_exchange()
    expects for its `order_info` argument — key names are hardcoded in
    edge_builder.py and must match exactly."""
    return {
        o.order_id: {
            "actual_start_time": o.pickup_time,
            "actual_end_time": o.delivery_time,
            "pickup_postal_code": o.pickup_location,
            "delivery_postal_code": o.delivery_location,
            "occupied_job": int(o.trailer_occupied_after),
            "released_job": int(o.trailer_released_from_prior),
        }
        for o in orders
    }


def build_edges(
    orders: list[Order],
    travel_lookup: TravelLookup,
    *,
    raw_routes: Iterable[tuple[str, str]] = (),
    parking_yds: Iterable[str] = (),
    bad_wait_min: float = 120,
    bad_deadhead_km: float = 5,
    wait_limit_min: float = 120,
    always_include_raw: bool = True,
) -> EdgeSet:
    """Build the feasible-movement graph for one instance's orders.

    `raw_routes`: the actual historical sequence of orders per vehicle, as
    (order_id, order_id) consecutive pairs — e.g. if a vehicle ran
    o1 -> o2 -> o3, pass [("o1","o2"), ("o2","o3")]. Optional; if you don't
    have historical routing, omit it (every edge is then judged purely on
    the wait/deadhead thresholds, and `bad_orders` will be empty since
    "bad" is defined relative to what actually happened). See
    reconstruct_raw_routes() if you have per-vehicle order sequences but
    need the consecutive-pair form.

    `parking_yds`: locations treated as trailer-parking yards for detour-cost
    purposes. Only consulted when an order has trailer_occupied_after=True
    and the next one doesn't continue the same trailer — irrelevant if you
    don't use the trailer flags (see schema.Order's docstring).
    """
    _validate_orders(orders)
    order_owner = {o.order_id: o.original_haulier for o in orders}
    order_info = _order_info_dict(orders)
    order_list = [o.order_id for o in orders]

    builder = EdgeBuilder(travel_lookup=travel_lookup)
    bad_orders, edges, edge_cost, edge_trailer_cost = builder.build_edges_with_bad_exchange(
        raw_edge_list=list(raw_routes),
        order_list=order_list,
        order_owner=order_owner,
        order_info=order_info,
        parking_yds=list(parking_yds),
        bad_wait_min=bad_wait_min,
        bad_deadhead_km=bad_deadhead_km,
        wait_limit_min=wait_limit_min,
        always_include_raw=always_include_raw,
    )
    return EdgeSet(
        bad_orders=bad_orders,
        edges=edges,
        edge_cost=edge_cost,
        edge_trailer_cost=edge_trailer_cost,
        raw_edge_list=list(raw_routes),
    )


def _companies_and_vehicle_limit(orders: list[Order], fleet: Fleet) -> tuple[list[str], dict[str, int]]:
    companies = sorted({o.original_haulier for o in orders})
    missing = [c for c in companies if c not in fleet]
    if missing:
        raise ValueError(
            f"No fleet entry for {missing} — every company that owns at least one "
            "order must have a vehicle_limit in `fleet`."
        )
    return companies, {c: int(fleet[c]) for c in companies}


def _order_distance_meters(orders: list[Order], travel_lookup: TravelLookup) -> dict[str, float]:
    dist: dict[str, float] = {}
    for o in orders:
        if o.distance_km is not None:
            dist[o.order_id] = float(o.distance_km) * 1000.0
            continue
        leg = travel_lookup.get((o.pickup_location, o.delivery_location))
        if leg is None:
            raise ValueError(
                f"Order {o.order_id!r}: no travel_lookup entry for "
                f"({o.pickup_location!r}, {o.delivery_location!r}) and no distance_km override given."
            )
        dist[o.order_id] = float(leg["distance"])
    return dist


def solve_network_flow(
    orders: list[Order],
    fleet: Fleet,
    cooperation_structure: CooperationStructure,
    travel_lookup: TravelLookup,
    *,
    edges: EdgeSet | None = None,
    use_trailer_cost: bool = True,
    raw_cost: float = 0.0,
    instance_id: str = "instance",
    build_edges_kwargs: dict[str, Any] | None = None,
) -> tuple[SolverResult, SolverStats]:
    """Assign every order to a company and sequence each company's vehicle
    tours, minimizing total travel (+ trailer-detour, if enabled) cost.

    No fairness constraints: this model freely assigns any order to any
    cooperating company (per `cooperation_structure`) if doing so reduces
    total system cost — it does not keep a company's own orders with that
    company by default. For a model that does keep each company's own
    orders by default and only reassigns ("swaps") a restricted set, use
    solve_fairness() instead.

    `edges`: pass a pre-built EdgeSet (from build_edges()) to skip rebuilding
    it, e.g. when solving the same instance under both models or re-solving
    after changing weights. If omitted, built internally with defaults (or
    `build_edges_kwargs`, forwarded to build_edges()).

    `raw_cost`: optional baseline cost (e.g. each company running its own
    orders alone) carried through into the output stats for comparison.
    Purely informational — does not affect the optimization. Pass 0.0 (the
    default) if you have no baseline to compare against.
    """
    companies, vehicle_limit = _companies_and_vehicle_limit(orders, fleet)
    cooperation = set(cooperation_structure)
    order_owner = {o.order_id: o.original_haulier for o in orders}
    order_list = [o.order_id for o in orders]

    if edges is None:
        edges = build_edges(orders, travel_lookup, **(build_edges_kwargs or {}))

    params = {
        "instance_id": instance_id,
        "companies": companies,
        "orders": order_list,
        "order_owner": order_owner,
        "cooperation": cooperation,
        "vehicle_limit": vehicle_limit,
        "edges_input": edges.edges,
        "edge_cost_input": edges.edge_cost,
        "edge_trailer_cost_input": edges.edge_trailer_cost,
        "raw_cost": raw_cost,
        "use_trailer_cost": use_trailer_cost,
        "raw_edge_list": edges.raw_edge_list,
    }
    return _solve(NetworkFlowSolver(), params)


def solve_fairness(
    orders: list[Order],
    fleet: Fleet,
    cooperation_structure: CooperationStructure,
    travel_lookup: TravelLookup,
    *,
    edges: EdgeSet | None = None,
    lambda_penalty: float = 0.05,
    fairness_tau: float = 0.0,
    baseline_profit: dict[str, float] | None = None,
    exchange_limit: dict[str, int] | None = None,
    sharing_limit: float | None = None,
    bad_orders: Iterable[str] | None = None,
    use_trailer_cost: bool = True,
    raw_cost: float = 0.0,
    instance_id: str = "instance",
    build_edges_kwargs: dict[str, Any] | None = None,
) -> tuple[SolverResult, SolverStats]:
    """Like solve_network_flow(), but each company keeps its own orders by
    default and only orders flagged as poorly-served ("bad", see
    build_edges()) are eligible to move to a cooperating company — the
    "swap" `y[i]` decision. Minimizes total cost plus `lambda_penalty` times
    the number of swaps, optionally floored by a per-company fairness
    constraint (`baseline_profit` + `fairness_tau`).

    `bad_orders`: which orders are swap-eligible. If omitted (the default)
    and `edges` is a pre-built EdgeSet (or gets built internally), this uses
    that EdgeSet's own `bad_orders` — i.e. only orders that actually
    violated the wait/deadhead thresholds against their historical routing
    are swappable. This is the same default the in-repo production pipeline
    uses. Pass an explicit iterable (including an empty one, to disable
    swapping entirely) to override.

    `exchange_limit`: hard cap on swapped-away orders per company. If
    omitted and `sharing_limit` is given (a fraction, e.g. 0.2 = at most 20%
    of a company's own orders may be swapped away), it's computed from each
    company's order count. If neither is given, there's no cap.

    `baseline_profit`: each company's own-orders-only profit (revenue minus
    empty-travel cost), used as the floor for `revenue - cost >=
    baseline_profit - fairness_tau * |baseline_profit|`. Typically computed
    by calling this same function (or solve_network_flow) with
    cooperation_structure restricted to each company's own self-loop.
    Omit to run without a fairness floor (only the swap-count penalty
    applies).
    """
    companies, vehicle_limit = _companies_and_vehicle_limit(orders, fleet)
    cooperation = set(cooperation_structure)
    order_owner = {o.order_id: o.original_haulier for o in orders}
    order_list = [o.order_id for o in orders]
    order_distance = _order_distance_meters(orders, travel_lookup)

    if edges is None:
        edges = build_edges(orders, travel_lookup, **(build_edges_kwargs or {}))

    if bad_orders is None:
        bad_orders = edges.bad_orders

    if exchange_limit is None and sharing_limit is not None:
        order_counts: dict[str, int] = {}
        for o in orders:
            order_counts[o.original_haulier] = order_counts.get(o.original_haulier, 0) + 1
        exchange_limit = {c: int(n * sharing_limit) for c, n in order_counts.items()}

    params = {
        "instance_id": instance_id,
        "companies": companies,
        "orders": order_list,
        "order_owner": order_owner,
        "cooperation": cooperation,
        "vehicle_limit": vehicle_limit,
        "edges_input": edges.edges,
        "edge_cost_input": edges.edge_cost,
        "edge_trailer_cost_input": edges.edge_trailer_cost,
        "raw_cost": raw_cost,
        "use_trailer_cost": use_trailer_cost,
        "order_distance": order_distance,
        "lambda_penalty": lambda_penalty,
        "fairness_tau": fairness_tau,
        "baseline_profit": baseline_profit,
        "exchange_limit": exchange_limit,
        "bad_orders": list(bad_orders),
        "raw_edge_list": edges.raw_edge_list,
    }
    return _solve(FairnessSolver(), params)


def reconstruct_raw_routes(order_sequence_by_vehicle: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Convenience helper: turn {vehicle_id: [order_id, ...]} (each list
    already in chronological order) into the (order_id, order_id) consecutive
    pairs build_edges()'s `raw_routes` expects.

    This is the only place in the library that would need a vehicle/trailer
    registration identifier at all — everything else only needs orders and
    companies. If your source system doesn't track historical vehicle
    routing, skip this entirely and don't pass `raw_routes` to build_edges().
    """
    pairs: list[tuple[str, str]] = []
    for order_ids in order_sequence_by_vehicle.values():
        pairs.extend(zip(order_ids, order_ids[1:]))
    return pairs
