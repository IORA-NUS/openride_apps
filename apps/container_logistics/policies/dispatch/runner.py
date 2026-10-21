"""``DispatchRunner``: decide, check, commit heads, record stats, call the capture hook.

The single path from a dispatch policy to committed ``(truck, order)`` pairs on both
topologies (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P1.8):

1. ``plan = policy.decide(ctx)``. A ``DispatchContractViolation`` raised inside is a
   violation; any other exception propagates unchanged (today's behaviour).
2. ``invariants.check_plan``. Any problem: log ERROR, count, commit NOTHING for the
   scope this tick. Violations are not stamped on the run this round (plan §1.2
   item 7); the log line goes to celery's journal, not ``sim.log``.
3. Map heads back to the raw documents in chain-insertion order. ``online`` timing
   commits heads only.

Stats (``calls``, ``violations``, ``total_ms``, ``max_ms``) are in memory only.
Framework side of the boundary. Not a leaf module (plan §2.7): ``assignment/app.py``
imports :func:`build_runner_for_app` locally, inside its one accessor.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from apps.container_logistics.policies.framework.errors import (
    DispatchContractViolation,
    PolicyError,
    PolicyTimingError,
)

from . import scopes
from .allowed import CompanyRule
from .base import DispatchPolicy
from .context import DispatchContext
from .helpers.candidates import CANDIDATE_KINDS, CandidateSettings
from .invariants import check_plan
from .plan import Plan
from .resolved import ResolvedDispatch

logger = logging.getLogger(__name__)

__all__ = ["DispatchRunner", "build_runner_for_app", "EXECUTABLE_TIMINGS"]

EXECUTABLE_TIMINGS: tuple[str, ...] = ("online",)

Pair = tuple[dict[str, Any], dict[str, Any]]


class DispatchRunner:
    """Runs one resolved dispatch policy for every scope of every tick.

    ``capture`` is the P3 fixture hook: ``capture(ctx=..., plan=..., problems=...)``,
    called after the check when not ``None``. Raises ``PolicyTimingError`` for a
    timing it cannot execute and ``PolicyError`` for an unknown candidate kind.
    """

    def __init__(
        self,
        *,
        policy: DispatchPolicy,
        resolved: ResolvedDispatch,
        capture: Callable[..., None] | None = None,
    ) -> None:
        if resolved.timing not in EXECUTABLE_TIMINGS:
            raise PolicyTimingError(
                f"dispatch timing {resolved.timing!r} for {resolved.name} is not executable; "
                f"executable timings: {list(EXECUTABLE_TIMINGS)}. Use timing 'online'."
            )
        if resolved.candidate_kind not in CANDIDATE_KINDS:
            raise PolicyError(
                f"dispatch candidate kind {resolved.candidate_kind!r} for {resolved.name} is not "
                f"one of {list(CANDIDATE_KINDS)}."
            )
        self.policy = policy
        self.resolved = resolved
        self.candidate_kind = resolved.candidate_kind
        self._capture = capture
        self.stats: dict[str, Any] = {"calls": 0, "violations": 0, "total_ms": 0.0, "max_ms": 0.0}

    @staticmethod
    def candidate_settings(prof: dict[str, Any]) -> CandidateSettings:
        """The profile's spatial/sampling knobs (``CandidateSettings.from_profile``).

        Exposed here so ``AssignmentApp`` reaches the dispatch package through the
        runner it already holds instead of another local import.
        """
        return CandidateSettings.from_profile(prof)

    @staticmethod
    def company_rule(haulier_id: str, max_travel_time_pickup: Any) -> CompanyRule:
        """The pooled feasibility rule for ``haulier_id`` (``pooled_planner`` imports no policies)."""
        return CompanyRule(haulier_id=haulier_id, max_travel_time_pickup=max_travel_time_pickup)

    def partition_context(self, **kw: Any) -> DispatchContext:
        """``scopes.partition_context`` with this runner's policy, params and kind."""
        return scopes.partition_context(
            policy=self.policy, params=self.resolved.params, candidate_kind=self.candidate_kind, **kw
        )

    def company_context(self, **kw: Any) -> DispatchContext:
        """``scopes.company_context`` with this runner's policy, params and kind."""
        return scopes.company_context(
            policy=self.policy, params=self.resolved.params, candidate_kind=self.candidate_kind, **kw
        )

    def run(self, ctx: DispatchContext) -> list[Pair]:
        """Decide for ``ctx`` and return the pairs to commit (``[]`` on a violation)."""
        plan, problems = self._decide(ctx)
        if not problems:
            problems = check_plan(plan, ctx)
        if self._capture is not None:
            self._capture(ctx=ctx, plan=plan, problems=problems)
        if problems:
            self._report_violation(ctx, problems)
            return []
        if plan.notes:
            logger.debug("dispatch %s notes in scope %s: %r", self.resolved.name, ctx.scope.key, dict(plan.notes))
        return self._commit_heads(plan, ctx)

    def _decide(self, ctx: DispatchContext) -> tuple[Any, list[str]]:
        start = time.perf_counter()
        try:
            return self.policy.decide(ctx), []
        except DispatchContractViolation as exc:
            return None, [str(exc)]
        finally:
            self._record_time((time.perf_counter() - start) * 1000.0)

    def _record_time(self, ms: float) -> None:
        self.stats["calls"] += 1
        self.stats["total_ms"] += ms
        self.stats["max_ms"] = max(self.stats["max_ms"], ms)

    def _report_violation(self, ctx: DispatchContext, problems: list[str]) -> None:
        self.stats["violations"] += 1
        logger.error(
            "DISPATCH POLICY CONTRACT VIOLATION: %s v%s in scope %s at step %d returned an "
            "invalid plan: %s. The plan was DISCARDED (nothing committed for this scope this "
            "tick). This is a policy bug: run `openride policy validate dispatch %s`.",
            self.resolved.name,
            self.resolved.version,
            ctx.scope.key,
            ctx.clock.time_step,
            "; ".join(problems),
            self.resolved.name,
        )

    @staticmethod
    def _commit_heads(plan: Plan, ctx: DispatchContext) -> list[Pair]:
        # First doc per id, matching check_plan; ids are unique in valid input
        # (filter_assignable_trucks / the order query), see plan §7 risk 7.
        trucks: dict[str, dict[str, Any]] = {}
        for t in ctx.raw_trucks:
            trucks.setdefault(str(t.get("_id")), t)
        orders: dict[str, dict[str, Any]] = {}
        for o in ctx.raw_orders:
            orders.setdefault(str(o.get("_id")), o)
        return [(trucks[tid], orders[oid]) for tid, oid in plan.heads()]


def build_runner_for_app(policy: DispatchPolicy, prof: dict[str, Any]) -> DispatchRunner:
    """The runner ``AssignmentApp`` uses for ``policy`` under assignment profile ``prof``.

    Raises ``PolicyError`` when the profile resolves dispatch to a different policy
    than ``policy`` (see ``dispatch.profile.resolve_for_instance``).
    """
    # Local import: dispatch/profile.py resolves through the registry, which
    # discovers assignment.solver; keep that off this module's import path.
    from .profile import resolve_for_instance

    return DispatchRunner(policy=policy, resolved=resolve_for_instance(policy, prof))
