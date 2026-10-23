"""The planner contract, checked: ``check_result(result, tick) -> list[str]``.

The runner calls this on every tick's result before anything is committed, and
``Tick.call`` on every sub-tick's; a non-empty answer discards that result (message 18
for the whole tick, 19 for a sub-tick). Replaces ``check_plan`` (plan
``plan_contract_final.md`` §2.6). :func:`check_wire_notes` is the runner's extra check
of the WHOLE tick's ``solver_notes``, the only notes that reach the wire.

Framework side of the boundary. Pure: reads the result and the documents; draws no RNG;
imports nothing from ``assignment``.

What makes the check hold against a DISHONEST solver (review H2): the framework's two
callers pass the state it took when the tick was built -- ``permitted`` (the tick's own
predicate: at the top ownership-or-declared AND physical, with the verified-pair memo
of sub-ticks the framework itself checked), ``index`` (the tick's own documents by id)
and ``intact`` (whether a document is unmodified since then) -- never anything read
back from an attribute a solver could have rewritten. So at the top a cross-company
pair the top did not declare, or a physically infeasible pair, is a violation whatever
solver produced it, however it tampered with the tick. Called with the defaults (tests,
the battery), it reads ``tick.permitted`` / ``tick.id_index``, which are read-only
properties over that same state.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from .result import Result

if TYPE_CHECKING:
    from .tick import Tick

logger = logging.getLogger(__name__)

__all__ = ["check_result", "check_wire_notes"]


def check_result(result: Any, tick: "Tick", *, permitted: Any = None, index: Any = None,
                 intact: Callable[[Any], bool] | None = None) -> list[str]:
    """Every way ``result`` breaks the contract for ``tick``; ``[]`` when it is valid.

    Reports: a value that is not a :class:`Result`; a pair that is not two strings; a
    truck or order id not in scope; a truck or an order appearing twice; a paired
    document that ``intact`` says was modified during the solve; a pair that
    ``permitted`` rejects (default ``tick.permitted``; the runner passes the top tick's
    verified-pair-aware check); a ``solver_notes`` key that is not one of the result's
    pairs, or a value that is not a mapping with string keys. ``index`` defaults to
    ``tick.id_index`` (first document per id, shared with the commit). Pure.
    """
    if not isinstance(result, Result):
        return [f"solve_tick() returned {type(result).__name__}, expected Result"]
    allowed = tick.permitted if permitted is None else permitted
    trucks, orders = tick.id_index if index is None else index
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
        if tid in trucks and oid in orders:
            t, o = trucks[tid], orders[oid]
            if intact is not None and not (intact(t) and intact(o)):
                which = [f"{kind} {i!r}" for kind, i, doc in (("truck", tid, t), ("order", oid, o)) if not intact(doc)]
                problems.append(f"{' and '.join(which)} was modified during the solve (documents are read-only)")
            elif not allowed(t, o):
                problems.append(f"pair ({tid!r}, {oid!r}) is not allowed")
    for key, note in result.solver_notes.items():
        if not isinstance(key, tuple) or key not in valid_pairs:
            problems.append(f"solver_notes key {key!r} is not one of the result's pairs")
        elif not (isinstance(note, Mapping) and all(isinstance(k, str) for k in note)):
            problems.append(f"solver_notes for {key!r} is not a mapping with string keys")
    return problems


def check_wire_notes(result: Result, reserved: Iterable[str]) -> list[str]:
    """Problems with the WHOLE tick's ``solver_notes`` as they will reach the wire (review L3).

    A note may not use a ``reserved`` key (a field the framework writes on the wire
    itself, ``compat.wire.reserved_note_keys_for``), and every note value must be plain
    JSON (no object, no NaN or infinity): the wire would otherwise carry a
    ``default=str`` text that differs from run to run. Call after :func:`check_result`
    (it assumes every note is a mapping with string keys). Pure.
    """
    reserved = frozenset(reserved)
    problems: list[str] = []
    for key, note in result.solver_notes.items():
        if not isinstance(note, Mapping):
            continue
        forged = sorted(k for k in note if k in reserved)
        if forged:
            problems.append(f"solver_notes for {key!r} sets the framework wire field(s) "
                            f"{', '.join(map(repr, forged))}; a solver's notes may not")
        try:
            json.dumps(dict(note), allow_nan=False)
        except (TypeError, ValueError) as exc:
            problems.append(f"solver_notes for {key!r} is not plain JSON ({exc})")
    return problems
