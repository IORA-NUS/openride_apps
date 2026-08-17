from __future__ import annotations

from typing import List

from .base import Assignment, BaseAssignmentSolver, Cost, Order, PairAllowed, Truck


class RandomAssignmentSolver(BaseAssignmentSolver):
    """Baseline: shuffle orders, give each the first feasible free truck at random.

    Ignores ``cost`` — kept as the reference/no-optimisation strategy.
    """

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
        # ``self.rng`` is the ``random`` module unless ``set_rng`` injected a
        # tick-seeded Random — byte-identical to the previous module-global
        # ``shuffle`` for every existing caller (plan §6.7).
        self.rng.shuffle(orders)
        used_truck_ids = set()
        assignment: Assignment = []
        for order in orders:
            candidates = [
                t for t in trucks
                if t.get("_id") not in used_truck_ids and pair_allowed(t, order)
            ]
            if not candidates:
                continue
            self.rng.shuffle(candidates)
            chosen = candidates[0]
            used_truck_ids.add(chosen.get("_id"))
            assignment.append((chosen, order))
        return assignment
