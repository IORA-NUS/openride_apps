"""Chains (one job sequence per start node) from a solved route map. OpenRide addition (plan N2 item 5).

Works on the ``routes`` map of a :class:`NetworkFlowSolver` result (every ``x`` variable
with its value) and on a :mod:`min_cost_flow` result (only the chosen arcs, value 1.0):
an arc is chosen when its value is > 0.5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

START, END = "start", "end"


@dataclass
class Chains:
    by_start: dict[Any, list[Any]] = field(default_factory=dict)
    """start node id -> its jobs in order ([] when the start node is unused)."""
    depot: dict[Any, list[list[Any]]] = field(default_factory=dict)
    """company -> its depot-started chains (companies without start nodes)."""
    unplanned: list[Any] = field(default_factory=list)
    """jobs the solver deferred (``result['unplanned']``)."""
    orphaned: list[Any] = field(default_factory=list)
    """jobs served on a cycle no start reaches (impossible on a DAG arc set)."""
    company_of_start: dict[Any, Any] = field(default_factory=dict)


def extract_chains(result: Mapping[str, Any], start_nodes: Mapping[Any, Mapping[str, Any]] | None = None) -> Chains:
    routes = result.get("routes")
    if routes is None:
        raise ValueError(f"result carries no routes (status {result.get('status')!r}): nothing to extract")
    start_nodes = start_nodes or {}
    succ: dict[Any, Any] = {}
    heads: list[tuple[Any, Any, Any]] = []   # (origin, first job, company), in routes order
    served: list[Any] = []
    for (i, j, c), v in routes.items():
        if v <= 0.5:
            continue
        if i == START or i in start_nodes:
            if j == END:
                continue
            heads.append((i, j, c))
        elif j != END:
            if i in succ:
                raise ValueError(f"job {i!r} has two chosen successors ({succ[i]!r}, {j!r})")
            succ[i] = j
            served.append(i)
        else:
            served.append(i)
    out = Chains(by_start={sid: [] for sid in start_nodes},
                 unplanned=list(result.get("unplanned", [])),
                 company_of_start={sid: spec.get("company") for sid, spec in start_nodes.items()})
    seen: set = set()
    for origin, first, c in heads:
        chain = [first]
        while chain[-1] in succ:
            nxt = succ[chain[-1]]
            if nxt in seen or nxt in chain:
                raise ValueError(f"job {nxt!r} is reached twice")
            chain.append(nxt)
        if first in seen:
            raise ValueError(f"job {first!r} heads two chains")
        seen.update(chain)
        if origin == START:
            out.depot.setdefault(c, []).append(chain)
        else:
            if out.by_start[origin]:
                raise ValueError(f"start node {origin!r} heads two chains")
            out.by_start[origin] = chain
    out.orphaned = [i for i in served if i not in seen]
    return out
