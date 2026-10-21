"""``Plan``: the value a dispatch policy returns from ``decide(ctx)``.

A plan maps a truck id to the ORDERED order ids it should serve. Insertion order
of ``chains`` is commit order; an order deliberately deferred is simply absent.
Under ``online`` timing the runner commits only each chain's head (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P1.1, P1.8); tails become executable in P4.

Leaf module (plan §2.7): stdlib plus ``policies.framework.errors`` only. It never
imports ``apps.container_logistics.assignment.*``. Framework side of the
framework/policy boundary: policies build plans, the runner validates and commits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from apps.container_logistics.policies.framework.errors import DispatchContractViolation

logger = logging.getLogger(__name__)

__all__ = ["Plan"]


@dataclass(frozen=True)
class Plan:
    """Truck id -> ordered order ids, plus free-form ``notes`` (logged at DEBUG).

    ``chains`` is stored as a read-only mapping so a plan cannot be mutated after
    the invariants checked it. Keys and ids are strings (``str(doc["_id"])``).
    """

    chains: Mapping[str, tuple[str, ...]]
    notes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Lists become tuples; anything else is kept as-is so check_plan reports it
        # instead of a str being silently exploded into characters.
        frozen = {
            str(k): tuple(v) if isinstance(v, (list, tuple)) else v
            for k, v in dict(self.chains).items()
        }
        object.__setattr__(self, "chains", MappingProxyType(frozen))
        object.__setattr__(self, "notes", MappingProxyType(dict(self.notes or {})))

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[dict, dict]],
        notes: Mapping[str, Any] | None = None,
    ) -> "Plan":
        """Build a heads-only plan from ``(truck_doc, order_doc)`` pairs, in pair order.

        Raises :class:`DispatchContractViolation` when a truck appears twice: a pair
        list cannot express a chain, so a repeated truck is a double assignment.
        A repeated ORDER is left for :func:`invariants.check_plan` to report.
        """
        chains: dict[str, tuple[str, ...]] = {}
        for truck, order in pairs:
            tid = str(truck.get("_id"))
            if tid in chains:
                raise DispatchContractViolation(f"truck {tid!r} appears twice in pairs")
            chains[tid] = (str(order.get("_id")),)
        return cls(chains=chains, notes=dict(notes or {}))

    def heads(self) -> list[tuple[str, str]]:
        """``(truck_id, first_order_id)`` per non-empty chain, in commit order."""
        return [(tid, chain[0]) for tid, chain in self.chains.items() if chain]
