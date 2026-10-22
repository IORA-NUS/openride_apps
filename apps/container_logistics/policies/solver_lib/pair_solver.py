"""``PairSolver``: the v1 pair-matching contract, adapted to ``solve_tick(tick, params)``.

A pair solver is *pure*: it receives the candidate trucks/orders for one tick plus two
solve arguments and returns ``(truck, order)`` pairs. It must not query the DB,
import constraints, or know how feasibility/cost are computed -- the framework owns
that.

- ``pair_allowed(truck, order) -> bool`` -- hard feasibility (``tick.permitted``:
  ownership or a declared partnership, size, restricted areas, max reposition time).
- ``cost(truck, order) -> float`` -- soft objective to minimise (lower is better;
  e.g. repositioning/deadhead distance). Cost-blind solvers may ignore it.

``solve`` / ``solve_pairs`` are the PAIR API and are unchanged; the framework entry
point is :meth:`PairSolver.solve_tick` (never ``solve``, plan ``plan_contract_final.md``
B1). ``assignment/solver/base.py`` aliases ``BaseAssignmentSolver = PairSolver``, so
the injection API inherited from ``DispatchPolicy`` is reachable (and monkeypatchable)
through the old name.

Solver helper (``policies/solver_lib``). Leaf module (plan §2.7): never imports
``apps.container_logistics.assignment.*`` at module top (the candidate helpers are
imported inside :meth:`PairSolver.solve_tick`).
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, Any

from apps.container_logistics.policies.dispatch.base import Assignment, Cost, DispatchPolicy, Order, PairAllowed, Truck
from apps.container_logistics.policies.dispatch.result import Result

if TYPE_CHECKING:
    from apps.container_logistics.policies.dispatch.tick import Tick

logger = logging.getLogger(__name__)

__all__ = ["PairSolver", "solve_pair_tick"]


def solve_pair_tick(solver: Any, tick: "Tick", *, spatial: bool) -> Result:
    """The legacy default decision of a pair solver over ``tick``, under ``solver.decision_rng``.

    ``spatial``: ``solve_pairs`` over the caller's ``tick.candidates`` when supplied,
    else the tick's own spatial pairs; otherwise ``solve`` over the (possibly sampled)
    trucks and every order. Both use the framework's measurement cost. The sample is
    taken only on the non-spatial branch because it consumes RNG (plan §7 risk 1).
    """
    from . import candidates  # local: candidates imports assignment.*

    settings = candidates.CandidateSettings.from_world(tick.world)
    with solver.decision_rng(tick):
        if spatial:
            pairs = tick.candidates if tick.candidates is not None else candidates.spatial_pairs(tick, settings)
            return Result.from_pairs(solver.solve_pairs(pairs, cost=tick.measure.cost))
        return Result.from_pairs(solver.solve(candidates.tick_sampled_trucks(tick, settings),
                                              list(tick.raw_orders), pair_allowed=tick.permitted,
                                              cost=tick.measure.cost))


class PairSolver(DispatchPolicy):
    """A planner policy expressed as ``solve`` (and optionally ``solve_pairs``).

    The default :meth:`solve_tick` reproduces the legacy call shape exactly: on the
    ``spatial`` candidate kind (``params.candidates == "spatial"`` where declared) it
    hands the tick's candidate pairs to ``solve_pairs``; otherwise it hands the
    (possibly sampled) trucks and the tick's orders to ``solve``. A subclass that
    overrides the legacy ``decide(ctx)`` keeps running it (through
    ``DispatchPolicy.solve_tick``).
    """

    @abstractmethod
    def solve(
        self,
        trucks: list[Truck],
        orders: list[Order],
        *,
        pair_allowed: PairAllowed,
        cost: Cost,
    ) -> Assignment:
        raise NotImplementedError

    def solve_tick(self, tick: "Tick", params: Any) -> Result:
        """``solve_pairs`` on the spatial kind, ``solve`` otherwise; a legacy ``decide`` override wins."""
        if type(self).decide is not PairSolver.decide:
            return super().solve_tick(tick, params)
        return solve_pair_tick(self, tick, spatial=getattr(params, "candidates", "sampled") == "spatial")

    def decide(self, ctx: Any) -> Any:
        """Legacy adapter (a ``compat.legacy_context.LegacyDecideContext``): the same call shape.

        ``ctx.default_candidate_kind == "spatial"`` only for a policy whose candidate
        kind resolved spatial. Runs under :meth:`decision_rng`.
        """
        from apps.container_logistics.policies.compat.legacy_context import Plan

        with self.decision_rng(ctx):
            if ctx.default_candidate_kind == "spatial":
                return Plan.from_pairs(
                    self.solve_pairs(ctx.candidate_pairs("spatial"), cost=ctx.accounting_cost)
                )
            return Plan.from_pairs(
                self.solve(
                    ctx.sampled_trucks(),
                    list(ctx.raw_orders),
                    pair_allowed=ctx.allowed_raw,
                    cost=ctx.accounting_cost,
                )
            )
