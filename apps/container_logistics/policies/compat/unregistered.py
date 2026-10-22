"""An UNREGISTERED solver instance as a planner leaf (compat: hand-injected solvers).

Single responsibility: keep the old rule for a solver object that has no ``@policy``
registration (a hand-injected test solver set as ``AssignmentApp._solver``), moved
from ``dispatch/profile.py:_unregistered_node`` and ``pair_solver.PairSolver.decide``
(plan ``plan_contract_final.md`` §5.1 row 6):

- :func:`legacy_spatial_rule` -- its candidate kind is ``spatial`` iff the profile's
  ``use_spatial_matching`` (absent means true) AND the instance has ``solve_pairs``.
- :class:`UnregisteredPairLeaf` -- the wrapper the framework calls for it. ``bind``
  declares like a built-in pair leaf (the arm's pairs at the top, the hint below);
  ``solve_tick`` runs the instance's own ``solve_tick`` when its class defines one,
  else its legacy ``decide(ctx)`` when its class defines one, else the pair-solver default (``solve_pairs`` on the spatial kind,
  ``solve`` otherwise) under the instance's ``decision_rng``. The instance itself
  stays the object injections reach (the parent's ``inner``).

Compat (old inputs). Imports ``solver_lib`` and ``dispatch.profile`` locally.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["UnregisteredPairLeaf", "legacy_spatial_rule"]


def legacy_spatial_rule(instance: Any, profile: Mapping[str, Any]) -> bool:
    """``use_spatial_matching(profile) and hasattr(instance, "solve_pairs")`` (the pre-v2 rule)."""
    from apps.container_logistics.policies.dispatch.profile import use_spatial_matching

    return use_spatial_matching(profile) and hasattr(instance, "solve_pairs")


class UnregisteredPairLeaf:
    """Wrapper around an unregistered solver ``instance`` whose kind is ``spatial`` or not."""

    def __init__(self, instance: Any, *, spatial: bool) -> None:
        self.instance = instance
        self.spatial = bool(spatial)

    def bind(self, params: Any, world: Mapping[str, Any], *, top: bool) -> Any:
        from apps.container_logistics.policies.solver_lib.pair_leaf import pair_leaf_declaration

        return pair_leaf_declaration(world, top=top, spatial=self.spatial)

    def solve_tick(self, tick: Any, params: Any) -> Any:
        from apps.container_logistics.policies.compat.legacy_context import LegacyDecideContext, result_from_legacy
        from apps.container_logistics.policies.dispatch.base import DispatchPolicy
        from apps.container_logistics.policies.solver_lib.pair_solver import PairSolver, solve_pair_tick

        instance = self.instance
        cls = type(instance)
        own_solve_tick = getattr(cls, "solve_tick", None)
        if own_solve_tick is not None and own_solve_tick not in (PairSolver.solve_tick, DispatchPolicy.solve_tick):
            return instance.solve_tick(tick, params)
        decide = getattr(cls, "decide", None)
        if decide is not None and decide not in (PairSolver.decide, DispatchPolicy.decide):
            kind = "spatial" if self.spatial else "sampled"
            return result_from_legacy(instance.decide(LegacyDecideContext(tick, default_candidate_kind=kind)))
        return solve_pair_tick(instance, tick, spatial=self.spatial)
