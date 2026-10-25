from __future__ import annotations

import gc
import logging
import time
from typing import Any, Dict

from ortools.linear_solver import pywraplp

from .base_solver import BaseSolver, create_solver_with_fallback


class NetworkFlowSolver(BaseSolver):
    """Classic network-flow solver."""

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

        t0 = time.time()

        solver = create_solver_with_fallback()
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
        }

        x = {
            (i, j, c): solver.BoolVar(f"x_{i}_{j}_{c}")
            for (i, j) in edges
            for c in companies
            if (
                (
                    i in orders and (order_owner[i], c) in cooperation
                    or i in [start_depot, end_depot]
                )
                and (
                    j in orders and (order_owner[j], c) in cooperation
                    or j in [start_depot, end_depot]
                )
            )
        }

        for i in orders:
            solver.Add(solver.Sum(a[i, c] for c in companies if (i, c) in a) == 1)

        for i in orders:
            for c in companies:
                if (i, c) not in a:
                    continue
                in_edges = [x[j, i, c] for (j, i2) in edges if i2 == i and (j, i, c) in x]
                out_edges = [x[i, j, c] for (i2, j) in edges if i2 == i and (i, j, c) in x]
                solver.Add(solver.Sum(in_edges) == a[i, c])
                solver.Add(solver.Sum(out_edges) == a[i, c])

        for c in companies:
            for n in orders:
                inflow = solver.Sum(x[i, n, c] for (i, j) in edges if j == n and (i, j, c) in x)
                outflow = solver.Sum(x[n, j, c] for (i, j) in edges if i == n and (i, j, c) in x)
                solver.Add(inflow == outflow)

        for c in companies:
            out_from_start = solver.Sum(
                x[i, j, c] for (i, j) in edges if i == start_depot and (i, j, c) in x
            )
            solver.Add(out_from_start <= vehicle_limit[c])
            into_end = solver.Sum(
                x[i, j, c] for (i, j) in edges if j == end_depot and (i, j, c) in x
            )
            solver.Add(into_end <= vehicle_limit[c])

        if use_trailer_cost:
            solver.Minimize(
                solver.Sum((edge_cost[i, j] + edge_trailer_cost[i, j]) * x[i, j, c] for (i, j, c) in x)
            )
        else:
            solver.Minimize(solver.Sum(edge_cost[i, j] * x[i, j, c] for (i, j, c) in x))

        num_vars = len(a) + len(x)
        num_binary_vars = num_vars
        num_constraints = len(orders) + 2 * len(a) + len(companies) * len(orders) + 2 * len(companies)

        status = solver.Solve()
        solve_time = time.time() - t0
        wall_time_ms = solver.wall_time()
        iterations = solver.iterations()
        mip_nodes = solver.nodes()

        objective_value = solver.Objective().Value() if status == pywraplp.Solver.OPTIMAL else None
        if (objective_value is not None) and use_trailer_cost:
            penalty_deduction = sum(
                vehicle_dispatch_penalty_meters * x[start_depot, i, c].solution_value()
                for i in orders
                for c in companies
                if (start_depot, i, c) in x and not released_job_mapping.get(i, False)
            )
            objective_value -= penalty_deduction

        if status == pywraplp.Solver.OPTIMAL:
            active_trailers_by_company = {}
            trailer_detour_by_company = {}

            for c in companies:
                dispatched = sum(
                    x[start_depot, i, c].solution_value()
                    for i in orders
                    if (start_depot, i, c) in x and not released_job_mapping.get(i, False)
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

        del solver
        gc.collect()

        return result, result_stats
