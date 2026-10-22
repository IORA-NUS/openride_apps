"""The scenario's active cooperation arm, as a solver reads it from the run world.

Single responsibility: interpret the scenario's cooperation block (world data, the
Run step's structure dropdown) for a SOLVER (plan ``plan_contract_final.md`` §5.1
rows 9-10). The framework never reads it: a solver's ``bind`` resolves the active arm
here and DECLARES the cross-company pairs it will use (``Declaration.cross_company``),
and the framework enforces exactly that declaration.

- :class:`Arm` / :func:`resolve_arm` -- the active structure, its adjacency and its
  multi-company components; verbatim from ``constraints.active_cooperation_structure``
  + ``share_adjacency`` and the old ``DispatchWorld``'s component map.
- :func:`directed_pairs` -- the arm's ``(owner, carrier)`` pairs (a carrier's trucks
  may serve an owner's orders iff the owner is in the carrier's adjacency; exactly
  ``constraints.share_eligible``).
- :func:`validate_arm` -- message 14, verbatim (pools must pair only adjacent
  companies); the built-in solvers call it from their top ``bind``, so the run-start
  binding fails a hand-broken structure before any agent spawns.

Reads only ``world.get("cooperation")``, so a ``RunWorld`` records exactly that key.

Solver helper (``policies/solver_lib``). Not a leaf module: imports
``assignment.constraints`` / ``assignment.pools`` at top.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import pools as assign_pools
from apps.container_logistics.policies.framework.errors import PolicyError

logger = logging.getLogger(__name__)

__all__ = ["Arm", "directed_pairs", "resolve_arm", "validate_arm"]


@dataclass(frozen=True)
class Arm:
    """The ACTIVE cooperation structure of a run world.

    ``structure`` is the structure dict itself; ``adjacency`` is
    ``constraints.share_adjacency(structure)`` frozen (``{haulier: frozenset}``);
    ``components`` the structure's ``components`` list; ``component_of`` maps a member
    of a multi-company component to its index (singletons are left out, so such a
    haulier partitions on its own id).
    """

    id: Any
    adjacency: Mapping[str, frozenset[str]]
    components: tuple[Any, ...]
    component_of: Mapping[str, int]
    structure: Mapping[str, Any]


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


def _active_structure(world: Mapping[str, Any]) -> Any:
    # One explicit read of the one key, so a RunWorld records it even when the world is
    # empty (``active_cooperation_structure`` skips the read on a falsy profile).
    return assign_constraints.active_cooperation_structure({"cooperation": world.get("cooperation")})


def resolve_arm(world: Mapping[str, Any]) -> Arm | None:
    """The active arm of ``world`` (``None``: no valid cooperation block, own-haulier only)."""
    structure = _active_structure(world)
    if structure is None:
        return None
    adjacency = assign_constraints.share_adjacency(structure)
    return Arm(
        id=structure.get("id") if isinstance(structure, dict) else None,
        adjacency=MappingProxyType({k: frozenset(v) for k, v in adjacency.items()}),
        components=tuple(structure.get("components") or ()) if isinstance(structure, dict) else (),
        component_of=MappingProxyType(_component_of(structure, adjacency)),
        structure=structure,
    )


def directed_pairs(arm: Arm | None) -> frozenset[tuple[str, str]]:
    """Every ``(owner, carrier)`` with ``owner`` in ``arm.adjacency[carrier]`` and ``owner != carrier``."""
    if arm is None:
        return frozenset()
    return frozenset((owner, carrier) for carrier, owners in arm.adjacency.items()
                     for owner in owners if owner != carrier)


def validate_arm(world: Mapping[str, Any]) -> None:
    """Raise ``PolicyError`` (message 14) when a pool pairs companies the adjacency does not.

    Checks the ACTIVE structure of ``world`` (the one the run executes, after any
    cooperation override): every two members of every pool (``pools_from_structure``:
    authored pools, else one per edge) must be adjacent in both directions. A compiled
    structure satisfies this by construction (pools are canonical, edges and adjacency
    derive from them); only a hand edit can break it. Run start (bind) only.
    """
    structure = _active_structure(world)
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
