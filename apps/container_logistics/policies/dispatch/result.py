"""``Result``: what a planner solver returns from ``solve_tick(tick, params)``.

A result is ``(truck_id, order_id)`` PAIRS in commit order, plus two opaque note
channels (plan ``plan_contract_final.md`` §2.6, decision 2):

- ``solver_notes`` -- per-pair JSON notes keyed by a pair of the result. The framework
  never interprets them; the wire layer routes them (e.g. a market's ``pool_id`` /
  ``market_round``).
- ``tick_notes`` -- per-tick JSON notes, read only by the TOP node's run-stats
  aggregator.
- ``prefetch`` -- order ids (exact ``str``) the TOP asks the framework to read for its
  NEXT tick beyond the regular unassigned-order fetch (F3). The caller reads them by id
  into ``tick.lookup`` (any state) and adds the unassigned, unclaimed ones to the next
  tick's orders, so they are still ``permitted``-checked like every other order. At most
  :data:`PREFETCH_MAX` ids (``invariants.normalise_result``, message 18 problem R7). A
  sub-tick's ``prefetch`` reaches nobody unless its caller copies it.

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

__all__ = ["PREFETCH_MAX", "Result"]

#: The most order ids one result may ``prefetch`` (2 per truck at 2000 trucks: a head and a
#: dispatched-not-done job each). More is a contract violation (R7, message 18).
PREFETCH_MAX = 4000


@dataclass(frozen=True)
class Result:
    """Committed pairs in order, plus opaque ``solver_notes`` (per pair), ``tick_notes`` and ``prefetch``.

    ``pairs`` is stored as a tuple of tuples, everything else as given, so
    ``invariants.check_result`` reports a malformed result instead of this class
    silently repairing it. The two note mappings are read-only copies; ``prefetch`` is a
    tuple when given as a list / tuple / set (a bare string or any other value is kept as
    given, for ``invariants.normalise_result`` to report).
    """

    pairs: tuple[tuple[str, str], ...] = ()
    solver_notes: Mapping[tuple[str, str], Mapping[str, Any]] = field(default_factory=dict)
    tick_notes: Mapping[str, Any] = field(default_factory=dict)
    prefetch: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        pairs = tuple(tuple(p) if isinstance(p, (list, tuple)) else p for p in (self.pairs or ()))
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "solver_notes", MappingProxyType(dict(self.solver_notes or {})))
        object.__setattr__(self, "tick_notes", MappingProxyType(dict(self.tick_notes or {})))
        prefetch = self.prefetch
        if prefetch is None:
            prefetch = ()
        elif isinstance(prefetch, (list, tuple)):
            prefetch = tuple(prefetch)
        elif isinstance(prefetch, (set, frozenset)):
            prefetch = tuple(sorted(prefetch, key=repr))  # a set has no order of its own (PYTHONHASHSEED)
        object.__setattr__(self, "prefetch", prefetch)

    @classmethod
    def from_pairs(
        cls,
        doc_pairs: Iterable[tuple[dict, dict]],
        *,
        solver_notes: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
        tick_notes: Mapping[str, Any] | None = None,
        prefetch: Iterable[str] = (),
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
        return cls(pairs=tuple(pairs), solver_notes=dict(solver_notes or {}), tick_notes=dict(tick_notes or {}),
                   prefetch=(prefetch if prefetch is None or isinstance(prefetch, (str, bytes, list, tuple, set, frozenset))
                             else tuple(prefetch)))
