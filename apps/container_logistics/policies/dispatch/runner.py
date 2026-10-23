"""``DispatchRunner``: build the tick, solve it, check it, commit the pairs, record stats.

The single path from the bound planner selection to committed ``(truck, order)``
pairs (plan ``plan_contract_final.md`` §2.7), identical for every solver:

1. :meth:`DispatchRunner.tick` builds the whole tick (``tick.Tick.top``) from the
   candidates, the unassigned orders and the assignment profile (the run world). The
   bound tree is re-bound first if a world key its ``bind`` read changed.
2. :meth:`DispatchRunner.run` calls ``result = top.solve_tick(tick, params)``, through
   ``tick.execute`` iff the top's registration offloads ``self`` (a node that offloads
   its ``children`` offloads each ``tick.call`` instead; ``solve_in_thread`` thus
   reaches every solver exactly once per decision). A ``DispatchContractViolation``
   raised inside is a violation; any other exception propagates unchanged (a crashing
   top-level solver aborts the tick, as the partitioned path always did).
3. ``invariants.check_result`` against the tick, with the verified-pair-aware check
   (a pair a sub-tick already proved re-checks only ownership-or-declared), the
   framework's own index; then every truck of the tick and every committed order must
   be unmodified since the tick was built (the share accounting reads all the trucks),
   and the notes must be wire-safe (``invariants.check_wire_notes``: no undeclared
   framework wire field, plain JSON).
   Any problem: log ERROR (message 18), count, commit NOTHING this tick. Nothing here
   is read back from the tick's solver-visible attributes (review H2, L3).
4. Map the pairs back to the raw documents (first document per id) in result order.
   Returns :class:`TickOutcome` ``(pairs, result)``.

Stats (``calls``, ``violations``, ``total_ms``, ``max_ms``; ``crashes`` once a solver
noted an incident) are in memory only and shared with every sub-tick.
:func:`build_runner_for_policy` builds a runner from a registered name alone (the
battery, bench and capture replay share it); :func:`build_runner_for_app` fits the
agent's built instance.

Capture: the hook is called around the solve (``before_solve(tick)``, then
``capture(tick=, result=, problems=)`` with the raw result, before the check), for the
whole tick and every sub-tick (``Tick.call``); the recorder keeps whole ticks only.

Framework side of the boundary: the module names no solver, no cooperation concept
and no candidate kind. Not a leaf module (plan §2.7): ``assignment/app.py`` imports
:func:`build_runner_for_app` locally, inside its one accessor.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from apps.container_logistics.policies.compat.measurement import offline_measurement_dials
from apps.container_logistics.policies.compat.wire import share_tag_format_for
from apps.container_logistics.policies.framework.errors import DispatchContractViolation, PolicyTimingError
from apps.container_logistics.policies.framework.slots import SLOTS

from .bind import BoundTree, RunWorld, bind_tree
from .invariants import check_result, check_wire_notes
from .result import Result
from .tick import DispatchClock, Measure, Tick, direct_execute

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchRunner",
    "TickOutcome",
    "build_runner_for_app",
    "build_runner_for_policy",
    "EXECUTABLE_TIMINGS",
]

# Derived, never restated: the slot table, the catalog and this runner change together
# (review F13). Only ``online`` is declared (plan G13).
EXECUTABLE_TIMINGS: tuple[str, ...] = SLOTS["planner"].executable_timings

Pair = tuple[dict[str, Any], dict[str, Any]]


@dataclass(frozen=True)
class TickOutcome:
    """What one tick committed: ``pairs`` (raw documents, commit order) and the checked ``result``.

    ``result`` is ``None`` when the tick's result was discarded (message 18).
    """

    pairs: list[Pair]
    result: Result | None = None

    @property
    def solver_notes(self) -> Mapping[tuple[str, str], Mapping[str, Any]]:
        return self.result.solver_notes if self.result is not None else {}

    @property
    def tick_notes(self) -> Mapping[str, Any]:
        return self.result.tick_notes if self.result is not None else {}


class DispatchRunner:
    """Runs one bound planner selection (a ``bind.BoundTree``) for every tick.

    ``capture`` is the fixture hook (``before_solve(tick)`` then ``capture(tick=,
    result=, problems=)``); ``None`` costs one ``if`` per solve. ``wire_fields`` are the
    payload fields the CALLER publishes from its own accounting (``AssignmentApp``: the
    share-tag keyword set): a note key naming one that the top did not declare in
    ``Declaration.wire_note_keys`` is a violation (review L3). Raises
    ``PolicyTimingError`` for a timing it cannot execute.
    """

    def __init__(self, tree: BoundTree, *, capture: Callable[..., None] | None = None,
                 profile: Mapping[str, Any] | None = None, wire_fields: Iterable[str] = ()) -> None:
        resolved = tree.resolved
        if resolved.timing not in EXECUTABLE_TIMINGS:
            raise PolicyTimingError(
                f"planner timing {resolved.timing!r} for {resolved.name} is not executable; "
                f"executable timings: {list(EXECUTABLE_TIMINGS)}. Use timing {EXECUTABLE_TIMINGS[0]!r}."
            )
        self.tree = tree
        self.wire_fields = frozenset(wire_fields)
        self._bound_digest = tree.digest(profile)
        self._capture = capture
        self.stats: dict[str, Any] = {"calls": 0, "violations": 0, "total_ms": 0.0, "max_ms": 0.0}

    @property
    def resolved(self) -> Any:
        """The bound top's ``ResolvedDispatch`` tree (name, version, label, params, ...)."""
        return self.tree.resolved

    @property
    def policy(self) -> Any:
        """The top-level built instance (the object the agent's injections reach)."""
        return self.tree.top.instance

    @property
    def tag_format(self) -> str:
        """The share-tag format the accounting renders this run's tags in (a compat name rule)."""
        resolved = self.tree.resolved
        return share_tag_format_for(resolved.name, registered=resolved.source != "unregistered")

    def _tree_for(self, profile: Mapping[str, Any] | None) -> BoundTree:
        """The bound tree for ``profile``: re-bound (same instances) when a key its binds read changed."""
        digest = self.tree.digest(profile)
        if digest != self._bound_digest:
            self.tree = bind_tree(self.tree.resolved, self.tree.top.instance, profile)
            self._bound_digest = self.tree.digest(profile)
        return self.tree

    def tick(
        self,
        *,
        candidates: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        profile: Mapping[str, Any] | None,
        clock: DispatchClock,
        seed: int,
        execute: Callable[..., Any] = direct_execute,
        dials: Mapping[str, Any] | None = None,
        scope_log: list[dict[str, Any]] | None = None,
    ) -> Tick:
        """The whole tick for this runner's selection under assignment profile ``profile``.

        ``dials`` are the measurement's accounting dials (live: ``AssignmentApp._solver_params``);
        ``None`` means the offline rule ``compat.measurement.offline_measurement_dials``.
        """
        tree = self._tree_for(profile)
        prof = profile or {}
        measure = Measure(offline_measurement_dials(tree.resolved) if dials is None else dials)
        return Tick.top(
            tree=tree, trucks=candidates, orders=orders, world=RunWorld.from_profile(prof),
            max_travel_time_pickup=prof.get("max_travel_time_pickup"), clock=clock, seed=seed,
            measure=measure, execute=execute, stats=self.stats, scope_log=scope_log, capture=self._capture,
        )

    def solve_unchecked(self, tick: Tick) -> Any:
        """The top's raw ``solve_tick`` output for ``tick`` (no capture, no check, no offload)."""
        top = tick._shared.tree.top
        return top.solver.solve_tick(tick, top.params)

    def run(self, tick: Tick) -> TickOutcome:
        """Solve ``tick`` and return what to commit (no pairs on a violation). Whole ticks only."""
        if tick.depth != 0:
            raise ValueError(f"DispatchRunner.run takes a whole tick, not sub-tick {tick.scope!r}")
        capture = self._capture
        if capture is not None:
            before = getattr(capture, "before_solve", None)
            if before is not None:
                before(tick)
        result, problems = self._solve(tick)
        if capture is not None:
            capture(tick=tick, result=result, problems=list(problems))
        shared = tick._shared
        if not problems:
            problems = check_result(result, tick, permitted=tick._top_check(), index=shared.index)
        if not problems:
            # One pass over every truck (the accounting reads them all) and the committed
            # orders: a pair judged above on a modified document is refused here.
            orders = shared.index[1]
            changed = shared.changed(orders[oid] for _tid, oid in result.pairs)
            if changed:
                shown = ", ".join(repr(i) for i in changed[:8]) + (" ..." if len(changed) > 8 else "")
                problems = [f"document(s) {shown} were modified during the solve (documents are read-only)"]
        if not problems:
            declared = getattr(shared.tree.declaration, "wire_note_keys", frozenset())
            problems = check_wire_notes(result, self.wire_fields - declared)
        if problems:
            self._report_violation(tick, problems)
            return TickOutcome(pairs=[], result=None)
        if result.tick_notes:
            logger.debug("planner %s tick notes: %r", self.resolved.label, sorted(result.tick_notes))
        return TickOutcome(pairs=tick.docs(result), result=result)  # the framework's own index

    def _solve(self, tick: Tick) -> tuple[Any, list[str]]:
        top = tick._shared.tree.top
        start = time.perf_counter()
        try:
            if top.offload == "self":
                # ONE decision: offload it like a sub-tick (solve_in_thread; review C3 #5).
                return tick.execute(top.solver.solve_tick, tick, top.params), []
            # The node offloads its own child calls (tick.call); wrapping it too would
            # run their execute inside a worker thread.
            return top.solver.solve_tick(tick, top.params), []
        except DispatchContractViolation as exc:
            return None, [str(exc)]
        finally:
            self._record_time((time.perf_counter() - start) * 1000.0)

    def _record_time(self, ms: float) -> None:
        self.stats["calls"] += 1
        self.stats["total_ms"] += ms
        self.stats["max_ms"] = max(self.stats["max_ms"], ms)

    def _report_violation(self, tick: Tick, problems: list[str]) -> None:
        self.stats["violations"] += 1
        logger.error(
            "PLANNER POLICY CONTRACT VIOLATION: %s v%s at step %d returned an invalid tick result: "
            "%s. The result was DISCARDED (nothing committed this tick). This is a policy bug: run "
            "`openride policy validate planner %s`.",
            self.resolved.label, self.resolved.version, tick.clock.time_step,
            "; ".join(problems), self.resolved.name,
        )


