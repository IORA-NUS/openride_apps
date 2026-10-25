"""``NetworkFlowDay``: a day plan per truck (network flow), dispatched head by head.

Single responsibility: the planner-slot wrapper of the day-plan solver (design page
§6 / §6a / §7, plan ``PLAN_N1_N3.md`` §4 as amended by ``PLAN_ADDENDUM.md``). It owns
no model and no transport of its own; it adapts ticks to the day-plan helpers in
``solver_lib.day_plan`` and turns their answers into the contract's ``Result``:

- ``instance.build_inputs`` -- a planning tick's documents (+ the store's statuses) ->
  one JSON-able solve input;
- ``runner.solve`` -- the solve, in a CHILD process of the agent's own interpreter
  (``sys.executable``: addendum M2), bounded in wall time and memory;
- ``store.PlanStore`` -- the current itineraries in memory, written through to Redis db 2
  (``RedisPlanStore``) when the tick names a run, else a private in-memory record
  (``tick.run_id == ""``: battery, bench, golden, fixtures);
- ``stats.DayPlanStats`` -- the run-stats aggregator of ``run_config.meta.day_plan``.

**Planning tick** (``tick.kind == "plan"``; the framework sends one at each day start and
every ``nf_replan_every_steps`` steps, holding the step meanwhile). Statuses the tick's
``lookup`` already settles are folded in first (done / cancelled / taken). The instance
is built from every online truck (``raw_trucks``), the busy trucks' open hauls
(``engaged``) and the window's orders incl. foresight (``raw_orders``); an unchanged
instance (``PlanInputs.hash`` equal to the last solved one) is NOT re-solved (noted
``skipped``). A solved plan becomes a new store version ``d{n}v{k}`` (the store keeps the
locked head -- dispatched / done jobs -- of every itinerary); any failure (the solve:
``runner.SolveError`` -- timeout, crash, out of memory, poor incumbent, invalid output;
or the store refusing the version) keeps the PREVIOUS version and is noted. A planning
tick never commits a pair (R6); it prefetches every itinerary head plus the oldest
unresolved dispatched job of each truck (at most 2 ids per truck, F3).

**Dispatch tick** (every other tick). For each FREE truck of the tick, in id order, its
itinerary is walked (addendum M4: the head rules by order state, read from
``tick.lookup`` -- the documents the previous result prefetched, each with
``claimed_by``, the truck of its open haul):

========================================  ============================================
order state of the job (``lookup``)       head rule
========================================  ============================================
assigned / *_in_progress / in_transit /   our truck: it is ours already -> ``dispatched``
completed                                 (``done`` when completed), next job;
                                          another truck -> ``failed:taken``, next job
cancelled                                 ``failed:cancelled``, next job
created                                   not released yet: the truck WAITS
unassigned, claimed by another truck      ``failed:taken``, next job
unassigned, claimed by our truck          ``dispatched`` (lifecycle lag), next job
unassigned, free, in the tick             ``permitted`` -> PAIR; else ``failed:infeasible``,
                                          next job
unassigned, free, not in the tick         claimed (the app leaves claimed orders out)
                                          -> ``failed:taken``, next job
not looked up, in the tick                ``permitted`` -> PAIR; else ``failed:infeasible``
not looked up, not in the tick            the truck WAITS (the next tick looks it up)
========================================  ============================================

A job dispatched to a truck that is FREE again while its order is still ``unassigned``
and unclaimed was refused: it is paired once more (retry once), then
``failed:refused``. An empty itinerary means the truck sleeps until the next plan; it is
never refilled by an online matcher. Every status change is written through
(``PlanStore.set_statuses``) and reported in the tick notes (``stats.events_note``).

**Failure policy.** A plan-store error never fails a tick: an unreachable record keeps
the change in memory (the store resynchronises on its next write) and a refused write
keeps the previous version; both are noted ``store_failed``. After an agent restart the
first tick with a run id reloads the record (``PlanStore.load_current``) and folds a
``rebuild`` note into the run stats, so the dispatch continues from the stored plan.

**Determinism.** Every list is in id order (trucks, jobs, events, prefetch); the solve is
exact min-cost flow by default (``deterministic`` false only when a MIP hit its time
limit); nothing draws randomness.

Solver side of the boundary: the framework still checks every pair (``permitted``,
``check_result``), commits it, does the accounting and drives the run stats.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.bind import Declaration
from apps.container_logistics.policies.dispatch.result import PREFETCH_MAX, Result
from apps.container_logistics.policies.framework import PolicyParams, param, policy
from apps.container_logistics.policies.framework.errors import PolicyParamsError
from apps.container_logistics.policies.solver_lib import cooperation
from apps.container_logistics.policies.solver_lib.day_plan import instance as plan_instance
from apps.container_logistics.policies.solver_lib.day_plan import runner as plan_runner
from apps.container_logistics.policies.solver_lib.day_plan import stats as plan_stats
from apps.container_logistics.policies.solver_lib.day_plan.store import (
    MemoryKV,
    MemoryPlanStore,
    PlanStore,
    PlanStoreError,
    PlanStoreUnavailable,
    RedisPlanStore,
)
from apps.container_logistics.policies.solver_lib.day_plan.time_model import TimeModel

logger = logging.getLogger(__name__)

__all__ = ["BACKENDS", "BADGE", "NetworkFlowDay", "default_store_factory", "prefetch_ids"]

#: The display badge (catalog and stamped provenance): one planner over the whole tick.
BADGE = "central"
#: ``nf_backend`` values (validated in ``Params``; deliberately NOT a ``choices`` param, so
#: the battery does not run one arm per backend: addendum M7).
BACKENDS = ("auto", "mincostflow", "mip", "greedy")
#: Backends that need OR-Tools in the child (the agent's own interpreter, addendum M2).
_ORTOOLS_BACKENDS = frozenset({"auto", "mincostflow", "mip"})
#: Seconds of the declared hold kept free for the agent's own work around the child
#: (building the instance, the store write): the child is killed this much before it.
_HOLD_MARGIN_S = 5.0
#: Redis client timeouts (critique minor 7): a wedged Redis must not block the hold.
_REDIS_TIMEOUT_S = 2.0
#: Order states in which a job is somebody's haul (critique M4).
_HAULED_STATES = frozenset({"assigned", "pickup_in_progress", "in_transit", "dropoff_in_progress", "completed"})
_CLAIMED_BY = "claimed_by"
_MAX_RETRIES = 1
_WARNED_FALLBACK = False


# ------------------------------------------------------------------------------- helpers


@lru_cache(maxsize=1)
def _time_model() -> TimeModel:
    """The shipped time model (a generated data module: importing it reads no file)."""
    return TimeModel.default()


def _ortools_importable() -> bool:
    return importlib.util.find_spec("ortools") is not None


def default_store_factory(run_id: str) -> PlanStore:
    """The record of ``run_id``: Redis (``OPENRIDE_PLAN_REDIS_URL``, db 2 by default) for a
    live run, a PRIVATE in-memory record offline (``run_id == ""``), so two offline
    decisions in one process never share a plan."""
    if run_id:
        return RedisPlanStore(run_id, socket_timeout=_REDIS_TIMEOUT_S)
    return MemoryPlanStore("local", kv=MemoryKV())


def _id(doc: Mapping[str, Any]) -> str:
    return str(doc.get("_id"))


def _truck_of(doc: Mapping[str, Any]) -> str | None:
    t = doc.get("truck")
    if isinstance(t, Mapping):
        t = t.get("_id")
    return None if t in (None, "") else str(t)


def prefetch_ids(store: PlanStore, trucks: Iterable[str], status_of: Callable[[str], Any]) -> tuple[str, ...]:
    """At most 2 ids per truck (in truck id order): its oldest unresolved ``dispatched`` job,
    then its head (first ``planned`` job); capped at ``PREFETCH_MAX``."""
    out: dict[str, None] = {}
    for tid in sorted(trucks):
        dispatched = head = None
        for j in store.itinerary(tid):
            rec = status_of(j) or {}
            s = rec.get("status")
            if s == "dispatched" and dispatched is None and rec.get("truck") in (None, tid):
                dispatched = j
            elif s == "planned" and head is None:
                head = j
            if head is not None and dispatched is not None:
                break
        for j in (dispatched, head):
            if j is not None and len(out) < PREFETCH_MAX:
                out.setdefault(j, None)
    return tuple(out)


class _RunState:
    """Everything the wrapper remembers for one run (one instance serves one run at a time)."""

    def __init__(self, run_id: str, store: PlanStore) -> None:
        self.run_id = run_id
        self.store = store
        self.loaded = not run_id          # offline: nothing to reload
        self.last_hash: str | None = None
        self.step_seconds: int | None = None
        self.retries: dict[str, int] = {}
        self.dispatched_at: dict[str, int] = {}  # job -> time step it was (last) paired
        self.trucks: tuple[str, ...] = ()        # trucks of the current version
        self.overlay: dict[str, dict[str, Any]] = {}  # statuses the store refused to take
        self.pending: dict[str, Any] = {}        # notes for the next tick (rebuild, store errors)


# ---------------------------------------------------------------------------------- policy


@policy(
    slot="planner",
    name="NetworkFlowDay",
    version="1",
    description="Day plan per truck (network flow over the day's orders, ORACLE foresight), "
                "re-planned every few hours and dispatched head by head.",
    badge=BADGE,
    badge_tooltip="Plans every truck's whole day at day start (knowing the day's orders in advance) "
                  "and hands each free truck the next job of its plan.",
    requires=("ortools",),
    top_only=True,
)
class NetworkFlowDay(DispatchPolicy):
    """Plan a day of jobs per truck on planning ticks; dispatch each truck's head on dispatch ticks."""

    @dataclass(frozen=True)
    class Params(PolicyParams):
        nf_backend: str = param(
            "auto",
            help="Solve backend: 'auto' (exact min-cost flow where the cooperation group allows it, "
                 "else the MIP with a quality guard), 'mincostflow' (same), 'mip' (the MIP everywhere), "
                 "'greedy' (no OR-Tools; a heuristic).")
        nf_time_limit_s: float = param(20.0, gt=0, le=300,
                                       help="Time limit of each MIP component solve (s); at most nf_max_hold_s.")
        nf_max_hold_s: int = param(30, ge=6, le=300,
                                   help="Declared planning hold (max_hold_seconds): the sim waits at most this "
                                        "long for a plan; the solve child is killed 5 s before it.")
        nf_replan_every_steps: int | None = param(
            18, ge=1, help="Re-plan the rest of the day every N steps (18 = 72 min at 240 s/step); unset = "
                           "day start only.")
        nf_replan_idle_trucks: bool = param(True, help="A re-plan may give new jobs to trucks whose itinerary "
                                                       "ran out (else they sleep until the next day).")
        nf_cadence_steps: int = param(6, ge=1, le=360,
                                      help="Dispatch cadence (steps) the plan's latency model assumes; set it to "
                                           "the run's assignment cadence.")
        nf_memory_cap_gib: float = param(4.0, gt=0, le=64, help="Address-space cap of the solve child (GiB).")
        nf_wait_limit_min: float = param(240.0, gt=0, le=1440,
                                         help="Longest a truck may wait between two chained jobs (min).")
        nf_late_slack_min: float | None = param(
            None, ge=0, le=1440, help="Lateness a chain link may absorb (min); unset = per pickup/dropoff "
                                      "type pair from the time model.")
        nf_defer_penalty_km: float = param(200.0, gt=0, le=10000,
                                           help="Cost of leaving a job unplanned (km): above any empty leg.")
        nf_stability_bonus_m: float = param(50.0, ge=0, le=100000,
                                            help="Discount on the previous version's arcs (m): re-plans move "
                                                 "fewer jobs.")
        nf_mip_gap: float | None = param(0.01, ge=0, le=1, help="Relative MIP gap; unset = solver default.")
        nf_mip_k_succ: int | None = param(10, ge=1, help="Successor / start arcs kept per job on the MIP path; "
                                                         "unset = all.")
        nf_mip_arc_budget: int = param(500_000, ge=1,
                                       help="Largest MIP (arcs) attempted; above it the group falls back to greedy.")
        nf_guard_threshold: float = param(0.95, ge=0, le=1,
                                          help="A MIP / greedy plan must serve at least this share of the exact "
                                               "bound, else the previous version is kept.")

        def __post_init__(self) -> None:
            if self.nf_backend not in BACKENDS:
                raise PolicyParamsError(
                    f"NetworkFlowDay params: nf_backend must be one of {', '.join(BACKENDS)} "
                    f"(got {self.nf_backend!r}).")
            if self.nf_time_limit_s > self.nf_max_hold_s - _HOLD_MARGIN_S:
                raise PolicyParamsError(
                    f"NetworkFlowDay params: nf_time_limit_s ({self.nf_time_limit_s}) must leave "
                    f"{_HOLD_MARGIN_S:g} s of nf_max_hold_s ({self.nf_max_hold_s}).")

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        #: ``run_id -> PlanStore``; tests replace it (e.g. a shared ``MemoryKV`` per run).
        self.store_factory: Callable[[str], PlanStore] = default_store_factory
        self._run: _RunState | None = None

    # -- bind ------------------------------------------------------------------------------

    def bind(self, params: Any, world: Mapping[str, Any], *, top: bool) -> Declaration:
        """Validate the active arm (message 14); declare its pairs, planning, run stats and provenance.

        Reads every world key ``solve_tick`` reads: ``cooperation`` (the arm), ``working_hours``
        (the compiled per-haulier hours, F5) and ``unassigned_order_max_wait_steps`` (the
        overdue sweep, M11). Pure: no I/O, no RNG.
        """
        cooperation.validate_arm(world)
        arm = cooperation.resolve_arm(world)
        world.get("working_hours")
        world.get("unassigned_order_max_wait_steps")
        provenance = {
            "badge": BADGE,
            "backend": params.nf_backend,
            "time_model": _time_model().digest,
            "cadence_steps": params.nf_cadence_steps,
            "replan_idle_trucks": params.nf_replan_idle_trucks,
        }
        if not top:  # top_only: never reached through the framework (R5); kept honest anyway
            return Declaration(provenance=provenance)
        planning = {
            "at_day_start": True,
            "every_steps": params.nf_replan_every_steps,
            "foresight": "day",
            "max_hold_seconds": int(params.nf_max_hold_s),
        }
        return Declaration(cross_company=cooperation.directed_pairs(arm), provenance=provenance,
                           planning=planning, run_stats=plan_stats.DayPlanStats,
                           **cooperation.arm_declaration_fields(arm))

    # -- state -----------------------------------------------------------------------------

    def _state(self, run_id: str) -> _RunState:
        st = self._run
        if st is None or st.run_id != run_id:
            try:
                store = self.store_factory(run_id)
            except PlanStoreError as exc:  # e.g. a run id the record cannot name: plan in memory
                logger.error("NetworkFlowDay: plan store for run %r unusable (%s); planning in memory only",
                             run_id, exc)
                store = MemoryPlanStore("local", kv=MemoryKV())
                st = self._run = _RunState(run_id, store)
                st.loaded = True
                st.pending["store_failed"] = f"open: {type(exc).__name__}: {exc}"
                return st
            st = self._run = _RunState(run_id, store)
        if not st.loaded:
            self._reload(st)
        return st

    def _reload(self, st: _RunState) -> None:
        """Agent-restart path: rebuild the in-memory plan from the record (once it answers)."""
        try:
            state = st.store.load_current()
        except PlanStoreError as exc:
            logger.warning("NetworkFlowDay: plan record of run %s not loaded (%s); retried next tick", st.run_id, exc)
            st.pending["store_failed"] = f"load: {type(exc).__name__}: {exc}"
            return
        st.loaded = True
        if state is None:
            return
        st.pending["rebuild"] = plan_stats.rebuild_note(state)["rebuild"]
        cur = state.get("current") or {}
        st.trucks = tuple(sorted((cur.get("itineraries") or {})))
        logger.warning("NetworkFlowDay: run %s restarted on stored plan %s (%d trucks)",
                       st.run_id, cur.get("id"), len(st.trucks))

    def _status(self, st: _RunState, job: str) -> dict[str, Any] | None:
        rec = st.overlay.get(job)
        return dict(rec) if rec is not None else st.store.status_of(job)

    def _refresh_trucks(self, st: _RunState) -> None:
        cur = st.store.state().get("current") or {}
        st.trucks = tuple(sorted((cur.get("itineraries") or {})))

    def _write_statuses(self, st: _RunState, changes: list[tuple[str, str, str | None, str | None]],
                        notes: dict[str, Any]) -> None:
        """Write-through; a refused write keeps the change in the overlay so it is never repeated."""
        if not changes:
            return
        try:
            st.store.set_statuses(changes)
        except PlanStoreUnavailable as exc:  # applied in memory; the store resyncs on its next write
            notes["store_failed"] = f"status: {exc}"
            logger.warning("NetworkFlowDay: status write-through failed (%s); kept in memory", exc)
        except PlanStoreError as exc:
            notes["store_failed"] = f"status: {type(exc).__name__}: {exc}"
            logger.error("NetworkFlowDay: status write refused (%s); kept in the wrapper's overlay", exc)
            for job, status, reason, truck in changes:
                st.overlay[job] = {"status": status, "reason": reason, "truck": truck}
            return
        for job, *_ in changes:
            st.overlay.pop(job, None)

    def _event(self, st: _RunState, job: str, status: str, reason: str | None) -> list[Any]:
        fields = st.store.job_fields(job) or {}
        return [job, status, reason, fields.get("haulier")]

    # -- the tick --------------------------------------------------------------------------

    def solve_tick(self, tick: Any, params: Any) -> Result:
        st = self._state(tick.run_id)
        notes: dict[str, Any] = {}
        if "rebuild" in st.pending:
            notes["rebuild"] = st.pending.pop("rebuild")
        if "store_failed" in st.pending:
            notes["store_failed"] = st.pending.pop("store_failed")
        if tick.kind == "plan":
            return self._plan_tick(tick, params, st, notes)
        return self._dispatch_tick(tick, params, st, notes)

    # -- settling statuses from the lookup ----------------------------------------------------

    def _settle(self, st: _RunState, tick: Any, *, free: frozenset[str],
                changes: list, events: list) -> dict[str, str]:
        """Settle every truck's dispatched jobs from ``tick.lookup``; returns ``{truck: refused job}``.

        done (completed by our truck), ``failed:taken`` (another truck hauls it),
        ``failed:cancelled``; a dispatched job whose truck is FREE in this tick while the
        order is ``unassigned`` and unclaimed was refused (returned, for the retry rule).
        """
        lookup = tick.lookup
        refused: dict[str, str] = {}
        for tid in st.trucks:
            for j in st.store.itinerary(tid):
                rec = self._status(st, j) or {}
                if rec.get("status") != "dispatched" or rec.get("truck") not in (None, tid):
                    continue
                doc = lookup.get(j)
                if doc is None:
                    continue
                state, hauler, claimed = doc.get("state"), _truck_of(doc), doc.get(_CLAIMED_BY)
                claimed = None if claimed in (None, "") else str(claimed)
                outcome: tuple[str, str | None] | None = None
                if state == "cancelled":
                    outcome = ("failed", "cancelled")
                elif state in _HAULED_STATES:
                    if hauler not in (None, tid):
                        outcome = ("failed", "taken")
                    elif state == "completed":
                        outcome = ("done", None)
                elif state in ("unassigned", "created"):
                    if claimed is not None and claimed != tid:
                        outcome = ("failed", "taken")
                    elif claimed is None and state == "unassigned" and tid in free and tid not in refused:
                        refused[tid] = j
                if outcome is not None:
                    status, reason = outcome
                    changes.append((j, status, reason, tid))
                    events.append(self._event(st, j, status, reason))
                    st.retries.pop(j, None)
        return refused

    # -- planning --------------------------------------------------------------------------

    @staticmethod
    def _effective_backend(backend: str) -> str:
        global _WARNED_FALLBACK
        if backend in _ORTOOLS_BACKENDS and not _ortools_importable():
            if not _WARNED_FALLBACK:
                logger.warning("NetworkFlowDay: OR-Tools is not importable in this interpreter; backend %r "
                               "falls back to 'greedy' (offline only: a run refuses this solver, requires=ortools)",
                               backend)
                _WARNED_FALLBACK = True
            return "greedy"
        return backend

    @staticmethod
    def _day_params(params: Any) -> plan_instance.DayPlanParams:
        return plan_instance.DayPlanParams(
            wait_limit_min=float(params.nf_wait_limit_min),
            late_slack_min=params.nf_late_slack_min,
            defer_penalty_km=float(params.nf_defer_penalty_km),
            stability_bonus_m=float(params.nf_stability_bonus_m),
            mip_k_succ=params.nf_mip_k_succ,
            mip_k_start=params.nf_mip_k_succ,
            mip_time_limit_s=float(params.nf_time_limit_s),
            mip_gap=params.nf_mip_gap,
            mip_arc_budget=int(params.nf_mip_arc_budget),
            guard_threshold=float(params.nf_guard_threshold),
            replan_idle_trucks=bool(params.nf_replan_idle_trucks),
        )

    @staticmethod
    def _plan_documents(tick: Any, now_step: int) -> tuple[list, dict, list]:
        """``(trucks, engaged, orders)`` as ``build_inputs`` takes them: first document per id,
        an engaged order never also an open job, and a released (``unassigned``) order that
        carries no ``meta.request_time_step`` released NOW (a copy; the tick's own document is
        never touched)."""
        trucks: dict[str, Any] = {}
        for t in tick.raw_trucks:
            trucks.setdefault(_id(t), t)
        engaged = {str(k): v for k, v in tick.engaged.items()}
        engaged_jobs = {_id(v) for v in engaged.values()}
        orders: dict[str, Any] = {}
        for o in tick.raw_orders:
            oid = _id(o)
            if oid in orders or oid in engaged_jobs or oid in trucks:
                continue
            meta = o.get("meta") or {}
            if meta.get("request_time_step") is None and o.get("request_time_step") is None \
                    and o.get("state") == "unassigned":
                o = {**o, "meta": {**meta, "request_time_step": int(now_step)}}
            orders[oid] = o
        return list(trucks.values()), engaged, list(orders.values())

    def _plan_tick(self, tick: Any, params: Any, st: _RunState, notes: dict[str, Any]) -> Result:
        w = tick.planning
        st.step_seconds = int(w.step_seconds)
        step_s = float(w.step_seconds)
        changes: list = []
        events: list = []
        if st.store.current_id is not None:
            self._settle(st, tick, free=frozenset(), changes=changes, events=events)
            self._write_statuses(st, changes, notes)
        trucks, engaged, orders = self._plan_documents(tick, w.now_step)
        arm = cooperation.resolve_arm(tick.world)
        state = st.store.state()
        if st.overlay:
            status = dict(state.get("status") or {})
            for j, rec in st.overlay.items():
                status[j] = {**(status.get(j) or {}), **rec}
            state["status"] = status
        plan: dict[str, Any]
        try:
            inputs = plan_instance.build_inputs(
                trucks, engaged, orders, state, _time_model(), self._day_params(params),
                now_s=w.now_step * step_s, day=w.day, window_end_s=w.end_step * step_s, step_seconds=step_s,
                cooperation_arm=(arm.adjacency if arm is not None else None),
                working_hours=tick.world.get("working_hours"),
                max_wait_steps=tick.world.get("unassigned_order_max_wait_steps"),
                cadence_steps=int(params.nf_cadence_steps),
                dispatched_at_s={j: s * step_s for j, s in sorted(st.dispatched_at.items())},
            )
        except (ValueError, TypeError) as exc:
            logger.error("NetworkFlowDay: day %d instance not built (%s); the previous plan stays", w.day, exc)
            plan = plan_stats.plan_note(None, outcome="failed", reason="bad_input", day=w.day)
            return self._plan_result(st, notes, events, plan, w.day)
        if inputs.hash == st.last_hash and st.store.current_day == w.day:
            plan = plan_stats.plan_note(inputs, outcome="skipped", reason="unchanged",
                                        version=st.store.current_id)
            return self._plan_result(st, notes, events, plan, w.day)
        backend = self._effective_backend(params.nf_backend)
        try:
            out = self._solve(inputs, backend, params)
        except plan_runner.SolveError as exc:
            logger.error("NetworkFlowDay: day %d solve failed (%s: %s); the previous plan stays",
                         w.day, exc.reason, exc)
            plan = plan_stats.plan_note(inputs, outcome="failed", reason=exc.reason)
            return self._plan_result(st, notes, events, plan, w.day)
        except OSError as exc:  # the child could not even be spawned
            logger.error("NetworkFlowDay: day %d solve child not started (%s); the previous plan stays", w.day, exc)
            plan = plan_stats.plan_note(inputs, outcome="failed", reason="spawn_failed")
            return self._plan_result(st, notes, events, plan, w.day)
        try:
            summary = st.store.write_version(w.day, out["itineraries"], plan_instance.store_job_fields(inputs))
        except PlanStoreUnavailable as exc:
            # Applied in memory (the plan dispatches); the record resyncs on the next write.
            notes["store_failed"] = f"version: {exc}"
            logger.warning("NetworkFlowDay: plan version kept in memory only (%s)", exc)
            summary = {"id": st.store.current_id, "planned": sum(len(c) for c in out["itineraries"].values()),
                       "unplanned": len(out["unplanned"]), "jobs_moved": 0}
        except PlanStoreError as exc:
            notes["store_failed"] = f"version: {type(exc).__name__}: {exc}"
            logger.error("NetworkFlowDay: plan version refused by the store (%s); the previous plan stays", exc)
            plan = plan_stats.plan_note(inputs, outcome="failed", reason="store_refused")
            return self._plan_result(st, notes, events, plan, w.day)
        st.last_hash = inputs.hash
        for j in list(st.overlay):
            if (st.store.status_of(j) or {}).get("status") in ("planned", "unplanned"):
                st.overlay.pop(j)  # re-planned: the new version is the truth
        self._refresh_trucks(st)
        plan = plan_stats.plan_note(inputs, outcome="solved", version=summary.get("id"),
                                    write_summary=summary, solve=out)
        if backend != params.nf_backend:
            notes["backend_fallback"] = backend
        return self._plan_result(st, notes, events, plan, w.day)

    def _solve(self, inputs: Any, backend: str, params: Any) -> dict[str, Any]:
        if not inputs["jobs"] or not inputs["trucks"]:  # nothing to optimise: no child
            return {"itineraries": {}, "unplanned": sorted(j["id"] for j in inputs["jobs"]),
                    "stats": {"backends": {}, "build_ms": 0.0, "solve_ms": 0.0, "wall_ms": 0.0,
                              "deterministic": True, "first_leg_m": {}, "status": "empty"}}
        return plan_runner.solve(
            inputs, backend=backend, time_limit_s=float(params.nf_time_limit_s),
            timeout_s=max(1.0, float(params.nf_max_hold_s) - _HOLD_MARGIN_S),
            memory_cap_bytes=int(float(params.nf_memory_cap_gib) * 1024 ** 3))

    def _plan_result(self, st: _RunState, notes: dict[str, Any], events: list, plan: Mapping[str, Any],
                     day: int) -> Result:
        tick_notes = plan_stats.merge_notes(notes, plan, plan_stats.events_note(day, events) if events else {})
        tick_notes["day"] = int(day)
        return Result(pairs=(), prefetch=prefetch_ids(st.store, st.trucks, lambda j: self._status(st, j)),
                      tick_notes=tick_notes)

    # -- dispatch --------------------------------------------------------------------------

    def _dispatch_tick(self, tick: Any, params: Any, st: _RunState, notes: dict[str, Any]) -> Result:
        if st.store.current_id is None:  # no plan yet (or it could not be loaded): nothing to hand out
            return Result(tick_notes=notes)
        trucks_by_id, orders_by_id = tick.id_index
        lookup = tick.lookup
        free = frozenset(trucks_by_id)
        changes: list = []
        events: list = []
        refused = self._settle(st, tick, free=free, changes=changes, events=events)
        settled = {c[0] for c in changes}
        pairs: list[tuple[Any, Any]] = []
        paired_orders: set[str] = set()

        def change(job: str, status: str, reason: str | None, tid: str) -> None:
            changes.append((job, status, reason, tid))
            settled.add(job)
            events.append(self._event(st, job, status, reason))

        for tid in sorted(free):
            truck = trucks_by_id[tid]
            retry = refused.get(tid)
            if retry is not None:
                order = orders_by_id.get(retry)
                if st.retries.get(retry, 0) < _MAX_RETRIES and order is not None and retry not in paired_orders \
                        and tick.permitted(truck, order):
                    st.retries[retry] = st.retries.get(retry, 0) + 1
                    st.dispatched_at[retry] = int(tick.clock.time_step)
                    pairs.append((truck, order))
                    paired_orders.add(retry)
                    continue
                change(retry, "failed", "refused", tid)
                st.retries.pop(retry, None)
            for j in st.store.itinerary(tid):
                if j in settled:
                    continue
                rec = self._status(st, j) or {}
                if rec.get("status") != "planned":
                    continue
                verdict = self._head_rule(tid, truck, j, lookup.get(j), orders_by_id.get(j), tick)
                if verdict == "wait":
                    break
                if verdict == "pair":
                    if j in paired_orders:  # cannot happen (itineraries are disjoint); never pair twice
                        break
                    change(j, "dispatched", None, tid)
                    st.dispatched_at[j] = int(tick.clock.time_step)
                    pairs.append((truck, orders_by_id[j]))
                    paired_orders.add(j)
                    break
                status, _, reason = verdict.partition(":")
                change(j, status, reason or None, tid)
        self._write_statuses(st, changes, notes)
        day = st.store.current_day or 0
        if events:
            notes.update(plan_stats.events_note(day, events))
        else:
            notes["day"] = int(day)
        return Result.from_pairs(pairs, tick_notes=notes,
                                 prefetch=prefetch_ids(st.store, st.trucks, lambda j: self._status(st, j)))

    @staticmethod
    def _head_rule(tid: str, truck: Any, job: str, doc: Mapping[str, Any] | None, order: Any, tick: Any) -> str:
        """``"pair"``, ``"wait"`` or ``"<status>[:<reason>]"`` for the planned head ``job`` (table above)."""
        if doc is not None:
            state, hauler = doc.get("state"), _truck_of(doc)
            claimed = doc.get(_CLAIMED_BY)
            claimed = None if claimed in (None, "") else str(claimed)
            if state in _HAULED_STATES:
                # Somebody's haul. Ours only when the document says so (a head was never
                # dispatched by this plan, so an unnamed hauler is somebody else).
                if (hauler or claimed) != tid:
                    return "failed:taken"
                return "done" if state == "completed" else "dispatched"
            if state == "cancelled":
                return "failed:cancelled"
            if state == "created":
                return "wait"
            if state == "unassigned":
                if claimed is not None:
                    return "dispatched" if claimed == tid else "failed:taken"
                if order is None:
                    return "failed:taken"  # unassigned, yet the app left it out: an open haul claims it
                return "pair" if tick.permitted(truck, order) else "failed:infeasible"
            return "wait"  # an unknown state: never guess
        if order is None:
            return "wait"
        return "pair" if tick.permitted(truck, order) else "failed:infeasible"
