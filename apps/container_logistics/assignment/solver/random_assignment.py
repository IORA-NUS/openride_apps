from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, param, policy

from .base import Assignment, BaseAssignmentSolver, Cost, Order, PairAllowed, Truck


@policy(
    slot="dispatch",
    name="RandomAssignment",
    version="1",
    description="Baseline: shuffled orders, each given a random feasible free truck; ignores cost.",
    # Scope-agnostic leaf: selected at top level it plans the whole tick centrally;
    # as a composite's inner it decides one scope (round-2 §2.4).
    information_model="central",
    order_sensitive="byte-identical to the pre-v2 shuffle; order independence would change existing results",
    # Budget for the battery's performance.scale check (plan P3.3): max(3 x worst, 500).
    # Measured 2026-10-21, decide() on testing.fixtures.scale() (1000 trucks x 500
    # orders, sampled to 500 trucks), 2 x 5 runs, load avg 4.5 on 32 cores:
    # 169.7-183.2 ms, worst 183.2 ms -> 3 x 183.2 = 549.6 -> 550. This box varies
    # 1.3-2x on its own (CLAUDE.md §17), hence the 3x margin and the best-of-3.
    perf_budget_ms=550,
)
class RandomAssignmentSolver(BaseAssignmentSolver):
    """Baseline: shuffle orders, give each the first feasible free truck at random.

    Ignores ``cost`` — kept as the reference/no-optimisation strategy.
    """

    @dataclass(frozen=True)
    class Params(PolicyParams):
        candidates: str = param(
            "sampled",
            choices=("sampled", "spatial"),
            help=(
                "How candidate trucks are generated: 'sampled' samples a capped set of "
                "trucks per haulier (the historical baseline); 'spatial' restricts "
                "candidates to the spatial index's nearby pairs."
            ),
        )

    def decide(self, ctx) -> Plan:
        """``solve`` over the sampled trucks (default), or ``solve_pairs`` on spatial pairs.

        Both ignore cost; the accounting cost is passed only to honour the signatures.
        Runs under ``decision_rng``: the shuffles draw from the scope's RNG.
        """
        with self.decision_rng(ctx):
            if ctx.params.candidates == "spatial":
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

    def solve_pairs(
        self,
        candidate_pairs: Iterable[Tuple[Truck, Order]],
        *,
        cost: Cost,
    ) -> Assignment:
        """``solve``'s shuffle-based matching restricted to pre-built feasible pairs.

        New behaviour, reachable only under ``candidates="spatial"`` (a new,
        non-comparable arm; the default stays ``sampled``). Orders are taken in
        first-seen order and shuffled; each order then gets a random truck among its
        candidates that is still free. Feasibility is assumed already applied.
        """
        trucks_by_order: Dict[str, List[Truck]] = {}
        order_by_id: Dict[str, Order] = {}
        for truck, order in candidate_pairs:
            oid = str(order.get("_id"))
            if oid not in order_by_id:
                order_by_id[oid] = order
                trucks_by_order[oid] = []
            trucks_by_order[oid].append(truck)
        order_ids = list(order_by_id)
        self.rng.shuffle(order_ids)
        used_truck_ids = set()
        assignment: Assignment = []
        for oid in order_ids:
            candidates = [t for t in trucks_by_order[oid] if t.get("_id") not in used_truck_ids]
            if not candidates:
                continue
            self.rng.shuffle(candidates)
            chosen = candidates[0]
            used_truck_ids.add(chosen.get("_id"))
            assignment.append((chosen, order_by_id[oid]))
        return assignment
