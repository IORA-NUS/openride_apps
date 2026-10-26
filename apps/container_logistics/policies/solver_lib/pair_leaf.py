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
  candidate knobs (``CandidateSettings.from_world``) its ``solve_tick`` will read;
- its provenance (``solver_lib.provenance``) carries those knobs; at the top also the
  ``central`` badge and the per-haulier sample scope, plus the arm's run record
  (``cooperation.arm_declaration_fields``: the ``COOPERATION`` stamp setting and the
  arm id as a run-name segment).

:data:`CENTRAL_BADGE` / :data:`CENTRAL_BADGE_TOOLTIP` are the leaves' ``@policy``
display text too.

Solver helper (``policies/solver_lib``); the framework never imports it. Not a leaf
module (``cooperation`` / ``candidates`` reach ``assignment.*``).
"""

from __future__ import annotations

from typing import Any, Mapping

from apps.container_logistics.policies.dispatch.bind import Declaration

from . import cooperation
from .candidates import TICK_SAMPLE_SCOPE
from .provenance import node_provenance

__all__ = ["CENTRAL_BADGE", "CENTRAL_BADGE_TOOLTIP", "PairLeafMixin", "pair_leaf_declaration"]

#: A pair leaf's badge: selected at the top it plans the whole tick centrally.
CENTRAL_BADGE = "central"
CENTRAL_BADGE_TOOLTIP = "One planner decides the whole tick with every company’s trucks and orders in view."


def pair_leaf_declaration(world: Mapping[str, Any], *, top: bool, spatial: bool) -> Declaration:
    """The declaration of a pair leaf whose candidate path is ``spatial`` (or sampled)."""
    hint = "spatial" if spatial else None
    # Reads the four knobs first (solve_tick reads the same ones), as before.
    provenance = node_provenance(world, badge=CENTRAL_BADGE, sample_scope=TICK_SAMPLE_SCOPE) if top \
        else node_provenance(world)
    if not top:
        return Declaration(candidate_hint=hint, provenance=provenance)
    cooperation.validate_arm(world)
    arm = cooperation.resolve_arm(world)
    return Declaration(cross_company=cooperation.directed_pairs(arm), candidate_hint=hint,
                       provenance=provenance, **cooperation.arm_declaration_fields(arm))


class PairLeafMixin:
    """``bind(params, world, *, top)`` for a leaf whose ``params.candidates`` picks its candidate path."""

    def bind(self, params: Any, world: Mapping[str, Any], *, top: bool) -> Declaration:
        return pair_leaf_declaration(world, top=top, spatial=getattr(params, "candidates", None) == "spatial")
