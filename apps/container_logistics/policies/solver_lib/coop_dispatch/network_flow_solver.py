from __future__ import annotations

import gc
import logging
import time
from typing import Any, Dict

from ortools.linear_solver import pywraplp

from .base_solver import BaseSolver, create_solver_with_fallback, solve_model

_STATUS_NAMES = {0: "OPTIMAL", 1: "FEASIBLE", 2: "INFEASIBLE", 3: "UNBOUNDED",
                 4: "ABNORMAL", 5: "MODEL_INVALID", 6: "NOT_SOLVED"}


def start_capacity(spec, sid=None) -> int:
    """A start node's ``capacity`` (default 1): how many chains it may head (OpenRide).

    A start node of capacity c stands for c interchangeable trucks (same class, same
    position, same ready time); its out-flow is <= c.
    """
    cap = spec.get("capacity", 1)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError(f"start node {sid!r}: capacity must be an int >= 1 (got {cap!r})")
    return cap


class NetworkFlowSolver(BaseSolver):
    """Classic network-flow solver.

    OpenRide options (VENDOR.md), all default off; with none of them the model, its
    constraint order and the returned result/stats are exactly upstream's:

    - ``start_nodes`` {sid: {"company": c, "capacity": k (default 1)}} + ``start_edges``
      [(sid, order)] + ``start_edge_cost`` {(sid, order): m}: truck sources of capacity k
      inside the company's commodity; such a company has no depot start arcs and no
      vehicle limit.
    - ``defer_penalty`` P: an order may stay unplanned at cost P (result ``unplanned``).
    - ``eligible`` {(order, company)}: only these pairs may serve.
    - ``time_limit_s`` / ``mip_gap`` / ``accept_feasible``.
    """

    def solve(self, params: Dict[str, Any]):
        self.validate_params(params)

        logging.info("Solving job assignment using network flow model...")

        instance_id = params["instance_id"]
        companies = params["companies"]
        orders = params["orders"]
        order_owner = self.normalize_order_owner(params)
        cooperation = params["cooperation"]
        vehicle_limit = params["vehicle_limit"]
        edges_input = params["edges_input"]
        edge_cost_input = params["edge_cost_input"]
        use_trailer_cost = params.get("use_trailer_cost", True)
        edge_trailer_cost_input = params["edge_trailer_cost_input"]
        raw_cost = params["raw_cost"]
        raw_active_trailers = params.get("raw_active_trailers", None)
        raw_trailer_detour = params.get("raw_trailer_detour", None)
        released_job_mapping = params.get("released_job_mapping", {})

        # ---- OpenRide options (all default off) ----
        start_nodes = params.get("start_nodes")
        defer_penalty = params.get("defer_penalty")
        eligible = params.get("eligible")
        time_limit_s = params.get("time_limit_s")
        mip_gap = params.get("mip_gap")
        accept_feasible = bool(params.get("accept_feasible", False))
        options_on = (
            start_nodes is not None or defer_penalty is not None or eligible is not None
            or time_limit_s is not None or mip_gap is not None or accept_feasible
        )
        if eligible is not None:
            eligible = set(eligible)
        order_set = set(orders)
        start_arcs = self._start_arcs(params, start_nodes, order_set, companies)
        sn_companies = {spec["company"] for spec in (start_nodes or {}).values()}

        t0 = time.time()

        solver = create_solver_with_fallback(time_limit_s=time_limit_s, mip_gap=mip_gap)
        if not solver:
            return {"status": -1, "message": "Solver initialization failed: no available backend"}

        start_depot, end_depot = "start", "end"

        edges = edges_input.copy()
        edge_cost = edge_cost_input.copy()
        edge_trailer_cost = edge_trailer_cost_input.copy()

        for o in orders:
            edge_cost[(start_depot, o)] = 0
            edge_cost[(o, end_depot)] = 0
            edges.append((start_depot, o))
            edges.append((o, end_depot))

            vehicle_dispatch_penalty_meters = 30000
            if released_job_mapping.get(o, False):
                edge_trailer_cost[(start_depot, o)] = 0
            else:
                edge_trailer_cost[(start_depot, o)] = vehicle_dispatch_penalty_meters
            edge_trailer_cost[(o, end_depot)] = 0

        a = {
            (i, c): solver.BoolVar(f"a_{i}_{c}")
            for i in orders
            for c in companies
            if (order_owner[i], c) in cooperation
            and (eligible is None or (i, c) in eligible)
        }

        # Same keys as upstream's ``i in orders and (order_owner[i], c) in cooperation``
        # test, plus the ``eligible`` filter (both are exactly ``(i, c) in a``); a set for
        # membership instead of the orders list. A company with start nodes has no depot
        # start arcs.
        depots = (start_depot, end_depot)
        x = {
            (i, j, c): solver.BoolVar(f"x_{i}_{j}_{c}")
            for (i, j) in edges
            for c in companies
            if (
                (
                    i in order_set and (i, c) in a
                    or i in depots
                )
                and (
                    j in order_set and (j, c) in a
                    or j in depots
                )
                and not (i == start_depot and c in sn_companies)
            )
        }

        # Edge index (always on): one pass over ``edges`` IN ORDER, duplicates kept, so
        # every adjacency list holds exactly the terms, in exactly the order, that
        # upstream's per-(order, company) scans over all edges collected.
        out_adj: Dict[Any, list] = {}
        in_adj: Dict[Any, list] = {}
        for (i, j) in edges:
            for c in companies:
                v = x.get((i, j, c))
                if v is not None:
                    out_adj.setdefault((i, c), []).append(v)
                    in_adj.setdefault((j, c), []).append(v)

        sn_out: Dict[Any, list] = {sid: [] for sid in (start_nodes or {})}
        for (sid, j, c) in start_arcs:
            if (j, c) not in a:
                continue
            v = solver.BoolVar(f"x_{sid}_{j}_{c}")
            x[sid, j, c] = v
            edge_cost[(sid, j)] = start_arcs[sid, j, c]
            edge_trailer_cost[(sid, j)] = 0
            sn_out[sid].append(v)
            in_adj.setdefault((j, c), []).append(v)

        u = {}
        if defer_penalty is not None:
            u = {i: solver.BoolVar(f"u_{i}") for i in orders}

        for i in orders:
            if defer_penalty is None:
                solver.Add(solver.Sum(a[i, c] for c in companies if (i, c) in a) == 1)
            else:
                solver.Add(solver.Sum(a[i, c] for c in companies if (i, c) in a) + u[i] == 1)

        for i in orders:
            for c in companies:
                if (i, c) not in a:
                    continue
                in_edges = in_adj.get((i, c), [])
                out_edges = out_adj.get((i, c), [])
                solver.Add(solver.Sum(in_edges) == a[i, c])
                solver.Add(solver.Sum(out_edges) == a[i, c])

        for c in companies:
            for n in orders:
                inflow = solver.Sum(v for v in in_adj.get((n, c), []))
                outflow = solver.Sum(v for v in out_adj.get((n, c), []))
                solver.Add(inflow == outflow)

        for c in companies:
            if c in sn_companies:
                continue
            out_from_start = solver.Sum(v for v in out_adj.get((start_depot, c), []))
            solver.Add(out_from_start <= vehicle_limit[c])
            into_end = solver.Sum(v for v in in_adj.get((end_depot, c), []))
            solver.Add(into_end <= vehicle_limit[c])

        for sid, outs in sn_out.items():
            solver.Add(solver.Sum(outs) <= start_capacity(start_nodes[sid], sid))

        if use_trailer_cost:
            objective = solver.Sum(
                (edge_cost[i, j] + edge_trailer_cost[i, j]) * x[i, j, c] for (i, j, c) in x
            )
        else:
            objective = solver.Sum(edge_cost[i, j] * x[i, j, c] for (i, j, c) in x)
        if defer_penalty is not None:
            objective = objective + solver.Sum(float(defer_penalty) * u[i] for i in orders)
        solver.Minimize(objective)

        num_vars = len(a) + len(x) + len(u)
        num_binary_vars = num_vars
        num_constraints = len(orders) + 2 * len(a) + len(companies) * len(orders) + 2 * len(companies)
        if options_on:
            num_constraints += len(sn_out) - 2 * len(sn_companies)

        status = solve_model(solver)
        solve_time = time.time() - t0
        wall_time_ms = solver.wall_time()
        iterations = solver.iterations()
        mip_nodes = solver.nodes()

        usable = status == pywraplp.Solver.OPTIMAL or (
            accept_feasible and status == pywraplp.Solver.FEASIBLE
        )
        objective_value = solver.Objective().Value() if usable else None
        if (objective_value is not None) and use_trailer_cost:
            penalty_deduction = sum(
                vehicle_dispatch_penalty_meters * x[start_depot, i, c].solution_value()
                for i in orders
                for c in companies
                if (start_depot, i, c) in x and not released_job_mapping.get(i, False)
            )
            objective_value -= penalty_deduction

        if usable:
            active_trailers_by_company = {}
            trailer_detour_by_company = {}

            for c in companies:
                dispatched = sum(
                    x[start_depot, i, c].solution_value()
                    for i in orders
                    if (start_depot, i, c) in x and not released_job_mapping.get(i, False)
                )
                dispatched += sum(
                    x[sid, i, c2].solution_value()
                    for (sid, i, c2) in start_arcs
                    if c2 == c and (sid, i, c2) in x and not released_job_mapping.get(i, False)
                )
                pickup_jobs = sum(
                    a[i, c].solution_value()
                    for i in orders
                    if (i, c) in a and released_job_mapping.get(i, False)
                )
                active_trailers_by_company[c] = dispatched + pickup_jobs

                detour = sum(
                    edge_trailer_cost[i, j] * x[i, j, c].solution_value()
                    for (i, j) in edges
                    if i != start_depot
                    and j != end_depot
                    and (i, j, c) in x
                    and (i, j) in edge_trailer_cost
                )
                trailer_detour_by_company[c] = detour

            total_active_trailers = sum(active_trailers_by_company.values())
            total_trailer_detour = sum(trailer_detour_by_company.values())

            raw_edge_list = params.get("raw_edge_list", [])
            raw_edge_set = set(raw_edge_list)
            edge_attribution = {
                "raw_edge": {"count": 0, "cost": 0.0},
                "intra_reordered": {"count": 0, "cost": 0.0},
                "inter_swapped": {"count": 0, "cost": 0.0},
            }

            for (i, j) in edges:
                if i == start_depot or j == end_depot:
                    continue
                for c in companies:
                    if (i, j, c) in x and x[i, j, c].solution_value() > 0.5:
                        val = edge_cost[i, j]
                        if (i, j) in raw_edge_set:
                            edge_attribution["raw_edge"]["count"] += 1
                            edge_attribution["raw_edge"]["cost"] += val
                        elif order_owner[i] == order_owner[j]:
                            edge_attribution["intra_reordered"]["count"] += 1
                            edge_attribution["intra_reordered"]["cost"] += val
                        else:
                            edge_attribution["inter_swapped"]["count"] += 1
                            edge_attribution["inter_swapped"]["cost"] += val

            result = {
                "instance_id": instance_id,
                "cooperation": cooperation,
                "status": status,
                "objective": objective_value,
                "assignment": {(i, c): a[i, c].solution_value() for (i, c) in a},
                "routes": {(i, j, c): x[i, j, c].solution_value() for (i, j, c) in x},
                "active_trailers_by_company": active_trailers_by_company,
                "trailer_detour_by_company": trailer_detour_by_company,
                "edge_attribution": edge_attribution,
            }
            if defer_penalty is not None:
                result["unplanned"] = [i for i in orders if u[i].solution_value() > 0.5]
        else:
            result = {"status": status, "message": "No optimal solution"}
            total_active_trailers = None
            total_trailer_detour = None
            active_trailers_by_company = {}
            trailer_detour_by_company = {}
            edge_attribution = {}

        result_stats = {
            "instance_id": instance_id,
            "status": status,
            "objective": objective_value,
            "raw_cost": raw_cost,
            "total_active_trailers": total_active_trailers,
            "total_trailer_detour": total_trailer_detour,
            "active_trailers_by_company": active_trailers_by_company,
            "trailer_detour_by_company": trailer_detour_by_company,
            "edge_attribution": edge_attribution,
            "num_variables": num_vars,
            "num_binary_variables": num_binary_vars,
            "num_constraints": num_constraints,
            "solve_time_s": solve_time,
            "wall_time_ms": wall_time_ms,
            "iterations": iterations,
            "mip_nodes": mip_nodes,
            "raw_active_trailers": raw_active_trailers,
            "raw_trailer_detour": raw_trailer_detour,
        }
        if options_on:
            result_stats["status_name"] = _STATUS_NAMES.get(status, str(status))
            result_stats["num_constraints_model"] = solver.NumConstraints()
            result_stats["num_start_nodes"] = len(sn_out)
            result_stats["num_start_arcs"] = sum(len(v) for v in sn_out.values())
            result_stats["time_limit_s"] = time_limit_s
            result_stats["mip_gap"] = mip_gap
            if defer_penalty is not None:
                result_stats["num_unplanned"] = len(result["unplanned"]) if usable else None

        del solver
        gc.collect()

        return result, result_stats

    @staticmethod
    def _start_arcs(params, start_nodes, order_set, companies):
        """Validated start arcs ``{(sid, order, company): cost}`` in ``start_edges`` order."""
        if start_nodes is None:
            if params.get("start_edges"):
                raise ValueError("start_edges given without start_nodes")
            return {}
        company_set = set(companies)
        for sid, spec in start_nodes.items():
            if sid in order_set or sid in ("start", "end"):
                raise ValueError(f"start node id {sid!r} collides with an order id or a depot")
            if spec.get("company") not in company_set:
                raise ValueError(f"start node {sid!r}: company {spec.get('company')!r} is not in companies")
            start_capacity(spec, sid)
        cost = params.get("start_edge_cost", {})
        arcs = {}
        for (sid, j) in params.get("start_edges", []):
            if sid not in start_nodes:
                raise ValueError(f"start edge ({sid!r}, {j!r}) names an unknown start node")
            if j not in order_set:
                raise ValueError(f"start edge ({sid!r}, {j!r}) names an unknown order")
            key = (sid, j, start_nodes[sid]["company"])
            if key in arcs:
                raise ValueError(f"duplicate start edge ({sid!r}, {j!r})")
            arcs[key] = float(cost[(sid, j)])
        return arcs
