"""The dispatch contract, checked: ``check_plan(plan, ctx) -> list[str]``.

The runner calls this on every plan before anything is committed; a non-empty
result discards the plan for that scope and tick (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P1.7). Time-feasibility of chain tails
arrives with plan timing in P4.

Framework side of the boundary. Pure: reads the plan and the context's raw documents
(through ``ctx.id_index``) and ``allowed_raw``; draws no RNG; imports nothing from
``assignment``. On the tick scope ``allowed_raw`` is ``world.allowed``, so a
cross-company head the cooperation structure does not permit is a violation whatever
solver produced it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from .plan import Plan
from .result import Result

if TYPE_CHECKING:
    from .context import DispatchContext
    from .tick import Tick

logger = logging.getLogger(__name__)

__all__ = ["check_plan", "check_result"]


def check_plan(plan: Any, ctx: "DispatchContext") -> list[str]:
    """Every way ``plan`` breaks the contract for ``ctx``; ``[]`` when it is valid.

    Reports: a result that is not a :class:`Plan`; a chain key that is not a truck
    in scope; an empty (or non-tuple) chain; an order id appearing twice anywhere;
    an order id not in scope; a head pair that ``ctx.allowed_raw`` rejects.
    """
    if not isinstance(plan, Plan):
        return [f"decide() returned {type(plan).__name__}, expected Plan"]
    trucks, orders = ctx.id_index  # first document per id, shared with the commit
    problems: list[str] = []
    seen_orders: set[str] = set()
    for tid, chain in plan.chains.items():
        if tid not in trucks:
            problems.append(f"truck {tid!r} is not in scope {ctx.scope.key}")
        if not isinstance(chain, tuple) or not chain:
            problems.append(f"chain for truck {tid!r} is empty or not a tuple: {chain!r}")
            continue
        for oid in chain:
            if oid in seen_orders:
                problems.append(f"order {oid!r} appears twice in the plan")
            seen_orders.add(oid)
            if oid not in orders:
                problems.append(f"order {oid!r} is not in scope {ctx.scope.key}")
        head = chain[0]
        if tid in trucks and head in orders and not ctx.allowed_raw(trucks[tid], orders[head]):
            problems.append(f"head pair ({tid!r}, {head!r}) is not allowed")
    return problems


def check_result(result: Any, tick: "Tick", *, permitted: Any = None) -> list[str]:
    """Every way ``result`` breaks the contract for ``tick``; ``[]`` when it is valid.

    Reports: a value that is not a :class:`Result`; a pair that is not two strings; a
    truck or order id not in scope; a truck or an order appearing twice; a pair that
    ``permitted`` rejects (default ``tick.permitted``; the runner passes the top
    tick's verified-pair-aware check); a ``solver_notes`` key that is not one of the
    result's pairs, or a value that is not a mapping with string keys. Pure.
    """
    if not isinstance(result, Result):
        return [f"solve_tick() returned {type(result).__name__}, expected Result"]
    allowed = tick.permitted if permitted is None else permitted
    trucks, orders = tick.id_index  # first document per id, shared with the commit
    scope = tick.scope
    problems: list[str] = []
    seen_trucks: set[str] = set()
    seen_orders: set[str] = set()
    valid_pairs: set[tuple[str, str]] = set()
    for pair in result.pairs:
        if not (isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            problems.append(f"pair {pair!r} is not a (truck_id, order_id) pair of strings")
            continue
        tid, oid = pair
        valid_pairs.add(pair)
        if tid in seen_trucks:
            problems.append(f"truck {tid!r} appears twice in the result")
        seen_trucks.add(tid)
        if oid in seen_orders:
            problems.append(f"order {oid!r} appears twice in the result")
        seen_orders.add(oid)
        if tid not in trucks:
            problems.append(f"truck {tid!r} is not in scope {scope}")
        if oid not in orders:
            problems.append(f"order {oid!r} is not in scope {scope}")
        if tid in trucks and oid in orders and not allowed(trucks[tid], orders[oid]):
            problems.append(f"pair ({tid!r}, {oid!r}) is not allowed")
    for key, note in result.solver_notes.items():
        if not isinstance(key, tuple) or key not in valid_pairs:
            problems.append(f"solver_notes key {key!r} is not one of the result's pairs")
        elif not (isinstance(note, Mapping) and all(isinstance(k, str) for k in note)):
            problems.append(f"solver_notes for {key!r} is not a mapping with string keys")
    return problems
