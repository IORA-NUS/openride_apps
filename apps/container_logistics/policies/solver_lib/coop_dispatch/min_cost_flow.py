"""Exact min-cost-flow backend for cooperation components with COMPLETE adjacency (plan D13, N2 item 6).

OpenRide addition, not upstream. Takes the same ``params`` as
:meth:`NetworkFlowSolver.solve` (with ``use_trailer_cost`` False) and returns a
``(result, stats)`` pair of the same shape (``routes`` holds only the chosen arcs).

Why it is exact: inside a component where every company may serve every other company's
orders (``(a, b)`` in ``cooperation`` for all a, b, self-loops included), the trucks of the
component are interchangeable, so the per-company commodities of the MIP collapse into ONE
commodity. With fixed job times, start nodes of capacity 1 and a skip arc for deferral the
model is a single-commodity min-cost flow, whose LP is totally unimodular: the integral
optimum of OR-Tools ``SimpleMinCostFlow`` is the MIP optimum.

Network per component: source S (supply = start nodes + depot vehicles), sink T; each job j
is split into ``in_j`` (supply -1) and ``out_j`` (supply +1), which forces exactly one unit
through j: either along a chain (an arc into ``in_j``) or, with ``defer_penalty`` P, over
the skip arc ``out_j -> in_j`` at cost P. Arcs: ``S -> sid`` / ``sid -> T`` (a truck may
stay unused), ``sid -> in_j`` (start cost), ``S -> D`` / ``D -> T`` / ``D -> in_j`` (depot,
capacity = the depot companies' summed ``vehicle_limit``), ``out_i -> in_j`` (edge cost),
``out_j -> T``.

Costs are rounded to integer meters (``round``): the optimum is exact for the rounded
costs. ``objective`` is that integer optimum; ``objective_float`` re-prices the chosen arcs
at their original costs, so it is within ``0.5 x (arcs in the plan)`` of the MIP optimum
(plan addendum M5: compare with an integer-rounding tolerance).

Refused (ValueError), use the MIP instead: a component that is not :func:`mcf_applicable`
(not complete; or its classes differ in ``class_attrs``, e.g. truck size / restricted areas
/ working hours); an ``eligible`` set that is not uniform over a component's companies for
some job (it would make the commodities differ again); ``use_trailer_cost`` True; duplicate
edges. :func:`solve_by_component` picks the backend per component instead of refusing.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable

import numpy as np
from ortools.graph.python import min_cost_flow

START, END = "start", "end"
OPTIMAL, INFEASIBLE = 0, 2


def _int(value: float, what: str) -> int:
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{what} must be finite (got {value!r})")
    return int(round(v))


def cooperation_components(companies: Iterable[Any], cooperation: Iterable[tuple[Any, Any]]) -> list[list[Any]]:
    """Connected components (undirected) of the cooperation graph, in ``companies`` order."""
    companies = list(companies)
    known = set(companies)
    parent = {c: c for c in companies}

    def find(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for a, b in cooperation:
        if a in known and b in known:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    groups: Dict[Any, list] = {}
    for c in companies:
        groups.setdefault(find(c), []).append(c)
    return list(groups.values())


def is_complete(component: Iterable[Any], cooperation: Iterable[tuple[Any, Any]]) -> bool:
    coop = set(cooperation)
    comp = list(component)
    return all((a, b) in coop for a in comp for b in comp)


def mcf_applicable(component: Iterable[Any], cooperation: Iterable[tuple[Any, Any]],
                   class_attrs: Dict[Any, Any] | None = None) -> tuple[bool, str]:
    """Whether the single-commodity min-cost flow is EXACT for this component (addendum M5).

    Needs complete directed adjacency, self pairs included, and every class (company /
    truck class) of the component sharing the attributes that decide job eligibility:
    ``class_attrs[c]`` (truck size, restricted areas, working hours, ... -- any hashable
    the caller builds) must be equal across the component. A class missing from
    ``class_attrs`` is compared as ``None``. Returns ``(ok, reason)``; ``reason`` is
    ``"complete"`` when ok.
    """
    comp = list(component)
    if not is_complete(comp, cooperation):
        return False, "adjacency not complete"
    if class_attrs is not None:
        attrs = {repr(class_attrs.get(c)) for c in comp}
        if len(attrs) > 1:
            return False, "classes differ in eligibility attributes"
    return True, "complete"


def solve_min_cost_flow(params: Dict[str, Any]):
    t0 = time.time()
    if params.get("use_trailer_cost", True):
        raise ValueError("min_cost_flow supports use_trailer_cost=False only")
    companies = list(params["companies"])
    orders = list(params["orders"])
    order_owner = params["order_owner"]
    cooperation = set(params["cooperation"])
    vehicle_limit = params["vehicle_limit"]
    start_nodes = params.get("start_nodes") or {}
    defer = params.get("defer_penalty")
    defer_i = None if defer is None else _int(defer, "defer_penalty")
    eligible = params.get("eligible")
    eligible = None if eligible is None else set(eligible)
    edge_cost = params["edge_cost_input"]
    start_cost = params.get("start_edge_cost", {})

    edges = list(params["edges_input"])
    if len(set(edges)) != len(edges):
        raise ValueError("min_cost_flow needs distinct edges (duplicates found)")

    comps = cooperation_components(companies, cooperation)
    comp_of = {c: k for k, comp in enumerate(comps) for c in comp}
    for comp in comps:
        ok, why = mcf_applicable(comp, cooperation, params.get("class_attrs"))
        if not ok:
            raise ValueError(f"cooperation component {comp!r}: {why}, so it is not complete for "
                             "the min-cost flow: use the MIP")

    # A job is servable iff its owner's component serves it, filtered by `eligible`.
    serv: Dict[Any, bool] = {}
    job_comp: Dict[Any, int] = {}
    for j in orders:
        k = comp_of[order_owner[j]]
        job_comp[j] = k
        if eligible is None:
            serv[j] = True
            continue
        flags = {(j, c) in eligible for c in comps[k]}
        if len(flags) > 1:
            raise ValueError(f"eligible is not uniform over component {comps[k]!r} for job {j!r}: use the MIP")
        serv[j] = flags.pop()

    sn_companies = {spec["company"] for spec in start_nodes.values()}
    sid_list = list(start_nodes)

    tails, heads, caps, costs, kinds = [], [], [], [], []
    node_ids: Dict[Any, int] = {}

    def node(key):
        nid = node_ids.get(key)
        if nid is None:
            nid = node_ids[key] = len(node_ids)
        return nid

    fcost: list = []

    def arc(t, h, cap, cost, kind, float_cost=None):
        tails.append(node(t)); heads.append(node(h)); caps.append(cap); costs.append(cost); kinds.append(kind)
        fcost.append(float(cost) if float_cost is None else float(float_cost))

    supplies: Dict[Any, int] = {}
    depot_cap: Dict[int, int] = {}
    for k, comp in enumerate(comps):
        sids = [s for s in sid_list if comp_of[start_nodes[s]["company"]] == k]
        dcap = sum(int(vehicle_limit[c]) for c in comp if c not in sn_companies)
        depot_cap[k] = dcap
        jobs_k = [j for j in orders if job_comp[j] == k]
        if not jobs_k and not sids:
            continue
        S, T, D = ("S", k), ("T", k), ("D", k)
        supplies[S] = len(sids) + dcap
        supplies[T] = -(len(sids) + dcap)
        for s in sids:
            arc(S, ("sid", s), 1, 0, None)
            arc(("sid", s), T, 1, 0, None)
        if dcap:
            arc(S, D, dcap, 0, None)
            arc(D, T, dcap, 0, None)
        for j in jobs_k:
            supplies[("in", j)] = -1
            supplies[("out", j)] = 1
            if defer_i is not None:
                arc(("out", j), ("in", j), 1, defer_i, ("skip", j), float(defer))
            if serv[j]:
                if dcap:
                    arc(D, ("in", j), 1, 0, ("depot", j, k))
                arc(("out", j), T, 1, 0, ("end", j))

    for (sid, j) in params.get("start_edges", []):
        if sid not in start_nodes or j not in serv:
            raise ValueError(f"start edge ({sid!r}, {j!r}) names an unknown start node or order")
        c = start_nodes[sid]["company"]
        if serv[j] and job_comp[j] == comp_of[c]:
            arc(("sid", sid), ("in", j), 1, _int(start_cost[(sid, j)], "start edge cost"), ("start", sid, j),
                start_cost[(sid, j)])
    for (i, j) in edges:
        if i == j or i not in serv or j not in serv:
            continue
        if serv[i] and serv[j] and job_comp[i] == job_comp[j]:
            arc(("out", i), ("in", j), 1, _int(edge_cost[(i, j)], "edge cost"), ("edge", i, j),
                edge_cost[(i, j)])

    smcf = min_cost_flow.SimpleMinCostFlow()
    if tails:
        smcf.add_arcs_with_capacity_and_unit_cost(
            np.array(tails, dtype=np.int64), np.array(heads, dtype=np.int64),
            np.array(caps, dtype=np.int64), np.array(costs, dtype=np.int64))
    if supplies:
        smcf.set_nodes_supplies(np.array([node(n) for n in supplies], dtype=np.int64),
                                np.array(list(supplies.values()), dtype=np.int64))
    status_raw = smcf.solve() if tails else smcf.OPTIMAL
    if orders and not tails:
        status_raw = smcf.INFEASIBLE
    ok = status_raw == smcf.OPTIMAL
    stats = {
        "backend": "mincostflow",
        "status": OPTIMAL if ok else INFEASIBLE,
        "status_name": "OPTIMAL" if ok else "INFEASIBLE",
        "mcf_status": int(status_raw),
        "components": comps,
        "num_nodes": len(node_ids),
        "num_arcs": len(tails),
        "objective": None,
        "solve_time_s": None,
    }
    if not ok:
        stats["solve_time_s"] = time.time() - t0
        return {"status": INFEASIBLE, "message": "No optimal solution"}, stats

    flows = smcf.flows(np.arange(len(tails), dtype=np.int64)) if tails else np.zeros(0, dtype=np.int64)
    succ, unplanned, firsts = {}, [], []
    for a_idx in np.flatnonzero(flows > 0).tolist():
        kind = kinds[a_idx]
        if kind is None:
            continue
        tag = kind[0]
        if tag == "skip":
            unplanned.append(kind[1])
        elif tag == "edge":
            succ[kind[1]] = kind[2]
        elif tag in ("start", "depot"):
            firsts.append(kind)

    # Label chains with a company: a start node's own; depot chains fill the component's
    # depot companies' vehicle limits in `companies` order.
    remaining = {c: int(vehicle_limit[c]) for c in companies if c not in sn_companies}
    routes: Dict[tuple, float] = {}
    assignment: Dict[tuple, float] = {}
    reached = set()
    for kind in firsts:
        if kind[0] == "start":
            origin, first, c = kind[1], kind[2], start_nodes[kind[1]]["company"]
        else:
            first, k = kind[1], kind[2]
            c = next(cc for cc in comps[k] if remaining.get(cc, 0) > 0)
            remaining[c] -= 1
            origin = START
        chain = [first]
        while chain[-1] in succ:
            chain.append(succ[chain[-1]])
        routes[(origin, first, c)] = 1.0
        for a, b in zip(chain, chain[1:]):
            routes[(a, b, c)] = 1.0
        routes[(chain[-1], END, c)] = 1.0
        for j in chain:
            assignment[(j, c)] = 1.0
            reached.add(j)
    unplanned_set = set(unplanned)
    orphaned = [j for j in orders if j not in reached and j not in unplanned_set]
    for j in orphaned:  # on a cycle no start reaches (only if the edges are cyclic)
        c = comps[job_comp[j]][0]
        assignment[(j, c)] = 1.0
        if j in succ:
            routes[(j, succ[j], c)] = 1.0

    objective = float(smcf.optimal_cost())
    objective_float = float(sum(fcost[a_idx] * int(flows[a_idx]) for a_idx in np.flatnonzero(flows > 0).tolist()))
    result = {
        "instance_id": params.get("instance_id"),
        "status": OPTIMAL,
        "objective": objective,
        "objective_float": objective_float,
        "assignment": assignment,
        "routes": routes,
    }
    if defer_i is not None:
        result["unplanned"] = [j for j in orders if j in unplanned_set]
    stats.update(objective=objective, objective_float=objective_float, solve_time_s=time.time() - t0,
                 num_unplanned=len(unplanned_set), num_orphaned=len(orphaned))
    return result, stats
