"""``Partitioned``: decide each cooperation group of the tick with an inner per-scope solver.

Single responsibility: the pre-v2 ``partitioned`` topology as an ordinary built-in
dispatch solver (plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.3). The
tick is split by ``compose.cooperation_groups`` (a multi-company component of the
active structure, else one haulier); each group gets its own context
(``scopes.partition_context``) and is decided by ``inner`` through
``compose.decide_in_scope`` via ``ctx.execute`` (``solve_in_thread`` runs each group in
a worker thread, as before). The group plans are merged in group order, which is the
pre-v2 commit order.

Byte identity with pre-v2 partitioned runs (the dispatch golden): every group context
carries the INNER's own ``rng`` / ``tiebreak_seed`` / ``rebate_book`` (the ``random``
module, ``None``, ``None`` unless something injected them) and samples from the
``random`` module; this solver never reads ``ctx.rng``, so the tick ``Random`` is never
built. Hence the ``module_rng`` and ``order_sensitive`` declarations, and the ``v1``
share-tag format (reserved to this policy). An inner's non-contract exception
propagates, as before; a contract violation drops only that group (message 19).

Policy side of the boundary: the framework still checks the merged plan against the
world rule, commits it and does the accounting.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref

from .. import compose, scopes
from ..composite import CompositePolicy
from ..context import DispatchContext
from ..plan import Plan

logger = logging.getLogger(__name__)

__all__ = ["Partitioned"]

_INNER_HELP = (
    "The per-scope solver each cooperation group is decided by (a leaf: GreedyNearest, "
    "RandomAssignment). --solver sets it."
)


@policy(
    slot="dispatch",
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
    """Decide each cooperation group of the tick with ``inner``; merge the plans in group order."""

    @dataclass(frozen=True)
    class Params(PolicyParams):
        inner: PolicyRef = param(ref("RandomAssignment"), kind="dispatch", leaf_only=True, help=_INNER_HELP)

    def set_rng(self, rng) -> None:
        """Recorded here only, NOT forwarded: the groups keep the inner's own RNG (byte identity, S12)."""
        self._rng = rng

    def set_tiebreak(self, tick_seed) -> None:
        """Recorded here only, NOT forwarded: pre-v2 partitioned ties are a shuffle, never a tiebreak."""
        self._tiebreak_seed = tick_seed

    def decide(self, ctx: DispatchContext) -> Plan:
        """One ``decide_in_scope`` per cooperation group, through ``ctx.execute``; a dropped group adds nothing."""
        chains: dict[str, tuple[str, ...]] = {}
        for group in compose.cooperation_groups(ctx):
            plan = ctx.execute(compose.decide_in_scope, self.inner, self.inner_node, self._group_context(ctx, group))
            if plan is not None:
                chains.update(plan.chains)
        return Plan(chains=chains)

    def _group_context(self, ctx: DispatchContext, group: compose.GroupScope) -> DispatchContext:
        """The group's context: the inner's RNG/tiebreak/rebates, the world rule, module sampling."""
        return scopes.partition_context(
            policy=self.inner,
            params=self.inner_node.params,
            candidate_kind=self.inner_node.candidate_kind,
            trucks=list(group.trucks),
            orders=list(group.orders),
            partition_key=group.key,
            rule=ctx.world.rule,
            settings=ctx.candidate_settings,
            accounting_cost=ctx.accounting_cost,
            clock=ctx.clock,
            # The random MODULE on purpose, not ctx.rng: the pre-v2 inline fallback called
            # random.sample, and the dispatch golden locks module-RNG consumption (plan
            # docs/plans/policy_slots_p0_p3.md §4 P1.5, §7 risk 1). This is the draw the
            # module_rng declaration above covers; another composite passes ctx.rng here.
            sample_rng=random,
            parent=ctx,
        )
