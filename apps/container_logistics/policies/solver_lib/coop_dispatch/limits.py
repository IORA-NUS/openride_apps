"""Resource guards for a solver CHILD process (plan addendum minor 8). OpenRide addition.

N0 measured one SCIP child at 11.6 GB maxrss on a box that also runs 64 celery workers.

- :func:`memory_cap_preexec` returns a ``preexec_fn`` for ``subprocess.Popen`` that caps
  the child's address space (``RLIMIT_AS``, default 4 GiB). An allocation past the cap
  raises ``MemoryError`` in the child (or kills a native solver); the parent survives.
  Call it in the child only -- never in a celery worker, whose own limit it would lower.
- :func:`check_mip_arc_budget` counts the MIP's ``x`` variables BEFORE building the model
  (the same rule ``NetworkFlowSolver`` uses) and refuses an instance above the budget.
"""

from __future__ import annotations

import resource
from typing import Any, Callable, Dict

DEFAULT_MEMORY_CAP_BYTES = 4 * 1024 ** 3
DEFAULT_MIP_ARC_BUDGET = 500_000


class ArcBudgetExceeded(ValueError):
    pass


def arrays_to_lists(params: Dict[str, Any]) -> Dict[str, Any]:
    """``params`` with ``edges_array`` / ``start_edges_array`` (index arrays, see
    ``min_cost_flow.solve_min_cost_flow``) turned into the MIP's ``edges_input`` /
    ``edge_cost_input`` / ``edge_trailer_cost_input`` and ``start_edges`` /
    ``start_edge_cost`` lists, in array order. Without arrays, ``params`` is returned as is.
    Kept import-free (this module is loaded standalone where OR-Tools is absent)."""
    if "edges_array" not in params and "start_edges_array" not in params:
        return params
    out = dict(params)
    orders = list(params["orders"])
    if "edges_array" in out:
        ea = out.pop("edges_array")
        edges = [(orders[a], orders[b]) for a, b in zip(list(ea["src"]), list(ea["dst"]))]
        out["edges_input"] = edges
        out["edge_cost_input"] = dict(zip(edges, (float(c) for c in ea["cost"])))
        out["edge_trailer_cost_input"] = {e: 0 for e in edges}
    if "start_edges_array" in out:
        sa = out.pop("start_edges_array")
        sids = list(params.get("start_nodes") or {})
        sedges = [(sids[a], orders[b]) for a, b in zip(list(sa["src"]), list(sa["dst"]))]
        out["start_edges"] = sedges
        out["start_edge_cost"] = dict(zip(sedges, (float(c) for c in sa["cost"])))
    return out


def memory_cap_preexec(limit_bytes: int = DEFAULT_MEMORY_CAP_BYTES) -> Callable[[], None]:
    if not isinstance(limit_bytes, int) or limit_bytes <= 0:
        raise ValueError(f"limit_bytes must be a positive int (got {limit_bytes!r})")

    def _apply() -> None:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))

    return _apply


def mip_arc_count(params: Dict[str, Any]) -> int:
    """Number of ``x`` variables ``NetworkFlowSolver`` would create for ``params``."""
    params = arrays_to_lists(params)
    companies = list(params["companies"])
    owner, coop = params["order_owner"], set(params["cooperation"])
    eligible = params.get("eligible")
    eligible = None if eligible is None else set(eligible)
    sn = params.get("start_nodes") or {}
    sn_cos = {s["company"] for s in sn.values()}
    serv = {o: {c for c in companies if (owner[o], c) in coop and (eligible is None or (o, c) in eligible)}
            for o in params["orders"]}
    n = sum(len(serv[i] & serv[j]) for (i, j) in dict.fromkeys(params["edges_input"])
            if i in serv and j in serv)
    for o, cs in serv.items():
        n += len(cs)                    # (o, end, c)
        n += len(cs - sn_cos)           # (start, o, c)
    n += sum(1 for (s, j) in params.get("start_edges", []) if s in sn and sn[s]["company"] in serv.get(j, ()))
    return n


def check_mip_arc_budget(params: Dict[str, Any], budget: int = DEFAULT_MIP_ARC_BUDGET) -> int:
    n = mip_arc_count(params)
    if n > budget:
        raise ArcBudgetExceeded(
            f"MIP instance {params.get('instance_id')!r} has {n} arc variables, above the budget "
            f"{budget}: solve it with the min-cost flow, raise k caps, or split it")
    return n
