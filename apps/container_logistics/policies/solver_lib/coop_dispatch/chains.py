"""Chains (job sequences per start node) from a solved route map. OpenRide addition (plan N2 item 5).

Works on the ``routes`` map of a :class:`NetworkFlowSolver` result (every ``x`` variable
with its value) and on a :mod:`min_cost_flow` result (only the chosen arcs, value 1.0):
an arc is chosen when its value is > 0.5.

A start node of ``capacity`` c (default 1) may head up to c chains. ``chains_by_start``
lists every start node's chains in a deterministic order: by the position of each chain's
first job in ``job_order`` when given (e.g. jobs in release order), else by ``str`` of the
first job id. A caller that merged c identical trucks into one start node splits that list
across them, e.g. sorted by truck id. ``by_start`` keeps the capacity-1 view (one job list
per start node, ``[]`` when unused) and lists only capacity-1 start nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

START, END = "start", "end"


@dataclass
class Chains:
    by_start: dict[Any, list[Any]] = field(default_factory=dict)
    """capacity-1 start node id -> its jobs in order ([] when the start node is unused)."""
    depot: dict[Any, list[list[Any]]] = field(default_factory=dict)
    """company -> its depot-started chains (companies without start nodes)."""
    unplanned: list[Any] = field(default_factory=list)
    """jobs the solver deferred (``result['unplanned']``)."""
    orphaned: list[Any] = field(default_factory=list)
    """jobs served on a cycle no start reaches (impossible on a DAG arc set)."""
    company_of_start: dict[Any, Any] = field(default_factory=dict)
    chains_by_start: dict[Any, list[list[Any]]] = field(default_factory=dict)
    """every start node id -> its chains (at most its capacity), deterministic order."""


def _capacity(spec) -> int:
    cap = spec.get("capacity", 1)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError(f"capacity must be an int >= 1 (got {cap!r})")
    return cap


def extract_chains(result: Mapping[str, Any], start_nodes: Mapping[Any, Mapping[str, Any]] | None = None,
                   *, job_order: Sequence[Any] | None = None) -> Chains:
    routes = result.get("routes")
    if routes is None:
        raise ValueError(f"result carries no routes (status {result.get('status')!r}): nothing to extract")
    start_nodes = start_nodes or {}
    caps = {sid: _capacity(spec) for sid, spec in start_nodes.items()}
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
    out = Chains(unplanned=list(result.get("unplanned", [])),
                 company_of_start={sid: spec.get("company") for sid, spec in start_nodes.items()},
                 chains_by_start={sid: [] for sid in start_nodes})
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
            lst = out.chains_by_start[origin]
            if len(lst) >= caps[origin]:
                raise ValueError(f"start node {origin!r} heads more chains than its capacity {caps[origin]}")
            lst.append(chain)
    if job_order is not None:
        pos = {j: k for k, j in enumerate(job_order)}

        def key(ch):
            return (pos.get(ch[0], len(pos)), str(ch[0]))
    else:
        def key(ch):
            return (0, str(ch[0]))
    for sid, lst in out.chains_by_start.items():
        lst.sort(key=key)
        if caps[sid] == 1:
            out.by_start[sid] = lst[0] if lst else []
    out.orphaned = [i for i in served if i not in seen]
    return out
