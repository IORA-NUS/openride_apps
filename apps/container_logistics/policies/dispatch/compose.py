"""Reusable decomposition helpers: split a tick into scopes, decide each, report.

Single responsibility: the building blocks a decomposition solver (``Partitioned``,
``PooledMarket``, a third-party composite) uses to delegate parts of a whole-tick
context to a per-scope solver (plan ``docs/plans/policy_slots_round2_whole_tick.md``
§2.3). Any solver may call them; the framework does not require them.

- :func:`cooperation_groups` -- the tick's cooperation groups (a multi-company
  component, else one haulier), a verbatim move of the pre-v2 partitioned bucketing
  of ``AssignmentApp.assign`` (round-2 S2).
- :func:`decide_in_scope` -- decide one sub-scope with a leaf and check the plan
  against that scope; a violation is logged (message 19), counted and dropped.
- :func:`heads_to_pairs` -- a plan's heads as the scope's raw ``(truck, order)``
  documents (first document per id).
- :class:`ScopeDecider` -- the adapter ``assignment.pooled_planner`` calls to decide
  one company (it imports nothing from ``policies``).

Framework side of the boundary (scoping, checking, reporting); the decisions stay the
solver's. Not a leaf module: it builds contexts through ``scopes`` (plan §2.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.framework.errors import DispatchContractViolation

from . import scopes
from .allowed import CompanyRule
from .context import DispatchContext
from .invariants import check_plan
from .plan import Plan

logger = logging.getLogger(__name__)

__all__ = [
    "GroupScope",
    "ScopeDecider",
    "cooperation_groups",
    "decide_in_scope",
    "heads_to_pairs",
    "report_scope_violation",
]

Doc = dict[str, Any]
Pair = tuple[Doc, Doc]


@dataclass(frozen=True)
class GroupScope:
    """One cooperation group of a tick: its partition key and its trucks and orders, in input order."""

    key: tuple[str, Any]
    trucks: tuple[Doc, ...]
    orders: tuple[Doc, ...]


def cooperation_groups(ctx: DispatchContext) -> list[GroupScope]:
    """The groups of ``ctx`` that have both orders and trucks, in first-seen order of their orders.

    A haulier in a multi-company component of the active structure
    (``ctx.world.component_of``) is keyed ``("component", idx)``; any other haulier
    ``("haulier", id)``; a document without a haulier never matches (fail-closed) and
    is left out. Pure (no RNG).
    """
    component_of = ctx.world.component_of

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
    for t in ctx.raw_trucks:
        truck_buckets.setdefault(_partition_key(assign_constraints.haulier_of(t)), []).append(t)
    order_buckets: dict[Any, list[Doc]] = {}
    for o in ctx.raw_orders:
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


def heads_to_pairs(plan: Plan, ctx: DispatchContext) -> list[Pair]:
    """``plan``'s heads as ``ctx``'s raw documents, in chain-insertion order (first doc per id)."""
    trucks, orders = ctx.id_index
    return [(trucks[tid], orders[oid]) for tid, oid in plan.heads()]


def report_scope_violation(
    name: str, version: str, ctx: DispatchContext, problems: list[str], *, stats: dict[str, Any]
) -> None:
    """Log message 19 for a discarded sub-scope plan and count it in ``stats["violations"]``."""
    stats["violations"] = stats.get("violations", 0) + 1
    logger.error(
        "DISPATCH POLICY CONTRACT VIOLATION: inner %s v%s in scope %s at step %d returned an "
        "invalid plan: %s. The scope's plan was DISCARDED (nothing committed for this scope "
        "this tick). This is a policy bug: run `openride policy validate dispatch %s`.",
        name, version, ctx.scope.key, ctx.clock.time_step, "; ".join(problems), name,
    )


def decide_in_scope(policy: Any, node: Any, sub_ctx: DispatchContext) -> Plan | None:
    """``policy.decide(sub_ctx)`` checked against ``sub_ctx``; ``None`` when the plan is discarded.

    ``node`` is ``policy``'s ``ResolvedDispatch`` (its name and version name the
    culprit). A ``DispatchContractViolation`` raised inside ``decide`` and any
    ``check_plan`` problem are reported (message 19) and counted in
    ``sub_ctx.stats["violations"]``; any other exception propagates. The context's
    capture hook, when set, sees the raw plan before the check.
    """
    capture = sub_ctx.capture
    if capture is not None:
        before = getattr(capture, "before_decide", None)
        if before is not None:
            before(sub_ctx)
    try:
        plan, problems = policy.decide(sub_ctx), []
    except DispatchContractViolation as exc:
        plan, problems = None, [str(exc)]
    if capture is not None:
        capture(ctx=sub_ctx, plan=plan, problems=list(problems))
    if not problems:
        problems = check_plan(plan, sub_ctx)
    if problems:
        report_scope_violation(node.name, node.version, sub_ctx, problems, stats=sub_ctx.stats)
        return None
    return plan


class ScopeDecider:
    """Decides one pooled-market company per call: the object ``PooledPlannerContext.dispatch`` holds.

    ``policy`` is the inner (leaf) solver, ``node`` its ``ResolvedDispatch``,
    ``parent`` the market's context (every company context inherits from it).
    """

    def __init__(self, policy: Any, node: Any, parent: DispatchContext) -> None:
        self.policy = policy
        self.node = node
        self.parent = parent
        self.candidate_kind = node.candidate_kind

    @property
    def stats(self) -> dict[str, Any]:
        """The runner's stats dict (shared with the parent context)."""
        return self.parent.stats

    @staticmethod
    def company_rule(haulier_id: str, max_travel_time_pickup: Any) -> CompanyRule:
        """The pooled feasibility rule for ``haulier_id``."""
        return CompanyRule(haulier_id=haulier_id, max_travel_time_pickup=max_travel_time_pickup)

    def company_context(self, **kw: Any) -> DispatchContext:
        """``scopes.company_context`` for the inner solver, inheriting from the market's context."""
        return scopes.company_context(
            policy=self.policy, params=self.node.params, candidate_kind=self.candidate_kind,
            parent=self.parent, **kw,
        )

    def run(self, company_ctx: DispatchContext) -> list[Pair]:
        """The pairs the inner solver commits for ``company_ctx`` (``[]`` when its plan is discarded)."""
        plan = decide_in_scope(self.policy, self.node, company_ctx)
        return [] if plan is None else heads_to_pairs(plan, company_ctx)

    def report_crash(self, *, haulier_id: str, round: int) -> None:
        """Log message 20 with the traceback (call it inside ``except``) and count it."""
        self.stats["crashes"] = self.stats.get("crashes", 0) + 1
        logger.exception(
            "DISPATCH POLICY CRASHED: inner %s raised for company %r in round %d at step %d; "
            "that company commits nothing this round (pooled-market behaviour, unchanged). "
            "Traceback follows. (celery journal, not sim.log)",
            self.node.name, haulier_id, round, self.parent.clock.time_step,
        )
