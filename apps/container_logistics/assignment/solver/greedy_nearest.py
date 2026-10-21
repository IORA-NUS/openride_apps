from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Tuple

from apps.container_logistics.policies.dispatch.helpers import objective
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, param, policy

from ..tiebreak import pair_tiebreak
from .base import Assignment, BaseAssignmentSolver, Cost, Order, PairAllowed, Truck


@policy(
    slot="dispatch",
    name="GreedyNearest",
    version="1",
    description="Globally greedy nearest-pair sweep, dual-cycle aware.",
    # Scope-agnostic leaf: selected at top level it plans the whole tick centrally;
    # as a composite's inner it decides one scope (round-2 §2.4).
    information_model="central",
    # Budget for the battery's performance.scale check (plan P3.3): max(3 x worst, 500).
    # Measured 2026-10-21, decide() on testing.fixtures.scale() (1000 trucks x 500
    # orders, spatial), 2 x 5 runs, load avg 4.5 on 32 cores: 108.0-133.4 ms, worst
    # 133.4 ms -> 3 x 133.4 = 400 -> floor 500. This box varies 1.3-2x on its own
    # (CLAUDE.md §17), hence the 3x margin and the battery's best-of-3.
    perf_budget_ms=500,
    # Per-arm budget (round-2 C5 rule: max(3 x worst of 2 x 5 runs, 500)). Measured
    # 2026-10-21 on the whole-tick testing.fixtures.scale() (1000 trucks x 500 orders,
    # one haulier, tick-scope sample cap 500 -> 250k pairs), 2 x 5 runs, load avg
    # 5.8-6.5 on 32 cores: 1865.1-1929.1 ms, worst 1929.1 -> 3 x 1929.1 = 5787 -> 5790.
    # ABOVE STEP_SETTLE_SECONDS (2000 ms): at 1000 x 500 this arm makes the assignment
    # agent a per-step straggler (its ~1.9 s solve alone is close to the settle window).
    arm_budgets_ms={"candidates=sampled": 5790},
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

    def decide(self, ctx) -> Plan:
        """Rank by the dual-cycle cost built from this policy's own params.

        With the default/legacy params this equals the framework's accounting cost,
        because both are ``constraints.assignment_cost`` with the same two floats.
        Runs under ``decision_rng``: the shuffle / tiebreak are the scope's.
        """
        p = ctx.params
        # ``ctx.dual_cycle_cost`` with the knobs bound once (same function, same floats):
        # this is called once per candidate pair, and the per-call view unwrapping and
        # knob-dict build of the ctx method cost ~3-7 ms per golden tick (gate 6). The
        # pairs scored here are raw documents (candidate_pairs / sampled_trucks / raw_orders).
        cost = objective.dual_cycle_cost_fn(
            bonus_km=p.dual_cycle_bonus_km, radius_km=p.dual_cycle_radius_km
        )

        with self.decision_rng(ctx):
            if p.candidates == "spatial":
                return Plan.from_pairs(self.solve_pairs(ctx.candidate_pairs("spatial"), cost=cost))
            return Plan.from_pairs(
                self.solve(ctx.sampled_trucks(), list(ctx.raw_orders), pair_allowed=ctx.allowed_raw, cost=cost)
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
