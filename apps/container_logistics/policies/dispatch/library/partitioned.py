"""``Partitioned``: decide each cooperation group of the tick with an inner per-scope solver.

Single responsibility: the pre-v2 ``partitioned`` topology as an ordinary built-in
planner solver (plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.3; contract
``plan_contract_final.md`` §2). At run start ``bind`` resolves the scenario's active
cooperation arm (``solver_lib.cooperation``), validates it (message 14) and, at the
top, declares the arm's cross-company pairs. Each tick is split by
:func:`cooperation_groups` (a multi-company component of the active arm, else one
haulier; a verbatim move of the old ``compose.cooperation_groups``); each group is a
sub-tick (``tick.restrict``) decided by ``inner`` through ``tick.call`` (the framework
offloads each call when ``solve_in_thread`` is on, as before). The group results are
merged in group order, which is the pre-v2 commit order, with each inner result's
``solver_notes``.

Byte identity with pre-v2 partitioned runs (the dispatch golden): every group sub-tick
carries the INNER's own ``rng`` / ``tiebreak_seed`` / ``rebate_book`` (the ``random``
module, ``None``, ``None`` unless something injected them) and samples from the
``random`` module; this solver never reads ``tick.rng``, so the tick ``Random`` is
never built. Hence the ``module_rng`` and ``order_sensitive`` declarations, and the
``v1`` share-tag format (reserved to this policy). An inner's non-contract exception
propagates, as before; a contract violation drops only that group (message 19).

Policy side of the boundary: the framework still checks the merged result against the
top's declaration, commits it and does the accounting.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.solver_lib import cooperation

from ..bind import Declaration
from ..composite import CompositePolicy
from ..result import Result

logger = logging.getLogger(__name__)

__all__ = ["GroupScope", "Partitioned", "cooperation_groups"]

Doc = dict[str, Any]

_INNER_HELP = (
    "The per-scope solver each cooperation group is decided by (a leaf: GreedyNearest, "
    "RandomAssignment). --solver sets it."
)


@policy(
    slot="planner",
    name="Partitioned",
    version="1",
    description="One planner per cooperation group (a partnered component, else one company), "
                "each group decided by the inner solver. The pre-v2 'partitioned' topology.",
    information_model="per_group",
    share_tag_format="v1",
    module_rng="pre-v2 byte identity: each group's inner shuffle and its sample cap draw from the "
               "random MODULE, exactly as the pre-v2 partitioned path did",
    order_sensitive="inner ties broken by a module-RNG shuffle over input order (pre-v2)",
    # Per-arm budgets for the battery's performance.scale check (round-2 C5 rule:
    # max(3 x worst of 2 x 5 runs, 500)). Measured 2026-10-21 on the whole-tick
    # testing.fixtures.scale() (1000 trucks x 500 orders, one haulier, so one group),
    # 2 x 5 runs, load avg 5.8-6.5 on 32 cores:
    #   default (inner RandomAssignment, sampled): 175.4-217.0 ms -> 3 x 217.0 = 651 -> 660
    #   inner=GreedyNearest (spatial):             110.0-129.6 ms -> 3 x 129.6 = 389 -> 500
    # (round-1 partition-scope measurement at load 3.3 was 165.1-171.2 ms -> 520.)
    perf_budget_ms=660,
    arm_budgets_ms={"inner=GreedyNearest": 500},
)
class Partitioned(CompositePolicy):
    """Decide each cooperation group of the tick with ``inner``; merge the results in group order."""

    @dataclass(frozen=True)
    class Params(PolicyParams):
        inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True, help=_INNER_HELP)

    def set_rng(self, rng) -> None:
        """Recorded here only, NOT forwarded: the groups keep the inner's own RNG (byte identity, S12)."""
        self._rng = rng

    def set_tiebreak(self, tick_seed) -> None:
        """Recorded here only, NOT forwarded: pre-v2 partitioned ties are a shuffle, never a tiebreak."""
        self._tiebreak_seed = tick_seed

    def bind(self, params: Any, world: Mapping[str, Any], *, top: bool) -> Declaration:
        """At the top: validate the active arm (message 14) and declare its cross-company pairs."""
        cooperation.validate_arm(world)
        arm = cooperation.resolve_arm(world)
        return Declaration(cross_company=cooperation.directed_pairs(arm) if top else frozenset())

    def solve_tick(self, tick: Any, params: Any) -> Result:
        """One ``tick.call(params.inner, ...)`` per cooperation group; a dropped group adds nothing."""
        arm = cooperation.resolve_arm(tick.world)
        pairs: list[tuple[str, str]] = []
        notes: dict[tuple[str, str], Any] = {}
        for group in cooperation_groups(tick, arm.component_of if arm is not None else {}):
            kind, value = group.key
            sub = tick.restrict(
                trucks=group.trucks,
                orders=group.orders,
                scope=f"partition:{kind}:{value}",
                # The random MODULE on purpose, not tick.rng: the pre-v2 inline fallback called
                # random.sample, and the dispatch golden locks module-RNG consumption (plan
                # docs/plans/policy_slots_p0_p3.md §4 P1.5, §7 risk 1). This is the draw the
                # module_rng declaration above covers; another composite passes tick.rng here.
                sample_rng=random,
            )
            result = tick.call(params.inner, sub)
            if result is not None:
                pairs.extend(result.pairs)
                notes.update(result.solver_notes)
        return Result(pairs=tuple(pairs), solver_notes=notes)


@dataclass(frozen=True)
class GroupScope:
    """One cooperation group of a tick: its partition key and its trucks and orders, in input order."""

    key: tuple[str, Any]
    trucks: tuple[Doc, ...]
    orders: tuple[Doc, ...]


def cooperation_groups(tick: Any, component_of: Mapping[str, int]) -> list[GroupScope]:
    """The groups of ``tick`` that have both orders and trucks, in first-seen order of their orders.

    A haulier in a multi-company component of the active arm (``component_of``) is
    keyed ``("component", idx)``; any other haulier ``("haulier", id)``; a document
    without a haulier never matches (fail-closed) and is left out. Pure (no RNG).
    Verbatim move of the old ``compose.cooperation_groups``.
    """

    # Partition both sides by the structure's connected component: one logical planner
    # per component. A haulier with no partners (or no structure at all) is its own
    # singleton partition, the pre-cooperation haulier bucket.
    def _partition_key(haulier_id: Any) -> tuple[str, Any] | None:
        if haulier_id is None:
            return None
        hid = str(haulier_id)
        if hid in component_of:
            return ("component", component_of[hid])
        return ("haulier", hid)

    truck_buckets: dict[Any, list[Doc]] = {}
    for t in tick.raw_trucks:
        truck_buckets.setdefault(_partition_key(assign_constraints.haulier_of(t)), []).append(t)
    order_buckets: dict[Any, list[Doc]] = {}
    for o in tick.raw_orders:
        order_buckets.setdefault(_partition_key(assign_constraints.haulier_of(o)), []).append(o)

    groups: list[GroupScope] = []
    for key, bucket_orders in order_buckets.items():
        if key is None:  # a None haulier never matches (fail-closed in haulier_matches)
            continue
        bucket_trucks = truck_buckets.get(key)
        if not bucket_trucks:
            continue
        groups.append(GroupScope(key=key, trucks=tuple(bucket_trucks), orders=tuple(bucket_orders)))
    return groups
