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
from typing import TYPE_CHECKING, Any

from .plan import Plan

if TYPE_CHECKING:
    from .context import DispatchContext

logger = logging.getLogger(__name__)

__all__ = ["check_plan"]


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
