"""Exact min-cost-flow backend for cooperation components with COMPLETE adjacency (plan D13, N2 item 6).

OpenRide addition, not upstream. Takes the same ``params`` as
:meth:`NetworkFlowSolver.solve` (with ``use_trailer_cost`` False) and returns a
``(result, stats)`` pair of the same shape (``routes`` holds only the chosen arcs).

Why it is exact: inside a component where every company may serve every other company's
orders (``(a, b)`` in ``cooperation`` for all a, b, self-loops included), the trucks of the
component are interchangeable, so the per-company commodities of the MIP collapse into ONE
commodity. With fixed job times, integer start-node capacities and a skip arc for deferral the
model is a single-commodity min-cost flow, whose LP is totally unimodular: the integral
optimum of OR-Tools ``SimpleMinCostFlow`` is the MIP optimum.

Network per component: source S (supply = start nodes + depot vehicles), sink T; each job j
is split into ``in_j`` (supply -1) and ``out_j`` (supply +1), which forces exactly one unit
through j: either along a chain (an arc into ``in_j``) or, with ``defer_penalty`` P, over
the skip arc ``out_j -> in_j`` at cost P. Arcs: ``S -> sid`` / ``sid -> T`` (capacity =
the start node's ``capacity``, default 1; a truck may stay unused), ``sid -> in_j`` (start cost), ``S -> D`` / ``D -> T`` / ``D -> in_j`` (depot,
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

# The network is built from numpy index arrays in blocks (no per-arc Python objects), so a
# day of ~5k jobs with ~4.4M job arcs builds and solves in seconds (follow-up E2).

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


def _start_capacity(spec, sid) -> int:
    cap = spec.get("capacity", 1)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError(f"start node {sid!r}: capacity must be an int >= 1 (got {cap!r})")
    return cap


def _int_costs(values: np.ndarray, what: str) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if values.size and not np.isfinite(values).all():
        raise ValueError(f"{what} must be finite")
    return np.rint(values).astype(np.int64)


def _index_arrays(spec, what: str, n_src: int, n_dst: int):
    src = np.asarray(spec["src"], dtype=np.int64)
    dst = np.asarray(spec["dst"], dtype=np.int64)
    cost = np.asarray(spec["cost"], dtype=float)
    if not (src.shape == dst.shape == cost.shape) or src.ndim != 1:
        raise ValueError(f"{what}: src, dst and cost must be 1-d arrays of one length")
    if src.size and (src.min() < 0 or src.max() >= n_src or dst.min() < 0 or dst.max() >= n_dst):
        raise ValueError(f"{what}: an index is out of range")
    return src, dst, cost


def solve_min_cost_flow(params: Dict[str, Any]):
    """Solve ``params`` (see the module docstring).

    Arcs come either as the MIP's lists (``edges_input`` + ``edge_cost_input``,
    ``start_edges`` + ``start_edge_cost``) or, without building any Python tuple, as
    ``edges_array = {"src", "dst", "cost"}`` (indices into ``orders``) and
    ``start_edges_array = {"src", "dst", "cost"}`` (``src`` indexes ``list(start_nodes)``,
    ``dst`` indexes ``orders``), e.g. from
    :meth:`time_window_edges.TimeWindowArrays.as_solver_params`. A start node's
    ``capacity`` (default 1) is how many chains it may head.
    """
    t0 = time.time()
    if params.get("use_trailer_cost", True):
        raise ValueError("min_cost_flow supports use_trailer_cost=False only")
    companies = list(params["companies"])
    orders = list(params["orders"])
    n = len(orders)
    oidx = {o: k for k, o in enumerate(orders)}
    if len(oidx) != n:
        raise ValueError("order ids must be unique")
    order_owner = params["order_owner"]
    cooperation = set(params["cooperation"])
    vehicle_limit = params["vehicle_limit"]
    start_nodes = params.get("start_nodes") or {}
    sid_list = list(start_nodes)
    sidx = {s: k for k, s in enumerate(sid_list)}
    caps = np.array([_start_capacity(start_nodes[s], s) for s in sid_list], dtype=np.int64)
    defer = params.get("defer_penalty")
    defer_i = None if defer is None else _int(defer, "defer_penalty")
    eligible = params.get("eligible")
    eligible = None if eligible is None else set(eligible)

    comps = cooperation_components(companies, cooperation)
    K = len(comps)
    comp_of = {c: k for k, comp in enumerate(comps) for c in comp}
    for comp in comps:
        ok, why = mcf_applicable(comp, cooperation, params.get("class_attrs"))
        if not ok:
            raise ValueError(f"cooperation component {comp!r}: {why}, so it is not complete for "
                             "the min-cost flow: use the MIP")

    # A job is servable iff its owner's component serves it, filtered by `eligible`.
    job_comp = np.array([comp_of[order_owner[j]] for j in orders], dtype=np.int64)
    serv = np.ones(n, dtype=bool)
    if eligible is not None:
        for k, j in enumerate(orders):
            flags = {(j, c) in eligible for c in comps[job_comp[k]]}
            if len(flags) > 1:
                raise ValueError(f"eligible is not uniform over component {comps[job_comp[k]]!r} "
                                 f"for job {j!r}: use the MIP")
            serv[k] = flags.pop()

    # ---- arcs as index arrays ----
    if "edges_array" in params:
        if params.get("edges_input"):
            raise ValueError("give edges_input or edges_array, not both")
        e_src, e_dst, e_fc = _index_arrays(params["edges_array"], "edges_array", n, n)
    else:
        edge_cost = params["edge_cost_input"]
        edges = list(params["edges_input"])
        e_src = np.fromiter((oidx.get(i, -1) for i, _ in edges), dtype=np.int64, count=len(edges))
        e_dst = np.fromiter((oidx.get(j, -1) for _, j in edges), dtype=np.int64, count=len(edges))
        e_fc = np.fromiter((float(edge_cost[x]) for x in edges), dtype=float, count=len(edges))
        known = (e_src >= 0) & (e_dst >= 0)  # an edge naming an unknown order is not an arc
        e_src, e_dst, e_fc = e_src[known], e_dst[known], e_fc[known]
    if e_src.size and np.unique(e_src * max(n, 1) + e_dst).size != e_src.size:
        raise ValueError("min_cost_flow needs distinct edges (duplicates found)")
    if "start_edges_array" in params:
        if params.get("start_edges"):
            raise ValueError("give start_edges or start_edges_array, not both")
        s_src, s_dst, s_fc = _index_arrays(params["start_edges_array"], "start_edges_array", len(sid_list), n)
    else:
        sedges = list(params.get("start_edges", []))
        start_cost = params.get("start_edge_cost", {})
        for (sid, j) in sedges:
            if sid not in sidx or j not in oidx:
                raise ValueError(f"start edge ({sid!r}, {j!r}) names an unknown start node or order")
        s_src = np.fromiter((sidx[a] for a, _ in sedges), dtype=np.int64, count=len(sedges))
        s_dst = np.fromiter((oidx[b] for _, b in sedges), dtype=np.int64, count=len(sedges))
        s_fc = np.fromiter((float(start_cost[x]) for x in sedges), dtype=float, count=len(sedges))
    e_ic = _int_costs(e_fc, "edge cost")
    s_ic = _int_costs(s_fc, "start edge cost")

    sn_companies = {spec["company"] for spec in start_nodes.values()}
    sid_comp = np.array([comp_of[start_nodes[s]["company"]] for s in sid_list], dtype=np.int64)
    dcap = np.array([sum(int(vehicle_limit[c]) for c in comp if c not in sn_companies) for comp in comps],
                    dtype=np.int64)

    # ---- nodes: in_j = 2j, out_j = 2j+1, then S/T/D per component, then start nodes ----
    base = 2 * n
    S_of = base + 3 * np.arange(K, dtype=np.int64)
    T_of, D_of = S_of + 1, S_of + 2
    sid_node = base + 3 * K + np.arange(len(sid_list), dtype=np.int64)
    jobs = np.arange(n, dtype=np.int64)
    IN, OUT = 2 * jobs, 2 * jobs + 1

    blocks: list = []   # (name, tails, heads, caps, int cost, float cost)

    def block(name, t, h, cap, ic, fc):
        t = np.asarray(t, dtype=np.int64)
        blocks.append((name, t, np.asarray(h, dtype=np.int64),
                       np.broadcast_to(np.asarray(cap, dtype=np.int64), t.shape),
                       np.broadcast_to(np.asarray(ic, dtype=np.int64), t.shape),
                       np.broadcast_to(np.asarray(fc, dtype=float), t.shape)))

    block("S>sid", S_of[sid_comp], sid_node, caps, 0, 0.0)
    block("sid>T", sid_node, T_of[sid_comp], caps, 0, 0.0)
    dk = np.flatnonzero(dcap > 0)
    block("S>D", S_of[dk], D_of[dk], dcap[dk], 0, 0.0)
    block("D>T", D_of[dk], T_of[dk], dcap[dk], 0, 0.0)
    if defer_i is not None:
        block("skip", OUT, IN, 1, defer_i, float(defer))
    depot_jobs = jobs[serv & (dcap[job_comp] > 0)] if n else jobs
    block("depot", D_of[job_comp[depot_jobs]], IN[depot_jobs], 1, 0, 0.0)
    sj = jobs[serv]
    block("end", OUT[sj], T_of[job_comp[sj]], 1, 0, 0.0)
    keep_s = serv[s_dst] & (job_comp[s_dst] == sid_comp[s_src]) if s_src.size else np.zeros(0, dtype=bool)
    s_src, s_dst, s_ic, s_fc = s_src[keep_s], s_dst[keep_s], s_ic[keep_s], s_fc[keep_s]
    block("start", sid_node[s_src], IN[s_dst], 1, s_ic, s_fc)
    keep_e = ((e_src != e_dst) & serv[e_src] & serv[e_dst] & (job_comp[e_src] == job_comp[e_dst])
              if e_src.size else np.zeros(0, dtype=bool))
    e_src, e_dst, e_ic, e_fc = e_src[keep_e], e_dst[keep_e], e_ic[keep_e], e_fc[keep_e]
    block("edge", OUT[e_src], IN[e_dst], 1, e_ic, e_fc)

    offsets, pos = {}, 0
    for name, t, *_ in blocks:
        offsets[name] = (pos, pos + t.size)
        pos += t.size
    n_arcs = pos

    smcf = min_cost_flow.SimpleMinCostFlow()
    if n_arcs:
        smcf.add_arcs_with_capacity_and_unit_cost(
            np.concatenate([b[1] for b in blocks]), np.concatenate([b[2] for b in blocks]),
            np.concatenate([b[3] for b in blocks]), np.concatenate([b[4] for b in blocks]))
    supply_nodes = np.concatenate([IN, OUT, S_of, T_of])
    S_supply = dcap.copy()
    np.add.at(S_supply, sid_comp, caps)
    supplies = np.concatenate([np.full(n, -1, dtype=np.int64), np.ones(n, dtype=np.int64),
                               S_supply, -S_supply])
    if supply_nodes.size:
        smcf.set_nodes_supplies(supply_nodes, supplies)
    if n_arcs:
        status_raw = smcf.solve()
        ok = status_raw == smcf.OPTIMAL
    else:
        status_raw = smcf.OPTIMAL if n == 0 else smcf.INFEASIBLE
        ok = n == 0
    stats = {
        "backend": "mincostflow",
        "status": OPTIMAL if ok else INFEASIBLE,
        "status_name": "OPTIMAL" if ok else "INFEASIBLE",
        "mcf_status": int(status_raw),
        "components": comps,
        "num_nodes": int(base + 3 * K + len(sid_list)),
        "num_arcs": int(n_arcs),
        "objective": None,
        "solve_time_s": None,
    }
    if not ok:
        stats["solve_time_s"] = time.time() - t0
        return {"status": INFEASIBLE, "message": "No optimal solution"}, stats

    flows = smcf.flows(np.arange(n_arcs, dtype=np.int64)) if n_arcs else np.zeros(0, dtype=np.int64)
    fc_all = np.concatenate([b[5] for b in blocks]) if n_arcs else np.zeros(0)
    objective_float = float(np.dot(fc_all, flows)) if n_arcs else 0.0

    def used(name):
        a, b = offsets[name]
        return np.flatnonzero(flows[a:b] > 0)

    unplanned_idx = used("skip").tolist() if defer_i is not None else []
    succ = np.full(n, -1, dtype=np.int64)
    ue = used("edge")
    succ[e_src[ue]] = e_dst[ue]
    firsts = [("depot", int(depot_jobs[a]), int(job_comp[depot_jobs[a]])) for a in used("depot").tolist()]
    firsts += [("start", sid_list[int(s_src[a])], int(s_dst[a])) for a in used("start").tolist()]

    # Label chains with a company: a start node's own; depot chains fill the component's
    # depot companies' vehicle limits in `companies` order.
    remaining = {c: int(vehicle_limit[c]) for c in companies if c not in sn_companies}
    routes: Dict[tuple, float] = {}
    assignment: Dict[tuple, float] = {}
    reached = np.zeros(n, dtype=bool)
    succ_l = succ.tolist()
    for kind in firsts:
        if kind[0] == "start":
            origin, first, c = kind[1], kind[2], start_nodes[kind[1]]["company"]
        else:
            first, k = kind[1], kind[2]
            c = next(cc for cc in comps[k] if remaining.get(cc, 0) > 0)
            remaining[c] -= 1
            origin = START
        chain = [first]
        while succ_l[chain[-1]] >= 0:
            chain.append(succ_l[chain[-1]])
        routes[(origin, orders[first], c)] = 1.0
        for a, b in zip(chain, chain[1:]):
            routes[(orders[a], orders[b], c)] = 1.0
        routes[(orders[chain[-1]], END, c)] = 1.0
        for j in chain:
            assignment[(orders[j], c)] = 1.0
        reached[chain] = True
    unplanned_mask = np.zeros(n, dtype=bool)
    unplanned_mask[unplanned_idx] = True
    orphaned = np.flatnonzero(~reached & ~unplanned_mask).tolist()
    for j in orphaned:  # on a cycle no start reaches (only if the edges are cyclic)
        c = comps[int(job_comp[j])][0]
        assignment[(orders[j], c)] = 1.0
        if succ_l[j] >= 0:
            routes[(orders[j], orders[succ_l[j]], c)] = 1.0

    objective = float(smcf.optimal_cost())
    result = {
        "instance_id": params.get("instance_id"),
        "status": OPTIMAL,
        "objective": objective,
        "objective_float": objective_float,
        "assignment": assignment,
        "routes": routes,
    }
    if defer_i is not None:
        result["unplanned"] = [orders[j] for j in sorted(unplanned_idx)]
    stats.update(objective=objective, objective_float=objective_float, solve_time_s=time.time() - t0,
                 num_unplanned=len(unplanned_idx), num_orphaned=len(orphaned))
    return result, stats
