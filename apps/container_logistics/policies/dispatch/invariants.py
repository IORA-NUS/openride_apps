"""The planner contract, checked: ``check_result(result, tick) -> list[str]``.

The runner calls this on every tick's result before anything is committed, and
``Tick.call`` on every sub-tick's; a non-empty answer discards that result (message 18
for the whole tick, 19 for a sub-tick). Replaces ``check_plan`` (plan
``plan_contract_final.md`` §2.6).

Framework side of the boundary. Pure: reads the result and the tick's documents
(through ``tick.id_index``) and ``tick.permitted``; draws no RNG; imports nothing from
``assignment``. At the top ``permitted`` is ownership-or-declared AND physical, so a
cross-company pair the top did not declare is a violation whatever solver produced it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from .result import Result

if TYPE_CHECKING:
    from .tick import Tick

logger = logging.getLogger(__name__)

__all__ = ["check_result"]


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
