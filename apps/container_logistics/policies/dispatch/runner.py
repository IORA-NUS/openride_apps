"""``DispatchRunner``: build the tick's context, decide, check, commit heads, record stats.

The single path from the selected dispatch policy to committed ``(truck, order)``
pairs (round 2, ``docs/plans/policy_slots_round2_whole_tick.md`` §2):

1. :meth:`DispatchRunner.tick_context` builds the whole-tick context
   (``scopes.tick_context``).
2. :meth:`DispatchRunner.run` calls ``plan = policy.decide(ctx)``, through
   ``ctx.execute`` unless the policy is a ``CompositePolicy`` (a decomposition offloads
   its own sub-scopes; ``solve_in_thread`` thus reaches every solver exactly once per
   decision). A ``DispatchContractViolation`` raised inside is a violation; any other
   exception propagates unchanged (a crashing top-level solver aborts the tick, as the
   partitioned path always did).
3. ``invariants.check_plan`` against the context. On the tick scope its feasibility is
   ``world.allowed``, so a cross-company head the structure does not permit is
   discarded whatever solver produced it. Any problem: log ERROR (message 18), count,
   commit NOTHING this tick.
4. Map heads back to the raw documents (first document per id) in chain-insertion
   order: ``online`` timing commits heads only. Returns
   :class:`TickOutcome` ``(pairs, notes)``; ``notes`` is the plan's (``market`` and
   ``pair_notes`` are the reserved keys the framework reads).

A decomposition solver decides its sub-scopes through ``compose.decide_in_scope``
(message 19 per discarded scope). ``run`` also accepts a sub-scope context (the
offline fixtures decide one partition); a violation there is message 19.

Stats (``calls``, ``violations``, ``total_ms``, ``max_ms``; ``crashes`` once a market
company crashed) are in memory only and shared with every scope of the tick.
:func:`build_runner_for_policy` builds a runner from a registered name alone (the
battery, bench and capture replay share it).

Capture (format ``/2``, one file per captured TICK): the hook is called around
``run`` (``before_decide(ctx)``, then ``capture(ctx=, plan=, problems=)`` with the raw
plan, before ``check_plan``). It also rides on the tick context, so
``compose.decide_in_scope`` calls it around every sub-scope decision; the capture
recorder ignores those (a whole tick replays its sub-scopes).

Framework side of the boundary. Not a leaf module (plan §2.7): ``assignment/app.py``
imports :func:`build_runner_for_app` locally, inside its one accessor.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from apps.container_logistics.policies.framework.errors import (
    DispatchContractViolation,
    PolicyError,
    PolicyTimingError,
)
from apps.container_logistics.policies.framework.slots import SLOTS

from . import compose, scopes
from .base import DispatchPolicy
from .composite import CompositePolicy
from .context import DispatchClock, DispatchContext, direct_execute
from .helpers.candidates import CANDIDATE_KINDS, CandidateSettings
from .invariants import check_plan
from .resolved import ResolvedDispatch

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchRunner",
    "TickOutcome",
    "accounting_params_for",
    "build_runner_for_app",
    "build_runner_for_policy",
    "framework_accounting_cost",
    "EXECUTABLE_TIMINGS",
]

# Derived, never restated: when P4 makes 'plan' executable in the slot table, the
# catalog and this runner change together (review F13).
EXECUTABLE_TIMINGS: tuple[str, ...] = SLOTS["planner"].executable_timings

Pair = tuple[dict[str, Any], dict[str, Any]]


@dataclass(frozen=True)
class TickOutcome:
    """What one tick committed: ``pairs`` (heads, raw documents, commit order) and the plan's ``notes``."""

    pairs: list[Pair]
    notes: Mapping[str, Any] = field(default_factory=dict)


