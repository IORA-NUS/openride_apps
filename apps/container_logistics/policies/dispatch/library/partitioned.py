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
never built. Hence the ``module_rng`` and ``order_sensitive`` declarations (the
pre-v2 ``v1`` share-tag format of its runs is the compat name rule
``compat.wire.share_tag_format_for``). An inner's non-contract exception
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
from apps.container_logistics.policies.solver_lib.provenance import node_provenance

from ..bind import Declaration
from ..composite import CompositePolicy
from ..result import Result

logger = logging.getLogger(__name__)

__all__ = ["GROUP_BADGE", "GroupScope", "Partitioned", "cooperation_groups"]

Doc = dict[str, Any]

#: The display badge (catalog and stamped provenance) and the stamped truck-sample scope.
GROUP_BADGE = "per group"
_SAMPLE_SCOPE = "per_group"

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
    badge=GROUP_BADGE,
    badge_tooltip="Each cooperation group is decided on its own; a decision sees only that group’s trucks "
                  "and orders.",
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
        """Validate the active arm (message 14); at the top declare its pairs, stamp and run-name segment.

        Provenance: the ``per group`` badge, the per-group sample scope and the
        candidate knobs (read here as well; the inner's own bind reads them too).
        """
        cooperation.validate_arm(world)
        arm = cooperation.resolve_arm(world)
        provenance = node_provenance(world, badge=GROUP_BADGE, sample_scope=_SAMPLE_SCOPE)
        if not top:
            return Declaration(provenance=provenance)
        return Declaration(cross_company=cooperation.directed_pairs(arm), provenance=provenance,
                           **cooperation.arm_declaration_fields(arm))

    def audit_scopes(self, scope_log: Any, tick: Any) -> list[str]:
        """The battery's scope audit: every sub-tick lies inside ONE cooperation group.

        A haulier of a multi-company component of the active arm belongs to that
        component's group, any other haulier to its own; a document without a haulier
        belongs to none, so a scope holding one is a problem too.
        """
        arm = cooperation.resolve_arm(tick.world)
        component_of = arm.component_of if arm is not None else {}
        truck_h, order_h = _haulier_index(tick)

        def group(hid: Any) -> Any:
            if hid is None:
                return None
            return ("component", component_of[hid]) if hid in component_of else ("haulier", hid)

        problems = []
        for entry in scope_log:
            keys = {group(truck_h.get(t)) for t in entry["trucks"]} | {group(order_h.get(o)) for o in entry["orders"]}
            if len(keys) > 1 or None in keys:
                problems.append(f"scope {entry['key']} spans cooperation groups {sorted(map(str, keys))}")
        return problems

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


def _haulier_index(tick: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """``({truck id: haulier}, {order id: haulier})`` of the tick (``constraints.haulier_of``)."""
    return ({str(t.get("_id")): assign_constraints.haulier_of(t) for t in tick.raw_trucks},
            {str(o.get("_id")): assign_constraints.haulier_of(o) for o in tick.raw_orders})


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
