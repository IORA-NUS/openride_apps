"""``PairSolver``: the v1 pair-matching contract, adapted to ``decide(ctx)``.

A pair solver is *pure*: it receives the candidate trucks/orders for one scope plus
two solve arguments and returns ``(truck, order)`` pairs. It must not query the DB,
import constraints, or know how feasibility/cost are computed — the framework owns
that.

- ``pair_allowed(truck, order) -> bool`` — hard feasibility (haulier, size,
  restricted areas, max reposition time).
- ``cost(truck, order) -> float`` — soft objective to minimise (lower is better;
  e.g. repositioning/deadhead distance). Cost-blind solvers may ignore it.

``assignment/solver/base.py`` aliases ``BaseAssignmentSolver = PairSolver``, so the
injection API inherited from :class:`DispatchPolicy` is reachable (and
monkeypatchable) through the old name. Plan ``docs/plans/policy_slots_p0_p3.md``
§4 P1.2.

Leaf module (plan §2.7): never imports ``apps.container_logistics.assignment.*``.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING

from .base import Assignment, Cost, DispatchPolicy, Order, PairAllowed, Truck
from .plan import Plan

if TYPE_CHECKING:
    from .context import DispatchContext

logger = logging.getLogger(__name__)

__all__ = ["PairSolver"]


class PairSolver(DispatchPolicy):
    """A dispatch policy expressed as ``solve`` (and optionally ``solve_pairs``).

    The default :meth:`decide` reproduces the legacy call shape exactly: on the
    ``spatial`` candidate kind it hands the scope's prebuilt candidate pairs to
    ``solve_pairs``; otherwise it hands the (possibly sampled) trucks and the
    scope's orders to ``solve``. Both use the framework's accounting cost.
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

    def decide(self, ctx: "DispatchContext") -> Plan:
        """Legacy adapter: ``solve_pairs`` on spatial scopes, ``solve`` otherwise.

        ``ctx.default_candidate_kind == "spatial"`` is only ever set for a policy
        that has ``solve_pairs`` (the legacy ``hasattr`` rule, applied once when the
        runner is built). ``sampled_trucks()`` is called only on the non-spatial
        branch because it consumes RNG (plan §7 risk 1). Runs under
        :meth:`decision_rng`, so ``self.rng`` is the scope's RNG.
        """
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
