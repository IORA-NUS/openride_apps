"""``PairLeafMixin``: the ``bind`` every built-in pair-matching leaf shares.

Single responsibility: what a pair leaf (``GreedyNearest``, ``RandomAssignment``)
declares at run start (plan ``plan_contract_final.md`` §2.3, §2.4, §7.7):

- at the TOP of a selection (a central planner over the whole tick) it resolves the
  scenario's active cooperation arm, validates it (message 14) and declares the arm's
  ``(owner, carrier)`` pairs: exactly the cross-company pairs the old framework world
  rule allowed;
- nested (``top=False``, e.g. a decomposition's ``inner``) it declares no
  cross-company pair (only the top declares, message 24);
- in both cases it hints its candidate kind (``"spatial"`` iff its own ``candidates``
  param is ``"spatial"``) so a caller may prebuild pairs for it, and it reads the four
  candidate knobs (``CandidateSettings.from_world``) its ``solve_tick`` will read.

Solver helper (``policies/solver_lib``); the framework never imports it. Not a leaf
module (``cooperation`` / ``candidates`` reach ``assignment.*``).
"""

from __future__ import annotations

from typing import Any, Mapping

from apps.container_logistics.policies.dispatch.bind import Declaration

from . import cooperation
from .candidates import CandidateSettings

__all__ = ["PairLeafMixin", "pair_leaf_declaration"]


def pair_leaf_declaration(world: Mapping[str, Any], *, top: bool, spatial: bool) -> Declaration:
    """The declaration of a pair leaf whose candidate path is ``spatial`` (or sampled)."""
    CandidateSettings.from_world(world)  # read now: solve_tick reads the same four knobs
    hint = "spatial" if spatial else None
    if not top:
        return Declaration(candidate_hint=hint)
    cooperation.validate_arm(world)
    return Declaration(cross_company=cooperation.directed_pairs(cooperation.resolve_arm(world)),
                       candidate_hint=hint)


class PairLeafMixin:
    """``bind(params, world, *, top)`` for a leaf whose ``params.candidates`` picks its candidate path."""

    def bind(self, params: Any, world: Mapping[str, Any], *, top: bool) -> Declaration:
        return pair_leaf_declaration(world, top=top, spatial=getattr(params, "candidates", None) == "spatial")
