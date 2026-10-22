"""``Result``: what a planner solver returns from ``solve_tick(tick, params)``.

A result is ``(truck_id, order_id)`` PAIRS in commit order, plus two opaque note
channels (plan ``plan_contract_final.md`` §2.6, decision 2):

- ``solver_notes`` -- per-pair JSON notes keyed by a pair of the result. The framework
  never interprets them; the wire layer routes them (e.g. a market's ``pool_id`` /
  ``market_round``).
- ``tick_notes`` -- per-tick JSON notes, read only by the TOP node's run-stats
  aggregator.

Chains and tours are not a framework concept: a tour solver's wrapper turns its tours
into pairs. Replaces ``dispatch/plan.py``'s ``Plan`` (kept for legacy ``decide``
policies in ``compat/legacy_context.py``).

Leaf module: stdlib plus ``policies.framework.errors``; never imports
``apps.container_logistics.assignment.*``. Framework side of the boundary: solvers
build results, the framework checks (``invariants.check_result``) and commits them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from apps.container_logistics.policies.framework.errors import DispatchContractViolation

__all__ = ["Result"]


@dataclass(frozen=True)
class Result:
    """Committed pairs in order, plus opaque ``solver_notes`` (per pair) and ``tick_notes``.

    ``pairs`` is stored as a tuple of tuples, everything else as given, so
    ``invariants.check_result`` reports a malformed result instead of this class
    silently repairing it. The two note mappings are read-only copies.
    """

    pairs: tuple[tuple[str, str], ...] = ()
    solver_notes: Mapping[tuple[str, str], Mapping[str, Any]] = field(default_factory=dict)
    tick_notes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        pairs = tuple(tuple(p) if isinstance(p, (list, tuple)) else p for p in (self.pairs or ()))
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "solver_notes", MappingProxyType(dict(self.solver_notes or {})))
        object.__setattr__(self, "tick_notes", MappingProxyType(dict(self.tick_notes or {})))

    @classmethod
    def from_pairs(
        cls,
        doc_pairs: Iterable[tuple[dict, dict]],
        *,
        solver_notes: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
        tick_notes: Mapping[str, Any] | None = None,
    ) -> "Result":
        """A result from ``(truck_doc, order_doc)`` pairs, in pair order (ids are ``str(doc["_id"])``).

        Raises :class:`DispatchContractViolation` when a truck appears twice (the rule
        and the message of the old ``Plan.from_pairs``). A repeated ORDER is left for
        ``invariants.check_result`` to report.
        """
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for truck, order in doc_pairs:
            tid = str(truck.get("_id"))
            if tid in seen:
                raise DispatchContractViolation(f"truck {tid!r} appears twice in pairs")
            seen.add(tid)
            pairs.append((tid, str(order.get("_id"))))
        return cls(pairs=tuple(pairs), solver_notes=dict(solver_notes or {}), tick_notes=dict(tick_notes or {}))
