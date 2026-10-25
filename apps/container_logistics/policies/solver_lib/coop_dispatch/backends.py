"""Backend choice per cooperation component, and the MIP quality guard (plan addendum B2 / M5).

OpenRide addition, not upstream.

- :func:`component_params` slices a ``NetworkFlowSolver`` params dict to one cooperation
  component (the components are independent: no order, edge, start node or vehicle is
  shared between them).
- :func:`solve_by_component` solves each component with the min-cost flow where it is
  exact (:func:`min_cost_flow.mcf_applicable`) and the backend allows it, else with the MIP,
  and records which backend ran per component (``stats["components"]``).
- :func:`served_upper_bound` is a single-commodity relaxation that bounds how many jobs ANY
  plan of the component can serve; :func:`served_reference` is the same relaxation at the
  instance's own deferral penalty; :func:`quality_guard` flags a time-limited MIP incumbent
  serving fewer than ``threshold`` of the reference (``poor_incumbent``) so the caller can
  keep its previous plan.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from .min_cost_flow import cooperation_components, mcf_applicable, solve_min_cost_flow
from .network_flow_solver import NetworkFlowSolver

BACKENDS = ("auto", "mincostflow", "mip")
POOR_INCUMBENT = "poor_incumbent"
OPTIMAL, FEASIBLE = 0, 1


def component_params(params: Dict[str, Any], component) -> Dict[str, Any]:
    comp = list(component)
    cs = set(comp)
    owner = params["order_owner"]
    orders = [o for o in params["orders"] if owner[o] in cs]
    oset = set(orders)
    edges = [e for e in params["edges_input"] if e[0] in oset and e[1] in oset]
    eset = set(edges)
    out = dict(params)
    out.update(
        companies=[c for c in params["companies"] if c in cs],
        orders=orders,
        cooperation={(a, b) for (a, b) in params["cooperation"] if a in cs and b in cs},
        vehicle_limit={c: v for c, v in params["vehicle_limit"].items() if c in cs},
        edges_input=edges,
        edge_cost_input={e: v for e, v in params["edge_cost_input"].items() if e in eset},
        edge_trailer_cost_input={e: v for e, v in params.get("edge_trailer_cost_input", {}).items() if e in eset},
        instance_id=f"{params.get('instance_id')}:{'+'.join(map(str, comp))}",
    )
    if params.get("start_nodes") is not None:
        sn = {s: spec for s, spec in params["start_nodes"].items() if spec["company"] in cs}
        se = [e for e in params.get("start_edges", []) if e[0] in sn and e[1] in oset]
        ses = set(se)
        out.update(start_nodes=sn, start_edges=se,
                   start_edge_cost={e: v for e, v in params.get("start_edge_cost", {}).items() if e in ses})
    if params.get("eligible") is not None:
        out["eligible"] = {(o, c) for (o, c) in params["eligible"] if o in oset and c in cs}
    if params.get("class_attrs") is not None:
        out["class_attrs"] = {c: v for c, v in params["class_attrs"].items() if c in cs}
    if params.get("released_job_mapping"):
        out["released_job_mapping"] = {o: v for o, v in params["released_job_mapping"].items() if o in oset}
    return out


def served_upper_bound(params: Dict[str, Any]) -> int:
    """Most jobs any plan of ``params`` can serve: a single-commodity relaxation (every
    company of a component pooled; a job servable if ANY company may serve it; an edge kept
    if some company may serve both ends), solved as a min-cost flow that maximises served
    jobs first (the deferral penalty exceeds the sum of every arc cost). Every MIP plan maps
    onto a flow of this network, so its served count is <= the bound."""
    return _relaxed_served(params, penalty=None)


def served_reference(params: Dict[str, Any]) -> int:
    """Jobs served by the optimum of the same relaxation at the instance's OWN deferral
    penalty (``defer_penalty``; without one, every servable job must be served). This is
    what the quality guard compares an incumbent with: it is <= :func:`served_upper_bound`,
    equal to it when the penalty dominates every arc cost (plan D12(g)), and it does not
    flag an incumbent for deferring jobs the penalty makes not worth serving."""
    return _relaxed_served(params, penalty=params.get("defer_penalty"))


def _relaxed_served(params: Dict[str, Any], penalty) -> int:
    owner, coop = params["order_owner"], set(params["cooperation"])
    eligible = params.get("eligible")
    eligible = None if eligible is None else set(eligible)
    comps = cooperation_components(params["companies"], coop)
    relaxed_owner = {}
    servers = {}
    for o in params["orders"]:
        cos = {c for c in params["companies"] if (owner[o], c) in coop
               and (eligible is None or (o, c) in eligible)}
        servers[o] = cos
    comp_of = {c: k for k, comp in enumerate(comps) for c in comp}
    # One pooled company per component; jobs no company may serve are dropped (unservable).
    pooled = [f"__pool{k}" for k in range(len(comps))]
    for o in params["orders"]:
        relaxed_owner[o] = pooled[comp_of[owner[o]]]
    orders = [o for o in params["orders"] if servers[o]]
    oset = set(orders)
    edges = [(i, j) for (i, j) in dict.fromkeys(params["edges_input"])
             if i in oset and j in oset and servers[i] & servers[j]]
    sn = params.get("start_nodes") or {}
    sn_cos = {s["company"] for s in sn.values()}
    start_nodes = {s: {"company": pooled[comp_of[spec["company"]]]} for s, spec in sn.items()}
    start_edges = [(s, j) for (s, j) in params.get("start_edges", [])
                   if s in sn and j in oset and sn[s]["company"] in servers[j]]
    # Depot vehicles get their own pooled class beside the start nodes' (a class with start
    # nodes has no depot arcs), in the same complete component.
    depot = [f"__depot{k}" for k in range(len(comps))]
    vl = {p: 0 for p in pooled + depot}
    for c, v in params["vehicle_limit"].items():
        if c in comp_of and c not in sn_cos:
            vl[depot[comp_of[c]]] += int(v)
    big = 1 + sum(abs(round(params["edge_cost_input"][e])) for e in edges) \
        + sum(abs(round(params.get("start_edge_cost", {})[e])) for e in start_edges)
    if penalty is not None:
        big = penalty
    relaxed = {
        "instance_id": "served_bound",
        "companies": pooled + depot,
        "orders": orders,
        "order_owner": relaxed_owner,
        "cooperation": {(a, b) for k in range(len(comps)) for a in (pooled[k], depot[k])
                        for b in (pooled[k], depot[k])},
        "vehicle_limit": vl,
        "edges_input": edges,
        "edge_cost_input": {e: params["edge_cost_input"][e] for e in edges},
        "use_trailer_cost": False,
        "defer_penalty": big,
    }
    if sn:
        relaxed.update(start_nodes=start_nodes, start_edges=start_edges,
                       start_edge_cost={e: params["start_edge_cost"][e] for e in start_edges})
    result, _ = solve_min_cost_flow(relaxed)
    return len(orders) - len(result["unplanned"])


def quality_guard(served: int, bound: int, threshold: float = 0.95) -> Dict[str, Any]:
    """``{"ok", "ratio", "note"}``: ``note`` is ``"poor_incumbent"`` when ``served`` is below
    ``threshold`` of ``bound`` (a bound of 0 is always ok)."""
    if bound < 0 or served < 0:
        raise ValueError("served and bound must be >= 0")
    ratio = 1.0 if bound == 0 else served / bound
    ok = ratio >= threshold
    return {"ok": ok, "ratio": ratio, "served": served, "bound": bound,
            "note": None if ok else POOR_INCUMBENT}


def _served(result, orders) -> int:
    if "unplanned" in result:
        return len(orders) - len(result["unplanned"])
    return len(orders) if result.get("routes") is not None else 0


def solve_by_component(params: Dict[str, Any], backend: str = "auto", *,
                       guard_threshold: float = 0.95):
    """Solve ``params`` one cooperation component at a time.

    ``backend``: ``"auto"`` = min-cost flow where :func:`mcf_applicable`, MIP elsewhere;
    ``"mincostflow"`` = the same (a component where the flow is not exact falls back to the
    MIP; it is never solved inexactly); ``"mip"`` = MIP everywhere. Every MIP component is
    that returns a non-OPTIMAL incumbent is checked by :func:`quality_guard` against
    :func:`served_reference`.

    Returns ``(result, stats)``: ``result`` merges the components' ``routes`` /
    ``assignment`` / ``unplanned`` and sums ``objective`` (``None`` if any component has no
    usable solution); ``stats["components"]`` lists per component its companies, backend,
    reason, status, objective, served and (MIP) guard verdict; ``stats["poor_incumbent"]``
    is true when any MIP component failed the guard.
    """
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS} (got {backend!r})")
    t0 = time.time()
    comps = cooperation_components(params["companies"], params["cooperation"])
    merged = {"instance_id": params.get("instance_id"), "status": OPTIMAL, "objective": 0.0,
              "assignment": {}, "routes": {}}
    if params.get("defer_penalty") is not None:
        merged["unplanned"] = []
    rows = []
    for comp in comps:
        sub = component_params(params, comp)
        ok, why = mcf_applicable(comp, sub["cooperation"], sub.get("class_attrs"))
        use_mcf = ok and backend != "mip"
        if use_mcf:
            try:
                result, cstats = solve_min_cost_flow(sub)
            except ValueError as exc:  # e.g. non-uniform eligible: not exact, so MIP
                use_mcf, why = False, str(exc)
        if not use_mcf:
            result, cstats = NetworkFlowSolver().solve(sub)
        row = {"companies": comp, "backend": "mincostflow" if use_mcf else "mip",
               "reason": why,
               "status": result.get("status"), "objective": result.get("objective"),
               "orders": len(sub["orders"])}
        usable = result.get("routes") is not None
        row["served"] = _served(result, sub["orders"]) if usable else 0
        if not use_mcf and usable and result.get("status") != OPTIMAL:
            # Only an incumbent a limit stopped can be poor; an OPTIMAL plan is the optimum.
            row["guard"] = quality_guard(row["served"], served_reference(sub), guard_threshold)
        rows.append(row)
        if not usable:
            merged["status"] = result.get("status")
            merged["objective"] = None
            continue
        if result.get("status") == FEASIBLE and merged["status"] == OPTIMAL:
            merged["status"] = FEASIBLE
        if merged["objective"] is not None:
            merged["objective"] += float(result["objective"])
        merged["assignment"].update({k: v for k, v in result["assignment"].items() if v > 0.5})
        merged["routes"].update({k: v for k, v in result["routes"].items() if v > 0.5})
        if "unplanned" in merged:
            merged["unplanned"] += list(result.get("unplanned", []))
    if merged["objective"] is None:
        merged.pop("routes"); merged.pop("assignment")
        merged["message"] = "No usable solution in at least one component"
    stats = {"backend": backend, "components": rows,
             "poor_incumbent": any(r.get("guard", {}).get("ok") is False for r in rows),
             "solve_time_s": time.time() - t0}
    return merged, stats
