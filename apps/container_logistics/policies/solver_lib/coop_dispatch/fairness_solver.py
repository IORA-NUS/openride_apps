from __future__ import annotations

import gc
import logging
import time
from typing import Any, Dict

from ortools.linear_solver import pywraplp

from .base_solver import BaseSolver, create_solver_with_fallback


class FairnessSolver(BaseSolver):
    """Fairness-enhanced network-flow solver."""

    @staticmethod
    def _status_name(status: int) -> str:
        status_names = {
            pywraplp.Solver.OPTIMAL: "OPTIMAL",
            pywraplp.Solver.FEASIBLE: "FEASIBLE",
            pywraplp.Solver.INFEASIBLE: "INFEASIBLE",
            pywraplp.Solver.UNBOUNDED: "UNBOUNDED",
            pywraplp.Solver.ABNORMAL: "ABNORMAL",
            pywraplp.Solver.MODEL_INVALID: "MODEL_INVALID",
            pywraplp.Solver.NOT_SOLVED: "NOT_SOLVED",
        }
        return status_names.get(status, f"UNKNOWN_STATUS_{status}")

    def validate_params(self, params: Dict[str, Any]) -> None:
        super().validate_params(params)
        required = ["order_distance", "lambda_penalty", "fairness_tau"]
        missing = [k for k in required if k not in params]
        if missing:
            raise ValueError(f"Missing fairness params: {missing}")

    def solve(self, params: Dict[str, Any]):
        self.validate_params(params)

        logging.info("Solving job assignment using fairness-enhanced model...")

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
        order_distance = params["order_distance"]

        lambda_penalty = params.get("lambda_penalty", 0.05)
        fairness_tau = params.get("fairness_tau", 0)
        baseline_profit = params.get("baseline_profit", None)
        exchange_limit = params.get("exchange_limit", None)
        bad_orders = params.get("bad_orders", None)
        raw_active_trailers = params.get("raw_active_trailers", None)
        raw_active_trailers_by_company = params.get("raw_active_trailers_by_company", None)
        raw_trailer_detour = params.get("raw_trailer_detour", None)
        raw_trailer_detour_by_company = params.get("raw_trailer_detour_by_company", None)
        released_job_mapping = params.get("released_job_mapping", {})

        if bad_orders is None:
            bad_orders = list(orders)
        else:
            bad_orders = list(bad_orders)

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

        a = {}
        for i in orders:
            c_owner = order_owner[i]
            if i in bad_orders:
                for c in companies:
                    if (c_owner, c) in cooperation:
                        a[(i, c)] = solver.BoolVar(f"a_{i}_{c}")
            else:
                if (c_owner, c_owner) in cooperation:
                    a[(i, c_owner)] = solver.BoolVar(f"a_{i}_{c_owner}")

        # Assignment-eligible (order, company) pairs. For non-bad orders this is
        # owner-only; for bad orders this follows cooperation edges.
        allowed_assignment_pairs = set(a.keys())

        x = {
            (i, j, c): solver.BoolVar(f"x_{i}_{j}_{c}")
            for (i, j) in edges
            for c in companies
            if (
                (
                    i in orders and (i, c) in allowed_assignment_pairs
                    or i in [start_depot, end_depot]
                )
                and (
                    j in orders and (j, c) in allowed_assignment_pairs
                    or j in [start_depot, end_depot]
                )
            )
        }

        y = {i: solver.BoolVar(f"y_{i}") for i in orders}

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
            into_end = solver.Sum(
                x[i, j, c] for (i, j) in edges if j == end_depot and (i, j, c) in x
            )
            solver.Add(out_from_start <= vehicle_limit[c])
            solver.Add(into_end <= vehicle_limit[c])

        for i in bad_orders:
            c_owner = order_owner[i]
            if (i, c_owner) in a:
                solver.Add(y[i] + a[i, c_owner] == 1)
            else:
                solver.Add(y[i] == 1)

        d_by_company = {
            c: sum(order_distance[i] for i in orders if order_owner[i] == c)
            for c in companies
        }

        revenue_expr = {}
        empty_expr = {}
        for c in companies:
            revenue_expr[c] = solver.Sum(order_distance[i] * a[i, c] for i in orders if (i, c) in a)
            empty_expr[c] = solver.Sum(edge_cost[i, j] * x[i, j, c] for (i, j) in edges if (i, j, c) in x)

        if baseline_profit is not None:
            for c in companies:
                if c not in baseline_profit:
                    continue
                base = float(baseline_profit[c])
                min_profit_c = base - fairness_tau * abs(base)
                solver.Add(revenue_expr[c] - empty_expr[c] >= min_profit_c)

        if exchange_limit:
            for c in companies:
                exchanged_orders = solver.Sum(y[i] for i in orders if order_owner[i] == c)
                solver.Add(exchanged_orders <= exchange_limit[c])

        total_swaps = solver.Sum(y[i] for i in orders)
        if use_trailer_cost:
            total_cost = solver.Sum(
                (edge_cost[i, j] + edge_trailer_cost[i, j]) * x[i, j, c] for (i, j, c) in x
            )
        else:
            total_cost = solver.Sum(edge_cost[i, j] * x[i, j, c] for (i, j, c) in x)
        solver.Minimize(total_cost + float(lambda_penalty) * total_swaps)

        status = solver.Solve()
        if status == pywraplp.Solver.INFEASIBLE:
            logging.warning("Model is infeasible.")

        solve_time = time.time() - t0
        wall_time_ms = solver.wall_time()
        iterations = solver.iterations()
        mip_nodes = solver.nodes()

        num_vars = len(a) + len(x) + len(y)
        num_binary_vars = num_vars
        num_constraints = solver.NumConstraints()

        objective_value = solver.Objective().Value() if status == pywraplp.Solver.OPTIMAL else None
        if (objective_value is not None) and use_trailer_cost:
            penalty_reduction = sum(
                vehicle_dispatch_penalty_meters * x[start_depot, i, c].solution_value()
                for i in orders
                for c in companies
                if (start_depot, i, c) in x and not released_job_mapping.get(i, False)
            )
            objective_value -= penalty_reduction

        if status == pywraplp.Solver.OPTIMAL or status == pywraplp.Solver.FEASIBLE:
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

            empty_cost_val = sum(edge_cost[i, j] * x[i, j, c].solution_value() for (i, j, c) in x)
            swap_count_val = sum(y[i].solution_value() for i in orders)

            revenue_by_company = {}
            empty_cost_by_company = {}
            profit_by_company = {}
            for c in companies:
                rev = sum(
                    order_distance[i] * (a[i, c].solution_value() if (i, c) in a else 0.0)
                    for i in orders
                )
                emp = sum(
                    edge_cost[i, j] * (x[i, j, c].solution_value() if (i, j, c) in x else 0.0)
                    for (i, j) in edges
                )
                revenue_by_company[c] = rev
                empty_cost_by_company[c] = emp
                profit_by_company[c] = rev - emp - trailer_detour_by_company.get(c, 0.0)

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
                "status": status,
                "status_name": self._status_name(status),
                "objective_withswaps": objective_value,
                "objective": empty_cost_val,
                "swap_count": swap_count_val,
                "assignment": {(i, c): a[i, c].solution_value() for (i, c) in a},
                "routes": {(i, j, c): x[i, j, c].solution_value() for (i, j, c) in x},
                "swapped": {i: y[i].solution_value() for i in y},
                "assigned_distance_by_company": {
                    c: sum(order_distance[i] * (a[i, c].solution_value() if (i, c) in a else 0.0) for i in orders)
                    for c in companies
                },
                "D_by_company": d_by_company,
                "revenue_by_company": revenue_by_company,
                "empty_cost_by_company": empty_cost_by_company,
                "trailer_detour_by_company": trailer_detour_by_company,
                "profit_by_company": profit_by_company,
                "active_trailers_by_company": active_trailers_by_company,
                "edge_attribution": edge_attribution,
            }

            result_stats = {
                "instance_id": instance_id,
                "status": status,
                "objective_withswaps": objective_value,
                "objective": empty_cost_val,
                "swap_count": swap_count_val,
                "assigned_distance_by_company": {
                    c: sum(order_distance[i] * (a[i, c].solution_value() if (i, c) in a else 0.0) for i in orders)
                    for c in companies
                },
                "D_by_company": d_by_company,
                "revenue_by_company": revenue_by_company,
                "empty_cost_by_company": empty_cost_by_company,
                "trailer_detour_by_company": trailer_detour_by_company,
                "profit_by_company": profit_by_company,
                "total_active_trailers": total_active_trailers,
                "total_trailer_detour": total_trailer_detour,
                "active_trailers_by_company": active_trailers_by_company,
                "edge_attribution": edge_attribution,
                "num_variables": num_vars,
                "num_binary_variables": num_binary_vars,
                "num_constraints": num_constraints,
                "solve_time_s": solve_time,
                "wall_time_ms": wall_time_ms,
                "iterations": iterations,
                "mip_nodes": mip_nodes,
                "raw_cost": raw_cost,
                "raw_active_trailers": raw_active_trailers,
                "raw_trailer_detour": raw_trailer_detour,
                "raw_active_trailers_by_company": raw_active_trailers_by_company,
                "raw_trailer_detour_by_company": raw_trailer_detour_by_company,
            }
        else:
            result = {"status": status, "message": "No optimal solution"}
            total_active_trailers = None
            total_trailer_detour = None
            # active_trailers_by_company = {}
            edge_attribution = {}
            zero_by_company = {str(c): 0.0 for c in companies}

            result_stats = {
                "instance_id": instance_id,
                "status": status,
                "status_name": self._status_name(status),
                "objective_withswaps": 0,
                "objective": 0,
                "swap_count": 0,
                "assigned_distance_by_company": zero_by_company.copy(),
                "D_by_company": d_by_company,
                "revenue_by_company": zero_by_company.copy(),
                "empty_cost_by_company": zero_by_company.copy(),
                "trailer_detour_by_company": zero_by_company.copy(),
                "profit_by_company": zero_by_company.copy(),
                "total_active_trailers": total_active_trailers,
                "total_trailer_detour": total_trailer_detour,
                "active_trailers_by_company": zero_by_company.copy(),
                "edge_attribution": edge_attribution,
                "num_variables": num_vars,
                "num_binary_variables": num_binary_vars,
                "num_constraints": num_constraints,
                "solve_time_s": solve_time,
                "wall_time_ms": wall_time_ms,
                "iterations": iterations,
                "mip_nodes": mip_nodes,
                "raw_cost": raw_cost,
                "raw_active_trailers": raw_active_trailers,
                "raw_trailer_detour": raw_trailer_detour,
                "raw_active_trailers_by_company": raw_active_trailers_by_company,
                "raw_trailer_detour_by_company": raw_trailer_detour_by_company,
            }

        del solver
        gc.collect()
        return result, result_stats
