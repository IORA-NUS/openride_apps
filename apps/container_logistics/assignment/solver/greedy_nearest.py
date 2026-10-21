from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from apps.container_logistics.policies.framework import PolicyParams, param, policy

from ..tiebreak import pair_tiebreak
from .base import Assignment, BaseAssignmentSolver, Cost, Order, PairAllowed, Truck


@policy(
    slot="dispatch",
    name="GreedyNearest",
    version="1",
    description="Globally greedy nearest-pair sweep, dual-cycle aware.",
)
class GreedyNearestSolver(BaseAssignmentSolver):
    """Globally greedy nearest-pair matching within a haulier bucket.

    Builds every feasible ``(truck, order)`` pair, sorts by injected ``cost``
    (ascending — repositioning/deadhead distance), and sweeps the sorted list
    assigning each pair whose truck and order are both still free. This minimises
    total deadhead more aggressively than a per-truck or per-order local greedy.

    Pairs whose cost is unknown (``inf`` / ``nan`` — typically missing geometry)
    are pushed to the end and broken randomly, so the solver degrades to
    random-like behaviour instead of crashing when coordinates are absent.

    Complexity: O(T x O) to build pairs + O(T x O log(T x O)) to sort. At the
    current per-tick caps (<=500 orders, bucketed per haulier) this is cheap.
    """

    @dataclass(frozen=True)
    class Params(PolicyParams):
        candidates: str = param(
            "spatial",
            choices=("spatial", "sampled"),
            help=(
                "How candidate (truck, order) pairs are generated: 'spatial' asks the "
                "spatial index for nearby pairs only; 'sampled' samples a capped set of "
                "trucks per haulier and scores every truck x order pair."
            ),
        )
        dual_cycle_bonus_km: float = param(
            5.0,
            ge=0,
            help=(
                "Km subtracted from a pair's deadhead cost when the truck's last drop-off "
                "is within dual_cycle_radius_km of the order's pickup (a dual cycle). "
                "The cost is clamped at 0. 0 disables the bonus."
            ),
        )
        dual_cycle_radius_km: float = param(
            0.5,
            ge=0,
            help=(
                "Max distance in km between the truck's last drop-off and the order's "
                "pickup for the pair to count as a dual cycle and earn the bonus."
            ),
        )

    def solve(
        self,
        trucks: List[Truck],
        orders: List[Order],
        *,
        pair_allowed: PairAllowed,
        cost: Cost,
    ) -> Assignment:
        trucks = list(trucks)
        orders = list(orders)
        if not trucks or not orders:
            return []

        def feasible_pairs() -> Iterable[Tuple[Truck, Order]]:
            for truck in trucks:
                for order in orders:
                    if pair_allowed(truck, order):
                        yield (truck, order)

        return self.solve_pairs(feasible_pairs(), cost=cost)

    def solve_pairs(
        self,
        candidate_pairs: Iterable[Tuple[Truck, Order]],
        *,
        cost: Cost,
    ) -> Assignment:
        """Greedy sweep over a pre-built set of feasible ``(truck, order)`` pairs.

        Identical ranking/assignment logic to :meth:`solve`, but the caller supplies
        the candidate pairs — e.g. the spatial index yields only nearby pairs — so
        the cost is O(candidates) instead of O(trucks x orders). ``pair_allowed`` is
        assumed already applied during candidate generation.
        """
        # (cost, geometry_missing) ordering: real costs first (ascending), then
        # unknown-cost pairs. We shuffle before the stable sort so equal-cost and
        # unknown-cost pairs don't inherit a bias from truck/order list position.
        scored: List[Tuple[float, int, Truck, Order]] = []
        for truck, order in candidate_pairs:
            try:
                c = float(cost(truck, order))
            except (TypeError, ValueError):
                c = math.inf
            missing = 1 if (math.isinf(c) or math.isnan(c)) else 0
            scored.append((c if missing == 0 else math.inf, missing, truck, order))

        if not scored:
            return []

        if self._tiebreak_seed is None:
            # LEGACY / partitioned path — byte-identical to before: ``self.rng`` is
            # the ``random`` MODULE unless a Random was injected via ``set_rng``.
            self.rng.shuffle(scored)
            scored.sort(key=lambda row: (row[0], row[1]))
        else:
            # POOLED path — a TOTAL order, so the result cannot depend on the
            # caller's input order (I-P5, plan §13.4 FIX-3). A shuffle could not
            # deliver this: `scored`'s pre-shuffle order is a function of the input
            # list order, and the sort below is stable, so equal-cost pairs were
            # broken differently per permutation. Ties are the NORMAL case here —
            # `assignment_cost` clamps every dual-cycle pair within the bonus
            # radius to exactly 0.0 (audit P5).
            seed = self._tiebreak_seed
            scored.sort(
                key=lambda row: (
                    row[0],
                    row[1],
                    pair_tiebreak(seed, row[3].get("_id"), row[2].get("_id")),
                )
            )

        used_truck_ids = set()
        used_order_ids = set()
        assignment: Assignment = []
        for _c, _missing, truck, order in scored:
            tid = truck.get("_id")
            oid = order.get("_id")
            if tid in used_truck_ids or oid in used_order_ids:
                continue
            used_truck_ids.add(tid)
            used_order_ids.add(oid)
            assignment.append((truck, order))
        return assignment
