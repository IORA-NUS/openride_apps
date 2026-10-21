"""Feasibility predicates as data: ``PartitionRule`` and ``CompanyRule``.

Each rule is the frozen, serialisable form of a closure that used to be built inline
per tick, moved verbatim (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P1.4):

- :class:`PartitionRule` -- ``AssignmentApp.assign``'s ``_pair_allowed`` and
  ``_serves`` (the partitioned topology).
- :class:`CompanyRule` -- ``pooled_planner._pair_allowed_for(carrier)`` (the pooled
  topology). It calls the ``constraints`` functions directly, which is exactly what
  ``ctx.pair_allowed_own`` / ``ctx.pair_allowed_pooled`` wrap with the same
  ``max_travel_time_pickup``.

Framework side of the boundary: feasibility is never a policy's decision. NOT a leaf
module: it imports ``assignment.constraints`` at top, which is safe only while
``assignment/app.py`` imports this package lazily (plan §2.7; the import-cycle test
pins it). ``constraints`` is called through the module so tests may patch it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints

logger = logging.getLogger(__name__)

__all__ = ["PartitionRule", "CompanyRule"]


@dataclass(frozen=True)
class PartitionRule:
    """Partitioned-topology feasibility: own-haulier strict, cross pairs via an edge.

    ``adjacency`` is ``constraints.share_adjacency(structure)`` (``{haulier: set}``);
    an empty mapping means own-haulier-only, today's no-cooperation behaviour.
    """

    max_travel_time_pickup: Any
    adjacency: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        frozen = {str(k): frozenset(v) for k, v in dict(self.adjacency or {}).items()}
        object.__setattr__(self, "adjacency", MappingProxyType(frozen))

    def allowed(self, t: dict[str, Any], o: dict[str, Any]) -> bool:
        """``app.py`` ``_pair_allowed``, verbatim."""
        # Own-haulier pairs keep the strict rule; cross pairs require a
        # structure edge (share_eligible) + physical feasibility. Own and
        # partner trucks compete ON EQUAL FOOTING — the deadhead cost decides,
        # never fleet availability.
        if assign_constraints.haulier_matches(t, o):
            return assign_constraints.pair_allowed(
                t, o, max_travel_time_pickup=self.max_travel_time_pickup
            )
        if self.adjacency and assign_constraints.share_eligible(t, o, self.adjacency):
            return assign_constraints.pair_allowed_ignoring_haulier(
                t, o, max_travel_time_pickup=self.max_travel_time_pickup
            )
        return False

    def serves(self, truck_haulier: str, order_haulier: str) -> bool:
        """``app.py`` ``_serves``, verbatim: own haulier, or an edge partner."""
        return truck_haulier == order_haulier or (
            order_haulier in self.adjacency.get(truck_haulier, ())
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for capture; adjacency as sorted lists under sorted keys."""
        return {
            "kind": "partition",
            "max_travel_time_pickup": self.max_travel_time_pickup,
            "adjacency": {k: sorted(self.adjacency[k]) for k in sorted(self.adjacency)},
        }


@dataclass(frozen=True)
class CompanyRule:
    """Pooled-topology feasibility for one carrier company.

    Own orders keep the STRICT rule (``constraints.pair_allowed``); pooled foreign
    orders drop the haulier-equality test because pool visibility already proved
    eligibility by construction (``pair_allowed_ignoring_haulier``).
    """

    haulier_id: str
    max_travel_time_pickup: Any

    def allowed(self, t: dict[str, Any], o: dict[str, Any]) -> bool:
        """``pooled_planner._pair_allowed_for(haulier_id)``, verbatim."""
        if assign_constraints.haulier_of(o) == self.haulier_id:
            return assign_constraints.pair_allowed(
                t, o, max_travel_time_pickup=self.max_travel_time_pickup
            )
        return assign_constraints.pair_allowed_ignoring_haulier(
            t, o, max_travel_time_pickup=self.max_travel_time_pickup
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for capture."""
        return {
            "kind": "company",
            "haulier_id": self.haulier_id,
            "max_travel_time_pickup": self.max_travel_time_pickup,
        }
