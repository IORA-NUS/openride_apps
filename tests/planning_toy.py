"""Test-only planning solvers for the F1/F3 contract (NOT shipped: registered only inside tests).

:func:`register_toys` registers them inside the caller's ``isolated_registry()``:

- ``ToyDayPlan`` (``top_only=True``): declares planning (day start + every 18 steps,
  foresight ``day``). On a planning tick it builds one itinerary per truck (each order, in
  id order, goes to the first free permitted truck of the same haulier, trucks in id
  order) and keeps it on the INSTANCE (keyed by ``tick.run_id``), committing nothing and
  prefetching every itinerary head. On a dispatch tick each truck takes its head when the
  head is in the tick, still ``unassigned`` in ``tick.lookup`` (when looked up) and
  ``permitted``. Deterministic and order-independent (ids only), no module RNG.
- ``ToyPlanCommits`` (``top_only=True``): declares planning but returns a pair on its
  planning tick (R6 must discard it).
- ``ToyPlanNotTopOnly``: declares planning but is NOT registered ``top_only`` (the
  battery's ``contract.planning_declaration`` must FAIL; as an inner, message 24).
- ``ToyBadPrefetch`` (``top_only=True``): prefetches a non-string id (R7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.container_logistics.assignment.constraints import haulier_of
from apps.container_logistics.policies.dispatch.bind import Declaration
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyParams, policy

PLANNING = {"at_day_start": True, "every_steps": 18, "foresight": "day", "max_hold_seconds": 30}


class _ToyBase:
    @dataclass(frozen=True)
    class Params(PolicyParams):
        pass

    planning: Any = PLANNING

    def __init__(self, params=None):
        self.params = params
        self.itineraries: dict[str, dict[str, list[str]]] = {}
        self.plan_ticks = 0
        self.dispatch_ticks = 0

    def bind(self, params, world, *, top):
        return Declaration(planning=self.planning)

    def _plan(self, tick) -> dict[str, list[str]]:
        free = sorted(tick.raw_trucks, key=lambda t: str(t["_id"]))
        chains: dict[str, list[str]] = {str(t["_id"]): [] for t in free}
        for order in sorted(tick.raw_orders, key=lambda o: str(o["_id"])):
            for truck in free:
                tid = str(truck["_id"])
                if not chains[tid] and haulier_of(truck) == haulier_of(order) and tick.permitted(truck, order):
                    chains[tid].append(str(order["_id"]))
                    break
        return {t: c for t, c in chains.items() if c}

    def solve_tick(self, tick, params):
        if tick.kind == "plan":
            self.plan_ticks += 1
            plan = self._plan(tick)
            self.itineraries[tick.run_id] = plan
            heads = tuple(c[0] for _t, c in sorted(plan.items()))
            return Result(prefetch=heads, tick_notes={"planned_trucks": len(plan)})
        self.dispatch_ticks += 1
        plan = self.itineraries.get(tick.run_id, {})
        trucks, orders = tick.id_index
        pairs = []
        for tid in sorted(plan):
            head = plan[tid][0]
            truck, order = trucks.get(tid), orders.get(head)
            if truck is None or order is None:
                continue
            looked = tick.lookup.get(head)
            if looked is not None and looked.get("state") != "unassigned":
                continue
            if tick.permitted(truck, order):
                pairs.append((truck, order))
        return Result.from_pairs(pairs)


class ToyDayPlan(_ToyBase):
    """Plans one head per truck at day start; dispatches heads."""


class ToyPlanCommits(_ToyBase):
    """Returns a pair on its planning tick (R6)."""

    def solve_tick(self, tick, params):
        if tick.kind == "plan" and tick.raw_trucks and tick.raw_orders:
            plan = self._plan(tick)
            if plan:
                tid, chain = sorted(plan.items())[0]
                return Result(pairs=((tid, chain[0]),))
        return super().solve_tick(tick, params)


class ToyPlanNotTopOnly(_ToyBase):
    """Declares planning without registering top_only."""


class ToyBadPrefetch(_ToyBase):
    """Prefetches a non-string id (R7)."""

    def solve_tick(self, tick, params):
        if tick.kind == "plan":
            return Result(prefetch=("ok", 7))
        return Result()


def register_toys() -> None:
    """Register every toy (call inside ``framework.registry.isolated_registry()``)."""
    policy(slot="planner", name="ToyDayPlan", version="1", description="Test-only day planner.",
           top_only=True)(ToyDayPlan)
    policy(slot="planner", name="ToyPlanCommits", version="1", description="Test-only R6 offender.",
           top_only=True)(ToyPlanCommits)
    policy(slot="planner", name="ToyPlanNotTopOnly", version="1",
           description="Test-only planner that forgot top_only.")(ToyPlanNotTopOnly)
    policy(slot="planner", name="ToyBadPrefetch", version="1", description="Test-only R7 offender.",
           top_only=True)(ToyBadPrefetch)
