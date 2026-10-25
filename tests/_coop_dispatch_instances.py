"""Small deterministic NetworkFlowSolver instances (no ortools needed to build them).

Shared by ``tests/data/capture_coop_dispatch_upstream_golden.py`` (which records the
PRISTINE upstream solver's output on them) and ``tests/test_coop_dispatch_vendor.py``
(which proves the vendored, option-extended solver reproduces that output exactly when
every option is off). Changing anything here invalidates the golden file: re-capture it
from the pristine upstream, never from the vendored copy.
"""

from __future__ import annotations

import random
from typing import Any


def _instance(seed: int, *, n: int, companies: list[str], coop: str, vl: dict[str, int],
              use_trailer_cost: bool | None, released_frac: float, int_ids: bool,
              edge_p: float, raw_edges: bool, dup_edges: int = 0) -> dict[str, Any]:
    rng = random.Random(seed)
    ids: list[Any] = list(range(1, n + 1)) if int_ids else [f"o{k}" for k in range(n)]
    owner = {o: companies[rng.randrange(len(companies))] for o in ids}
    if coop == "self":
        cooperation = {(c, c) for c in companies}
    elif coop == "full":
        cooperation = {(a, b) for a in companies for b in companies}
    else:  # "chain": a<->b, b<->c, but not a<->c (non-transitive)
        cooperation = {(c, c) for c in companies}
        for a, b in zip(companies, companies[1:]):
            cooperation |= {(a, b), (b, a)}
    edges, cost, tcost = [], {}, {}
    for a in range(n):
        for b in range(a + 1, n):
            if rng.random() < edge_p:
                e = (ids[a], ids[b])
                edges.append(e)
                cost[e] = float(rng.randrange(200, 20000))
                tcost[e] = float(rng.choice([0, 0, 0, rng.randrange(500, 8000)]))
    # Duplicated edges: upstream's scans count the shared x variable once per copy.
    edges += [edges[k] for k in range(min(dup_edges, len(edges)))]
    p: dict[str, Any] = {
        "instance_id": f"inst{seed}",
        "companies": companies,
        "orders": ids,
        # JSON-style string keys for int ids exercise normalize_order_owner.
        "order_owner": ({str(k): v for k, v in owner.items()} if int_ids else owner),
        "cooperation": cooperation,
        "vehicle_limit": vl,
        "edges_input": edges,
        "edge_cost_input": cost,
        "edge_trailer_cost_input": tcost,
        "raw_cost": float(seed * 1000),
        "released_job_mapping": {o: True for o in ids if rng.random() < released_frac},
    }
    if use_trailer_cost is not None:
        p["use_trailer_cost"] = use_trailer_cost
    if raw_edges and edges:
        p["raw_edge_list"] = edges[: max(1, len(edges) // 3)]
    return p


def golden_instances() -> list[dict[str, Any]]:
    """The fixed instance list. Each is a NetworkFlowSolver ``params`` dict."""
    return [
        _instance(1, n=5, companies=["A", "B"], coop="self", vl={"A": 3, "B": 3},
                  use_trailer_cost=None, released_frac=0.3, int_ids=False, edge_p=0.7, raw_edges=True),
        _instance(2, n=7, companies=["A", "B"], coop="full", vl={"A": 2, "B": 2},
                  use_trailer_cost=False, released_frac=0.0, int_ids=False, edge_p=0.6, raw_edges=False),
        _instance(3, n=8, companies=["A", "B", "C"], coop="chain", vl={"A": 2, "B": 3, "C": 2},
                  use_trailer_cost=True, released_frac=0.4, int_ids=False, edge_p=0.5, raw_edges=True),
        _instance(4, n=6, companies=["A", "B", "C"], coop="full", vl={"A": 1, "B": 1, "C": 2},
                  use_trailer_cost=False, released_frac=0.0, int_ids=True, edge_p=0.8, raw_edges=True),
        _instance(5, n=9, companies=["A", "B"], coop="chain", vl={"A": 4, "B": 4},
                  use_trailer_cost=True, released_frac=0.5, int_ids=True, edge_p=0.45, raw_edges=False),
        # Vehicle limit too small for a sparse graph: INFEASIBLE, the "No optimal solution" path.
        _instance(6, n=6, companies=["A"], coop="self", vl={"A": 1},
                  use_trailer_cost=False, released_frac=0.0, int_ids=False, edge_p=0.2, raw_edges=False),
        _instance(7, n=8, companies=["A", "B", "C"], coop="self", vl={"A": 3, "B": 3, "C": 3},
                  use_trailer_cost=None, released_frac=0.2, int_ids=False, edge_p=0.55, raw_edges=True),
        # Duplicate entries in edges_input (the edge index must keep them, as the scans do).
        _instance(8, n=6, companies=["A", "B"], coop="full", vl={"A": 3, "B": 3},
                  use_trailer_cost=False, released_frac=0.0, int_ids=False, edge_p=0.7, raw_edges=False,
                  dup_edges=2),
    ]


# Keys in the solver's stats that measure wall time, not the model or its solution.
TIMING_KEYS = ("solve_time_s", "wall_time_ms")


def _key(k: Any) -> Any:
    return list(k) if isinstance(k, tuple) else k


def canonical(obj: Any) -> Any:
    """JSON-safe, ORDER-PRESERVING form of a solver result/stats (tuple-keyed dicts become
    ``[[key...], value]`` lists in insertion order; sets become sorted lists)."""
    if isinstance(obj, dict):
        if any(isinstance(k, tuple) for k in obj):
            return [[_key(k), canonical(v)] for k, v in obj.items()]
        return {str(k): canonical(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(_key(x) for x in obj)
    if isinstance(obj, (list, tuple)):
        return [canonical(x) for x in obj]
    return obj


def strip_timings(stats: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in stats.items() if k not in TIMING_KEYS}
