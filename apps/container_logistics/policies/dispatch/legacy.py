"""Legacy dispatch selection: the pre-v2 keys translated into a dispatch selection tree.

Single responsibility: the deterministic translation of the legacy spellings
(``solver``/``strategy``, ``solverParams``/``solver_params``, ``planner.deployment``,
``planner.topology``, ``planner.market``) into ``policies.dispatch``. Plan
``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §4.

Round 2 C1 ships only :data:`LEGACY_LEAF_DEFAULT`; work item C2 completes the module
(``legacy_topology``, ``legacy_market_params``, ``wrap_legacy_leaf``, the echoes).

Leaf module (plan §2.7, round-2 §4): stdlib plus ``policies.framework`` errors only;
never imports ``apps.container_logistics.assignment.*``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["LEGACY_LEAF_DEFAULT"]

#: The per-scope solver a legacy selection with no ``solver``/``strategy`` runs: the
#: pre-v2 default. It is the default ``inner`` of the slot default ``Partitioned``.
LEGACY_LEAF_DEFAULT = "RandomAssignment"
