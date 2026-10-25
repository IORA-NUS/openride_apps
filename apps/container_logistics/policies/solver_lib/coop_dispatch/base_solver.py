from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict

from ortools.linear_solver import pywraplp


def _gurobi_thread_param() -> int | None:
    """Read GUROBI_THREADS from environment. Returns None if not set or invalid."""
    raw = os.environ.get("GUROBI_THREADS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return None


class _GVar:
    """Thin proxy around gurobipy.Var that adds solution_value() and delegates
    arithmetic/comparison to the underlying Gurobi variable so that LinExpr
    expressions and TempConstrs are built from real gurobipy objects."""

    __slots__ = ("_v",)

    def __init__(self, v: Any) -> None:
        self._v = v

    def solution_value(self) -> float:
        try:
            return float(self._v.X)
        except Exception:
            return 0.0

    # ---------- arithmetic (returns native gurobipy LinExpr) ----------

    def __mul__(self, other: Any) -> Any:
        return self._v * (other._v if isinstance(other, _GVar) else other)

    def __rmul__(self, other: Any) -> Any:
        return (other._v if isinstance(other, _GVar) else other) * self._v

    def __add__(self, other: Any) -> Any:
        return self._v + (other._v if isinstance(other, _GVar) else other)

    def __radd__(self, other: Any) -> Any:
        return (other._v if isinstance(other, _GVar) else other) + self._v

    def __sub__(self, other: Any) -> Any:
        return self._v - (other._v if isinstance(other, _GVar) else other)

    def __rsub__(self, other: Any) -> Any:
        return (other._v if isinstance(other, _GVar) else other) - self._v

    # ---------- comparisons (return native gurobipy TempConstr) ----------

    def __eq__(self, other: Any) -> Any:  # type: ignore[override]
        return self._v == (other._v if isinstance(other, _GVar) else other)

    def __le__(self, other: Any) -> Any:
        return self._v <= (other._v if isinstance(other, _GVar) else other)

    def __ge__(self, other: Any) -> Any:
        return self._v >= (other._v if isinstance(other, _GVar) else other)

    def __hash__(self) -> int:
        return hash(self._v)

    def __repr__(self) -> str:
        return f"_GVar({self._v})"


class GurobiSolverAdapter:
    """Exposes a pywraplp-compatible interface backed by gurobipy (Gurobi 13).

    Status integer constants match pywraplp.Solver so that callers comparing
    ``status == pywraplp.Solver.OPTIMAL`` continue to work unchanged.
    """

    # Mirror pywraplp.Solver status constants.
    OPTIMAL = 0
    FEASIBLE = 1
    INFEASIBLE = 2
    UNBOUNDED = 3
    ABNORMAL = 4
    MODEL_INVALID = 5
    NOT_SOLVED = 6

    def __init__(self, threads: int | None = None) -> None:
        import gurobipy as gp
        self._gp = gp
        self._model = gp.Model()
        if threads is not None:
            self._model.Params.Threads = threads
        self._wall_ms: float = 0.0

    # ---------- variable creation ----------

    def BoolVar(self, name: str) -> _GVar:
        v = self._model.addVar(vtype=self._gp.GRB.BINARY, name=name)
        return _GVar(v)

    def IntVar(self, lb: float, ub: float, name: str) -> _GVar:
        v = self._model.addVar(lb=lb, ub=ub, vtype=self._gp.GRB.INTEGER, name=name)
        return _GVar(v)

    def NumVar(self, lb: float, ub: float, name: str) -> _GVar:
        v = self._model.addVar(lb=lb, ub=ub, vtype=self._gp.GRB.CONTINUOUS, name=name)
        return _GVar(v)

    # ---------- model building ----------

    def Sum(self, iterable: Any) -> Any:
        """Return quicksum, unwrapping any _GVar proxies first."""
        return self._gp.quicksum(
            item._v if isinstance(item, _GVar) else item
            for item in iterable
        )

    def Add(self, constraint: Any) -> None:
        self._model.addConstr(constraint)

    def Minimize(self, expr: Any) -> None:
        self._model.setObjective(expr, self._gp.GRB.MINIMIZE)

    def Maximize(self, expr: Any) -> None:
        self._model.setObjective(expr, self._gp.GRB.MAXIMIZE)

    # ---------- solve ----------

    def Solve(self) -> int:
        t0 = time.monotonic()
        self._model.update()
        self._model.optimize()
        self._wall_ms = (time.monotonic() - t0) * 1000.0

        GRB = self._gp.GRB
        s = self._model.Status
        if s == GRB.OPTIMAL:
            return self.OPTIMAL
        if s == GRB.SUBOPTIMAL:
            return self.FEASIBLE
        if s in (GRB.INFEASIBLE, GRB.INF_OR_UNBD):
            return self.INFEASIBLE
        if s == GRB.UNBOUNDED:
            return self.UNBOUNDED
        return self.ABNORMAL

    # ---------- post-solve stats ----------

    def wall_time(self) -> float:
        return self._wall_ms

    def iterations(self) -> int:
        try:
            return int(self._model.IterCount)
        except Exception:
            return 0

    def nodes(self) -> int:
        try:
            return int(self._model.NodeCount)
        except Exception:
            return 0

    def NumConstraints(self) -> int:
        try:
            self._model.update()
            return self._model.NumConstrs
        except Exception:
            return 0

    class _Obj:
        def __init__(self, m: Any) -> None:
            self._m = m

        def Value(self) -> float | None:
            try:
                return float(self._m.ObjVal)
            except Exception:
                return None

    def Objective(self) -> "_Obj":
        return self._Obj(self._model)

    def __del__(self) -> None:
        try:
            self._model.dispose()
        except Exception:
            pass


def create_solver_with_fallback(preferred_backend: str = "GUROBI") -> Any:
    """Return a solver object compatible with the pywraplp.Solver interface.

    Tries gurobipy (Gurobi 13) first; falls back to OR-Tools SCIP → CBC.
    """
    threads = _gurobi_thread_param()

    # --- gurobipy path (preferred) ---
    if preferred_backend == "GUROBI":
        try:
            import gurobipy  # noqa: F401
            adapter = GurobiSolverAdapter(threads=threads)
            logging.info(
                "Solver initialized with GUROBI backend (gurobipy)"
                + (f" (Threads={threads})" if threads else "")
            )
            return adapter
        except Exception as exc:
            logging.warning("gurobipy unavailable (%s); falling back to OR-Tools", exc)

    # --- OR-Tools path (fallback) ---
    # SCIP holds the GIL during Solve() so Celery's SIGALRM-based task_time_limit
    # never fires. Enforce hard limits inside the solver itself so it cannot run
    # unbounded. Default: 90 min (under the 2h Celery hard kill) and 8 GB RAM.
    _scip_time_ms = int(os.environ.get("SCIP_TIME_LIMIT_SECONDS", "5400")) * 1000
    _scip_mem_mb = int(os.environ.get("SCIP_MEMORY_LIMIT_MB", "8192"))

    backends = ["SCIP", "CBC"] if preferred_backend == "GUROBI" else [preferred_backend, "SCIP", "CBC"]
    backends = list(dict.fromkeys(backends))
    for backend in backends:
        try:
            solver = pywraplp.Solver.CreateSolver(backend)
            if solver:
                solver.SetTimeLimit(_scip_time_ms)
                if backend == "SCIP":
                    solver.SetSolverSpecificParametersAsString(
                        f"limits/memory = {_scip_mem_mb}"
                    )
                logging.warning(
                    "Solver falling back to %s (OR-Tools) — time_limit=%ds mem_limit=%dMB",
                    backend, _scip_time_ms // 1000, _scip_mem_mb,
                )
                return solver
        except Exception as exc:
            logging.debug("Failed to initialize %s backend: %s", backend, exc)

    logging.error("Failed to initialize any solver backend")
    return None


class BaseSolver(ABC):
    """Abstract solver contract for sibling optimization models."""

    REQUIRED_BASE_KEYS = {
        "instance_id",
        "companies",
        "orders",
        "order_owner",
        "cooperation",
        "vehicle_limit",
        "edges_input",
        "edge_cost_input",
        "edge_trailer_cost_input",
        "raw_cost",
    }

    def validate_params(self, params: Dict[str, Any]) -> None:
        missing = sorted(k for k in self.REQUIRED_BASE_KEYS if k not in params)
        if missing:
            raise ValueError(f"Missing required solver params: {missing}")

    @staticmethod
    def normalize_order_owner(params: Dict[str, Any]) -> Dict[Any, Any]:
        order_owner = params.get("order_owner", {})
        orders = params.get("orders", [])
        if not orders:
            return order_owner

        sample = orders[0]
        if isinstance(sample, int):
            normalized = {}
            for k, v in order_owner.items():
                try:
                    normalized[int(k)] = v
                except (TypeError, ValueError):
                    normalized[k] = v
            return normalized
        return order_owner

    @abstractmethod
    def solve(self, params: Dict[str, Any]):
        raise NotImplementedError