class DispatchRunner:
    """Runs one resolved dispatch selection (a tree) for every tick.

    ``policy`` is the built top-level policy and ``resolved`` its ``ResolvedDispatch``
    tree. ``capture`` is the P3 fixture hook: ``capture(ctx=..., plan=...,
    problems=...)``, called after ``decide`` (the tick's in :meth:`run`, and each
    sub-scope's through ``compose.decide_in_scope``) and BEFORE the check
    (``problems`` holds only a violation ``decide`` raised); with ``before_decide(ctx)``
    it is called first, so it can snapshot RNG state without consuming it. ``None``
    costs one ``if`` per scope. Raises ``PolicyTimingError`` for a timing it cannot
    execute and ``PolicyError`` for an unknown candidate kind.
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
                f"planner timing {resolved.timing!r} for {resolved.name} is not executable; "
                f"executable timings: {list(EXECUTABLE_TIMINGS)}. Use timing {EXECUTABLE_TIMINGS[0]!r}."
            )
        if resolved.candidate_kind not in CANDIDATE_KINDS:
            raise PolicyError(
                f"planner candidate kind {resolved.candidate_kind!r} for {resolved.name} is not "
                f"one of {list(CANDIDATE_KINDS)}."
            )
        self.policy = policy
        self._composite = isinstance(policy, CompositePolicy)
        self.resolved = resolved
        self.candidate_kind = resolved.candidate_kind
        self.share_tag_format = resolved.share_tag_format
        self._capture = capture
        self.stats: dict[str, Any] = {"calls": 0, "violations": 0, "total_ms": 0.0, "max_ms": 0.0}

    @staticmethod
    def candidate_settings(prof: Mapping[str, Any]) -> CandidateSettings:
        """The profile's spatial/sampling knobs (``CandidateSettings.from_profile``)."""
        return CandidateSettings.from_profile(prof)

    def tick_context(
        self,
        *,
        candidates: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        world: Any,
        settings: CandidateSettings,
        clock: DispatchClock,
        tick_seed: int,
        execute: Callable[..., Any] = direct_execute,
        scope_log: list[dict[str, Any]] | None = None,
    ) -> DispatchContext:
        """The whole-tick context for this runner's policy (``scopes.tick_context``)."""
        return scopes.tick_context(
            policy=self.policy, params=self.resolved.params, candidate_kind=self.candidate_kind,
            trucks=candidates, orders=orders, world=world, settings=settings, clock=clock,
            tick_seed=tick_seed, execute=execute, stats=self.stats, scope_log=scope_log,
            capture=self._capture,
        )

    def run(self, ctx: DispatchContext) -> TickOutcome:
        """Decide for ``ctx`` and return what to commit (no pairs on a violation)."""
        capture = self._capture
        if capture is not None:
            before = getattr(capture, "before_decide", None)
            if before is not None:
                before(ctx)
        plan, problems = self._decide(ctx)
        if capture is not None:
            capture(ctx=ctx, plan=plan, problems=list(problems))
        if not problems:
            problems = check_plan(plan, ctx)
        if problems:
            self._report_violation(ctx, problems)
            return TickOutcome(pairs=[], notes={})
        if plan.notes:
            logger.debug("dispatch %s notes in scope %s: %r", self.resolved.label, ctx.scope.key,
                         sorted(plan.notes))
        return TickOutcome(pairs=compose.heads_to_pairs(plan, ctx), notes=dict(plan.notes))

    def _decide(self, ctx: DispatchContext) -> tuple[Any, list[str]]:
        start = time.perf_counter()
        try:
            if self._composite:
                # A decomposition owns its sub-scopes' offloading (Partitioned: one
                # ctx.execute per group; PooledMarket: one per company solve, via
                # compose.ScopeDecider.run). Wrapping it too would run the scopes'
                # execute inside a worker thread.
                return self.policy.decide(ctx), []
            # Any other top-level solver is ONE decision: offload it like a group
            # (solve_in_thread; review C3 #5). direct_execute when the flag is off.
            return ctx.execute(self.policy.decide, ctx), []
        except DispatchContractViolation as exc:
            return None, [str(exc)]
        finally:
            self._record_time((time.perf_counter() - start) * 1000.0)

    def _record_time(self, ms: float) -> None:
        self.stats["calls"] += 1
        self.stats["total_ms"] += ms
        self.stats["max_ms"] = max(self.stats["max_ms"], ms)

    def _report_violation(self, ctx: DispatchContext, problems: list[str]) -> None:
        if ctx.scope.kind != "tick":
            compose.report_scope_violation(self.resolved.name, self.resolved.version, ctx, problems,
                                           stats=self.stats)
            return
        self.stats["violations"] += 1
        logger.error(
            "PLANNER POLICY CONTRACT VIOLATION: %s v%s at step %d returned an invalid tick plan: "
            "%s. The plan was DISCARDED (nothing committed this tick). This is a policy bug: run "
            "`openride policy validate planner %s`.",
            self.resolved.label, self.resolved.version, ctx.clock.time_step,
            "; ".join(problems), self.resolved.name,
        )


