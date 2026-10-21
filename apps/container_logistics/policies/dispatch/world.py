"""``DispatchWorld``: the read-only world facts of one assignment tick.

Single responsibility: the FRAMEWORK's half of a dispatch decision, as one value
(plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.2): who owns what, which
companies may serve whose orders (the active cooperation structure), the pair
feasibility rule, and the accounting cost. A dispatch policy reads it through
``ctx.world``; it never decides any of it.

:meth:`DispatchWorld.from_profile` builds the world EXACTLY as
``AssignmentApp.assign``'s partitioned setup did (round-2 S2), so the rule is the
same ``PartitionRule`` object the per-bucket path used. :func:`tick_seed` is the
per-tick seed formula of ``AssignmentApp._tick_seed``. :func:`validate_cooperation_world`
is the run-start consistency check of pools against adjacency (message 14).

Framework side of the boundary. NOT a leaf module: it imports
``assignment.constraints`` and ``assignment.pools`` at top, which is safe only while
``assignment/app.py`` imports the dispatch package locally (plan §2.7).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import pools as assign_pools
from apps.container_logistics.policies.framework.errors import PolicyError

from .allowed import CompanyRule, PartitionRule

logger = logging.getLogger(__name__)

__all__ = ["DispatchWorld", "tick_seed", "validate_cooperation_world"]

# The rule types whose ``allowed`` is ``physical AND <who may serve whom>``, so a pass
# under one proves ``pair_allowed_ignoring_haulier`` with its ``max_travel_time_pickup``
# (allowed.py: both branches of both rules end in it; ``pair_allowed`` is
# ``haulier_matches AND pair_allowed_ignoring_haulier``). Exact types: a subclass may
# redefine ``allowed``.
_PHYSICAL_PROVING_RULES = (PartitionRule, CompanyRule)


def tick_seed(run_id: str, time_step: int) -> int:
    """Stable 64-bit per-tick seed: ``blake2b(f"{run_id}|{time_step}")``, big-endian.

    The formula of ``AssignmentApp._tick_seed`` (identical inputs give identical
    awards, I-P5). It makes one TICK reproducible, not a whole run: agent scheduling
    is async.
    """
    digest = hashlib.blake2b(f"{run_id}|{time_step}".encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def _component_of(structure: Any, adjacency: Mapping[str, Any]) -> dict[str, int]:
    """``{haulier: component index}`` for members of multi-company components (S2, verbatim).

    Empty without adjacency or without a dict structure; singleton components are
    left out, so such a haulier partitions on its own id.
    """
    component_of: dict[str, int] = {}
    if adjacency and isinstance(structure, dict):
        for idx, members in enumerate(structure.get("components") or []):
            if isinstance(members, (list, tuple)) and len(members) > 1:
                for hid in members:
                    component_of[str(hid)] = idx
    return component_of


@dataclass(frozen=True, eq=False)
class DispatchWorld:
    """Ownership, cooperation, feasibility and accounting for one tick (read-only).

    ``structure`` is the ACTIVE cooperation structure (post-override) or ``None``;
    ``rule`` is ``PartitionRule(max_travel_time_pickup, share_adjacency(structure))``;
    ``component_of`` maps a member of a multi-company component to its index;
    ``accounting_params`` are the dials of the framework's accounting cost
    (``AssignmentApp._solver_params``).
    """

    structure: Mapping[str, Any] | None
    rule: PartitionRule
    component_of: Mapping[str, int]
    max_travel_time_pickup: Any
    accounting_params: Mapping[str, Any]

    @classmethod
    def from_profile(
        cls, prof: Mapping[str, Any], *, accounting_params: Mapping[str, Any] | None
    ) -> "DispatchWorld":
        """The world of assignment profile ``prof`` (S2 of the round-2 plan, verbatim)."""
        structure = assign_constraints.active_cooperation_structure(prof)
        adjacency = assign_constraints.share_adjacency(structure)
        mtt = prof.get("max_travel_time_pickup")
        return cls(
            structure=structure,
            rule=PartitionRule(max_travel_time_pickup=mtt, adjacency=adjacency),
            component_of=MappingProxyType(_component_of(structure, adjacency)),
            max_travel_time_pickup=mtt,
            accounting_params=MappingProxyType(dict(accounting_params or {})),
        )

    def allowed(self, t: dict[str, Any], o: dict[str, Any]) -> bool:
        """Pair feasibility: own-haulier strict, cross pairs only along a structure edge."""
        return self.rule.allowed(t, o)

    def structure_allows(self, t: dict[str, Any], o: dict[str, Any]) -> bool:
        """The who-may-serve-whom half of :meth:`allowed`: own haulier, or a structure edge.

        ``allowed(t, o) == structure_allows(t, o) and physical(t, o)`` exactly, where
        physical is ``constraints.pair_allowed_ignoring_haulier`` (``PartitionRule.allowed``
        rewritten; a test pins the identity). Fail-closed on a missing haulier.
        """
        if assign_constraints.haulier_matches(t, o):
            return True
        adjacency = self.rule.adjacency
        return bool(adjacency) and assign_constraints.share_eligible(t, o, adjacency)

    def proves_physical(self, rule: Any) -> bool:
        """Whether a pass under sub-scope ``rule`` proves this world's physical half for a pair.

        True only for an exact ``PartitionRule``/``CompanyRule`` with this world's
        ``max_travel_time_pickup`` (same type and value).
        """
        if type(rule) not in _PHYSICAL_PROVING_RULES:
            return False
        mine, theirs = self.max_travel_time_pickup, rule.max_travel_time_pickup
        return type(mine) is type(theirs) and mine == theirs

    def tick_allowed(self, verified: set[tuple[int, int]]) -> Any:
        """The tick scope's feasibility: :meth:`allowed`, minus physical work already proved.

        ``verified`` holds ``(id(truck), id(order))`` of heads a sub-scope check passed
        under a rule that :meth:`proves_physical` (``compose.decide_in_scope`` records
        them; identity keys, so a duplicate id with other content is never a hit). For
        such a pair only :meth:`structure_allows` is re-checked; every other pair gets
        the full :meth:`allowed`. The framework's tick check therefore stays
        authoritative for every solver (a central or custom solver's heads are never
        in ``verified``) while a decomposition's heads skip the duplicate physical
        check (review C3 #2). Pure: the verdict equals :meth:`allowed` for every pair,
        provided the documents are not mutated between the sub-scope check and the tick
        check. That is the read-only contract on ``DispatchPolicy``; a policy that moves
        a truck for a sub-solve and restores it gets the sub-scope's verdict.
        """
        world_allowed = self.allowed
        structure_allows = self.structure_allows

        def allowed(t: dict[str, Any], o: dict[str, Any]) -> bool:
            if verified and (id(t), id(o)) in verified:
                return structure_allows(t, o)
            return world_allowed(t, o)

        return allowed

    def own_allowed(self, t: dict[str, Any], o: dict[str, Any]) -> bool:
        """The strict own-haulier rule (``constraints.pair_allowed``): the owner reserve's."""
        return assign_constraints.pair_allowed(t, o, max_travel_time_pickup=self.max_travel_time_pickup)

    def accounting_cost(self, t: dict[str, Any], o: dict[str, Any]) -> float:
        """The framework's accounting objective (deadhead km, dual-cycle aware)."""
        return assign_constraints.assignment_cost(t, o, self.accounting_params)

    def partners(self, haulier_id: str) -> frozenset[str]:
        """The companies ``haulier_id`` has a structure edge to (empty without one)."""
        return self.rule.adjacency.get(str(haulier_id), frozenset())

    def may_serve(self, server: str, owner: str) -> bool:
        """Whether ``server``'s trucks may serve ``owner``'s orders (own, or an edge partner)."""
        return self.rule.serves(str(server), str(owner))

    def cooperation_pairs(self, hauliers: Iterable[str]) -> tuple[tuple[str, str], ...]:
        """Every ``(owner, server)`` pair among ``hauliers`` that may cooperate, sorted.

        Includes the self-loop ``(h, h)`` of every haulier: a company always serves its
        own orders. This is the cooperation set a central solver (NetworkFlow) needs.
        """
        ids = sorted({str(h) for h in hauliers})
        return tuple((owner, server) for owner in ids for server in ids if self.may_serve(server, owner))


def validate_cooperation_world(profile: Mapping[str, Any]) -> None:
    """Raise ``PolicyError`` (message 14) when a pool pairs companies the adjacency does not.

    Checks the ACTIVE structure of ``profile`` (the one the run executes, after any
    cooperation override): every two members of every pool (``pools_from_structure``:
    authored pools, else one per edge) must be adjacent in both directions. A compiled
    structure satisfies this by construction (pools are canonical, edges and adjacency
    derive from them); only a hand edit can break it. Run start only.
    """
    structure = assign_constraints.active_cooperation_structure(dict(profile or {}))
    if not isinstance(structure, dict):
        return
    adjacency = assign_constraints.share_adjacency(structure)
    for pool in assign_pools.pools_from_structure(structure):
        members = sorted(pool.members)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if b not in adjacency.get(a, ()) or a not in adjacency.get(b, ()):
                    raise PolicyError(
                        f"cooperation structure {structure.get('id')!r}: pool {pool.id!r} lets "
                        f"{a} and {b} share orders but the structure's adjacency does not pair "
                        "them. Pools and adjacency must agree; recompile the scenario (pools "
                        "are canonical)."
                    )