def build_runner_for_app(policy: Any, prof: Mapping[str, Any], *, wire_fields: Iterable[str] = ()) -> DispatchRunner:
    """The runner ``AssignmentApp`` uses for its built ``policy`` under assignment profile ``prof``.

    ``policy`` is fitted into the profile's selection tree (``bind.bind_profile``:
    reused as the top, or wrapped as the innermost leaf) and bound against ``prof``.
    Raises ``PolicyError`` when it fits neither, or a declaration is refused.
    """
    # Local imports: capture and bind_profile resolve through the registry, which
    # discovers assignment.solver; keep that off this module's import path.
    from .bind import bind_profile
    from .capture import CaptureRecorder, CaptureSettings

    tree = bind_profile(prof, policy)
    settings = CaptureSettings.from_profile(prof)
    # Capture records whole ticks of the bound selection (format /3).
    capture = None if settings is None else CaptureRecorder(
        settings, resolved=tree.resolved, rebate_aware=CaptureSettings.rebate_aware_of(prof))
    return DispatchRunner(tree, capture=capture, profile=prof, wire_fields=wire_fields)


def build_runner_for_policy(
    name: str,
    params: Mapping[str, Any] | None = None,
    *,
    capture: Callable[..., None] | None = None,
    profile: Mapping[str, Any] | None = None,
) -> DispatchRunner:
    """A fresh instance of registered planner policy ``name`` (a whole tree) wrapped in a runner.

    ``params`` are validated by the policy's ``Params`` (``None``: all defaults; refs
    resolved recursively). The tree is bound against ``profile`` (default: an empty
    world) and re-bound by :meth:`DispatchRunner.tick` for each tick's world. Raises
    ``UnknownPolicyError`` / ``PolicyParamsError``.
    """
    from apps.container_logistics.policies.framework.registry import get_policy_spec

    from .profile import build_policy_tree, dispatch_node

    spec = get_policy_spec("planner", name)
    validated = spec.params_cls.from_mapping(params, where=f"planner params for {name}")
    node = dispatch_node(spec.name, validated, timing="online", source="policies")
    return DispatchRunner(bind_tree(node, build_policy_tree(node), profile), capture=capture, profile=profile)