def build_runner_for_app(policy: DispatchPolicy, prof: Mapping[str, Any]) -> DispatchRunner:
    """The runner ``AssignmentApp`` uses for its built ``policy`` under assignment profile ``prof``.

    ``policy`` is fitted into the profile's selection tree
    (``dispatch.profile.bind_profile_instance``): reused as the top, or wrapped as the
    innermost leaf. Raises ``PolicyError`` when it fits neither.
    """
    # Local import: dispatch/profile.py resolves through the registry, which
    # discovers assignment.solver; keep that off this module's import path.
    from .capture import CaptureRecorder, CaptureSettings
    from .profile import bind_profile_instance

    top, bound = bind_profile_instance(prof, policy)
    settings = CaptureSettings.from_profile(prof)
    # Capture records whole ticks of the bound selection (format /2).
    capture = None if settings is None else CaptureRecorder(
        settings, resolved=bound, rebate_aware=CaptureSettings.rebate_aware_of(prof))
    return DispatchRunner(policy=top, resolved=bound, capture=capture)


def build_runner_for_policy(
    name: str,
    params: Mapping[str, Any] | None = None,
    *,
    capture: Callable[..., None] | None = None,
) -> DispatchRunner:
    """A fresh instance of registered dispatch policy ``name`` (a whole tree) wrapped in a runner.

    ``params`` are validated by the policy's ``Params`` (``None``: all defaults; refs
    resolved recursively). Raises ``UnknownPolicyError`` / ``PolicyParamsError``.
    """
    from apps.container_logistics.policies.framework.registry import get_policy_spec

    from .profile import build_policy_tree, dispatch_node

    spec = get_policy_spec("planner", name)
    validated = spec.params_cls.from_mapping(params, where=f"planner params for {name}")
    node = dispatch_node(spec.name, validated, timing="online", source="policies")
    return DispatchRunner(policy=build_policy_tree(node), resolved=node, capture=capture)


_ACCOUNTING_KEYS = ("dual_cycle_bonus_km", "dual_cycle_radius_km")


def accounting_params_for(selection: Any) -> dict[str, Any]:
    """The accounting dials a selection implies: the F1 one-dial mirror rule, offline.

    ``selection`` is a ``ResolvedDispatch`` tree (each key from the first node,
    pre-order, that declares it; normally the leaf), a ``PolicyParams`` or a plain
    mapping. Keys nobody declares are left out (the constraint defaults apply).
    """
    if isinstance(selection, ResolvedDispatch):
        out: dict[str, Any] = {}
        node: ResolvedDispatch | None = selection
        while node is not None:
            values = node.params.to_dict() if node.params is not None else {}
            for key in _ACCOUNTING_KEYS:
                if key in values and key not in out:
                    out[key] = values[key]
            node = node.inner
        return out
    values = selection.to_dict() if hasattr(selection, "to_dict") else dict(selection or {})
    return {k: values[k] for k in _ACCOUNTING_KEYS if k in values}


def framework_accounting_cost(selection: Any) -> Callable[[dict[str, Any], dict[str, Any]], float]:
    """The accounting cost an offline context uses: ``constraints.assignment_cost``.

    The live sim passes ``AssignmentApp._solver_params``, which the one-dial mirror
    keeps equal to the selection's dual-cycle params; offline they come from
    :func:`accounting_params_for` and the constraint defaults for the rest.
    """
    from apps.container_logistics.assignment import constraints as assign_constraints

    accounting = accounting_params_for(selection)

    def cost(t: dict[str, Any], o: dict[str, Any]) -> float:
        return assign_constraints.assignment_cost(t, o, accounting)

    return cost
