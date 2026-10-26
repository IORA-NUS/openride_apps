"""``DayPlanStats``: a day-plan solver's run statistics (``run_config.meta.day_plan``).

Single responsibility: fold the day-plan wrapper's per-tick ``Result.tick_notes`` into run
level PLANNER statistics (design §6a / §7, plan §4 ``stats.py``, minor 15), decide when
they are pushed. A ``dispatch.run_stats.RunStatsAggregator`` with ``meta_key =
"day_plan"``. Solver helper; stdlib only; the framework drives it without knowing it.

**Planner-only (schema 2).** These stats describe the PLANNER: plan versions, re-plan
churn (jobs moved), skipped / failed re-plans, fallbacks, incidents and quality. Order
outcomes (done / undone / failed) are sim KPIs that every arm gets,
``run_config.meta.order_outcomes``; they are never re-derived here. Schema 1 also carried
``plan_jobs`` / ``plan_jobs_done`` / ``plan_jobs_dispatched`` / ``plan_jobs_failed`` /
``plan_jobs_undone`` (totals and per day, with ``by_haulier``) and ``open_at_day_end``;
schema 2 removed them, so ``schema`` tells the two shapes apart.

**Notes vocabulary** (build them with :func:`plan_note`, :func:`rebuild_note`;
:func:`merge_notes` joins several into one tick's notes)::

    {"day": n,                                    # run day of the tick (elapsed axis)
     "plan": {"outcome": "solved"|"skipped"|"failed", "reason": str|null,
              "version": "d3v2"|null, "jobs_moved": int, "planned": int, "unplanned": int,
              "first_leg_km": {haulier: km},       # this version's truck -> first job km
              "solve_ms": float, "build_ms": float, "wall_ms": float,
              "deterministic": bool, "backends": {backend: n_components},
              "moved": {"truck": n, "position": n, "to_unplanned": n},   # jobs_moved split
              "prev_arcs": {"prev_job_arcs", "prev_job_arcs_feasible", "prev_start_arcs",
                            "prev_start_arcs_feasible", "prev_arcs_kept"},   # re-plans only
              "fallback": "greedy", "fallback_reason": str,   # a no-plan day's greedy plan
              "fallback_failed": str, "component_fallbacks": {reason: n},
              "accepted_poor": true,
              "inexact_components": {reason: n}},  # groups the exact flow could not take (V-M1)
     "late_steps": n,                              # the plan tick ran n steps after its boundary
     "no_plan": 1,                                 # a dispatch tick with no plan at all
     "uncommitted": n,                             # last tick's pairs the framework did not commit
     "store_failed": str, "backend_fallback": str, # plan store / backend degradations
     "rebuild": {"kpi": {"d3": {"versions": k, "jobs_moved": m}}}}

Every key is optional. A day record is created only by a plan note (or a rebuild's
``kpi``). :meth:`DayPlanStats.fold` IGNORES an ``events`` key (a schema-1 wrapper's
per-job status changes): job statuses are the dispatch store's business, not run stats.

**Restart (minor 15).** The aggregator is in-memory. After an agent restart the wrapper
folds :func:`rebuild_note` (from ``PlanStore.state()``, the Redis record) once: per-day
``versions`` / ``jobs_moved`` come back from the store's KPI hash; solve times and skip /
failure counters of the lost process cannot, so ``restarts`` is stamped.

**Incidents** (§6.21 class: a run must not read ``ok`` while degraded): every degradation
note is counted in ``incidents`` (``store_failed``, ``backend_fallback`` {backend: n},
``late_plans`` / ``late_steps_max``, ``no_plan_ticks``, ``plan_fallbacks`` {reason: n},
``fallback_failed``, ``component_fallbacks`` {reason: n}, ``accepted_poor``,
``inexact_components`` {reason: n}, ``uncommitted_pairs``).

**Quality (V-M1).** ``quality = {"degraded": bool, "reasons": [...]}`` at the top of the
snapshot: ``degraded`` whenever any plan of the run was not the solver's own answer --
a greedy fallback plan, a failed fallback, a component fallback, a plan kept under the
guard (``accepted_poor``), a component the exact flow could not take, or an OR-Tools
backend fallback -- so an arm that silently ran greedy is visible where runs are
compared, not only in ``incidents``. :meth:`DayPlanStats.run_end_warnings` repeats it as
the run-end WARNING the framework logs (``run_stats.flush``).

**Push cadence.** Forced (``fold`` returns ``True``) on a new day, a failed re-plan, a
fallback plan (or a failed fallback) and a rebuild; otherwise every :data:`PUSH_EVERY` folded ticks.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

__all__ = ["DayPlanStats", "PUSH_EVERY", "merge_notes", "plan_note", "rebuild_note"]

PUSH_EVERY = 10
SCHEMA = 2


# ------------------------------------------------------------------------------ notes


def plan_note(inputs: Mapping[str, Any] | None, *, outcome: str, version: str | None = None,
              write_summary: Mapping[str, Any] | None = None, solve: Mapping[str, Any] | None = None,
              reason: str | None = None, day: int | None = None) -> dict[str, Any]:
    """Notes of one planning tick.

    ``inputs`` -- the :class:`instance.PlanInputs` solved (``None`` allowed for a skip /
    failure before building); ``write_summary`` -- ``PlanStore.write_version``'s return;
    ``solve`` -- ``runner.solve``'s return (or ``{"stats": ...}`` of a failure)."""
    if outcome not in ("solved", "skipped", "failed"):
        raise ValueError(f"outcome must be solved / skipped / failed (got {outcome!r})")
    d = day if day is not None else (inputs or {}).get("day")
    plan: dict[str, Any] = {"outcome": outcome, "reason": reason, "version": version}
    if outcome == "solved":
        if inputs is None or solve is None:
            raise ValueError("a solved plan note needs inputs and the solve result")
        st = solve.get("stats") or {}
        truck_h = {t["id"]: inputs["classes"][t["class"]]["haulier"] for t in inputs["trucks"]}
        first: dict[str, float] = {}
        for tid, m in (st.get("first_leg_m") or {}).items():
            h = truck_h.get(tid)
            first[h] = first.get(h, 0.0) + float(m) / 1000.0
        ws = write_summary or {}
        plan.update(
            jobs_moved=int(ws.get("jobs_moved", 0)),
            moved={k: int(ws.get(f"moved_{k}", 0)) for k in ("truck", "position", "to_unplanned")},
            planned=int(ws.get("planned", sum(len(c) for c in solve.get("itineraries", {}).values()))),
            unplanned=int(ws.get("unplanned", len(solve.get("unplanned", ())))),
            first_leg_km={h: round(v, 3) for h, v in sorted(first.items())},
            solve_ms=float(st.get("solve_ms", 0.0)), build_ms=float(st.get("build_ms", 0.0)),
            wall_ms=float(st.get("wall_ms", 0.0)), deterministic=bool(st.get("deterministic", True)),
            backends=dict(st.get("backends") or {}))
        if st.get("fallbacks"):
            plan["component_fallbacks"] = dict(st["fallbacks"])
        if st.get("accepted_poor"):
            plan["accepted_poor"] = True
        if st.get("prev_arcs"):
            plan["prev_arcs"] = {k: int(v) for k, v in sorted(st["prev_arcs"].items())}
    return {"day": d, "plan": plan}


def rebuild_note(store_state: Mapping[str, Any]) -> dict[str, Any]:
    """Notes that restore the aggregator from ``PlanStore.state()`` after a restart
    (planner KPI only: per-day ``versions`` / ``jobs_moved``)."""
    cur = store_state.get("current") or {}
    kpi = {k: {"versions": int(v.get("versions", 0)), "jobs_moved": int(v.get("jobs_moved", 0))}
           for k, v in sorted((store_state.get("kpi") or {}).items())}
    return {"day": cur.get("day"), "rebuild": {"kpi": kpi}}


def merge_notes(*notes: Mapping[str, Any]) -> dict[str, Any]:
    """One tick's notes from several (a planning step that also dispatched)."""
    out: dict[str, Any] = {}
    for n in notes:
        for k, v in n.items():
            if k in out and k != "day":
                raise ValueError(f"two notes carry {k!r}")
            else:
                out[k] = v
    return out


# ------------------------------------------------------------------------- aggregator


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(math.ceil(q * len(s))) - 1))
    return round(s[k], 1)


class DayPlanStats:
    """Run-stats aggregator of a day-plan solver (``meta_key = "day_plan"``)."""

    meta_key = "day_plan"

    def __init__(self) -> None:
        self._days: dict[int, dict[str, Any]] = {}
        self._solve_ms: list[float] = []
        self._build_ms: list[float] = []
        self._wall_ms: list[float] = []
        self._backends: dict[str, int] = {}
        self._deterministic = True
        self._ticks = 0
        self._plan_ticks = 0
        self._restarts = 0
        self._last_day: int | None = None
        self._since_push = 0
        self._folded = False
        self._incidents: dict[str, Any] = self._blank_incidents()

    # -- protocol -------------------------------------------------------------------
    @classmethod
    def empty_snapshot(cls) -> dict[str, Any]:
        return cls().snapshot()

    @staticmethod
    def _blank_incidents() -> dict[str, Any]:
        return {"store_failed": 0, "backend_fallback": {}, "late_plans": 0, "late_steps_max": 0,
                "no_plan_ticks": 0, "plan_fallbacks": {}, "fallback_failed": 0,
                "component_fallbacks": {}, "accepted_poor": 0, "inexact_components": {},
                "uncommitted_pairs": 0}

    def _count(self, key: str, sub: Any = None, n: int = 1) -> None:
        if sub is None:
            self._incidents[key] += int(n)
        else:
            d = self._incidents[key]
            d[str(sub)] = d.get(str(sub), 0) + int(n)

    def _fold_incidents(self, notes: Mapping[str, Any]) -> bool:
        forced = False
        if notes.get("store_failed"):  # counted, not forced: a dead Redis notes it on EVERY tick
            self._count("store_failed")
        if notes.get("backend_fallback"):
            self._count("backend_fallback", notes["backend_fallback"])
        if notes.get("late_steps"):
            self._count("late_plans")
            self._incidents["late_steps_max"] = max(self._incidents["late_steps_max"], int(notes["late_steps"]))
        if notes.get("no_plan"):
            self._count("no_plan_ticks")
        if notes.get("uncommitted"):
            self._count("uncommitted_pairs", n=int(notes["uncommitted"]))
        plan = notes.get("plan") or {}
        if plan.get("fallback"):
            self._count("plan_fallbacks", plan.get("fallback_reason") or "unknown")
            forced = True
        if plan.get("fallback_failed"):
            self._count("fallback_failed")
            forced = True
        for k, v in (plan.get("component_fallbacks") or {}).items():
            self._count("component_fallbacks", k, int(v))
        if plan.get("accepted_poor"):
            self._count("accepted_poor")
        for k, v in (plan.get("inexact_components") or {}).items():
            self._count("inexact_components", k, int(v))
        return forced

    def fold(self, tick_notes: Mapping[str, Any], clock: Any) -> bool:
        self._ticks += 1
        self._since_push += 1
        self._folded = True
        forced = self._fold_incidents(tick_notes)
        day = tick_notes.get("day")
        if "rebuild" in tick_notes:
            self._rebuild(tick_notes["rebuild"])
            forced = True
        plan = tick_notes.get("plan")
        if plan is not None:
            if day is None:
                raise ValueError("a plan note needs its day")
            forced |= self._fold_plan(int(day), plan)
        # an ``events`` key (schema-1 job status changes) is ignored: planner-only stats
        return forced

    def push_due(self, *, force: bool = False) -> bool:
        if not self._folded:
            return False
        if force:
            return True
        return self._since_push >= PUSH_EVERY

    def mark_pushed(self) -> None:
        self._since_push = 0

    # -- folding --------------------------------------------------------------------
    @staticmethod
    def _blank_day() -> dict[str, Any]:
        return {"versions": 0, "replans_skipped": 0, "replans_failed": {}, "jobs_moved": 0,
                "moved": {"truck": 0, "position": 0, "to_unplanned": 0},
                "prev_arcs": {}, "fallbacks": 0, "planned": None, "unplanned": None, "first_leg_km": None}

    def _day(self, d: int) -> dict[str, Any]:
        rec = self._days.get(d)
        if rec is None:
            rec = self._days[d] = self._blank_day()
        return rec

    def _fold_plan(self, day: int, plan: Mapping[str, Any]) -> bool:
        self._plan_ticks += 1
        forced = False
        if self._last_day is None or day > self._last_day:
            if self._last_day is not None:
                forced = True
            self._last_day = day
        rec = self._day(day)
        outcome = plan.get("outcome")
        if outcome == "skipped":
            rec["replans_skipped"] += 1
        elif outcome == "failed":
            reason = str(plan.get("reason") or "error")
            rec["replans_failed"][reason] = rec["replans_failed"].get(reason, 0) + 1
            forced = True
        elif outcome == "solved":
            rec["versions"] += 1
            rec["jobs_moved"] += int(plan.get("jobs_moved", 0))
            for k, v in (plan.get("moved") or {}).items():
                rec["moved"][k] = rec["moved"].get(k, 0) + int(v)
            for k, v in (plan.get("prev_arcs") or {}).items():
                rec["prev_arcs"][k] = rec["prev_arcs"].get(k, 0) + int(v)
            if plan.get("fallback"):
                rec["fallbacks"] += 1
            rec["planned"] = int(plan.get("planned", 0))
            rec["unplanned"] = int(plan.get("unplanned", 0))
            if rec["first_leg_km"] is None:  # the day plan (v1); re-plans start mid-day
                rec["first_leg_km"] = {h: float(v) for h, v in sorted((plan.get("first_leg_km") or {}).items())}
            for k, ms in (("solve_ms", self._solve_ms), ("build_ms", self._build_ms), ("wall_ms", self._wall_ms)):
                if plan.get(k) is not None:
                    ms.append(float(plan[k]))
            if plan.get("deterministic") is False:
                self._deterministic = False
            for b, n in (plan.get("backends") or {}).items():
                self._backends[b] = self._backends.get(b, 0) + int(n)
        else:
            raise ValueError(f"unknown plan outcome {outcome!r}")
        return forced

    def _rebuild(self, data: Mapping[str, Any]) -> None:
        self._restarts += 1
        for k, v in (data.get("kpi") or {}).items():
            rec = self._day(int(str(k).lstrip("d")))
            rec["versions"] = max(rec["versions"], int(v.get("versions", 0)))
            rec["jobs_moved"] = max(rec["jobs_moved"], int(v.get("jobs_moved", 0)))
        days = [d for d in self._days]
        if days:
            self._last_day = max(days + ([self._last_day] if self._last_day is not None else []))

    # -- views ----------------------------------------------------------------------
    #: incident key -> the ``quality.reasons`` entry it raises (V-M1).
    _DEGRADING = (("plan_fallbacks", "fallback_plan"), ("fallback_failed", "fallback_failed"),
                  ("component_fallbacks", "component_fallback"), ("accepted_poor", "accepted_poor"),
                  ("inexact_components", "inexact"), ("backend_fallback", "backend_fallback"))

    def _quality(self) -> dict[str, Any]:
        reasons = [name for key, name in self._DEGRADING if self._incidents.get(key)]
        return {"degraded": bool(reasons), "reasons": reasons}

    def run_end_warnings(self) -> list[str]:
        """The run-end WARNING lines (``run_stats.flush``): one when any plan was degraded."""
        q = self._quality()
        if not q["degraded"]:
            return []
        inc = self._incidents
        detail = ", ".join(f"{key}={inc[key]}" for key, _ in self._DEGRADING if inc.get(key))
        return [f"day plan DEGRADED this run ({', '.join(q['reasons'])}): {detail}. Plans were not the "
                "solver's own exact answer; compare this arm with care (run_config.meta.day_plan.quality)."]

    def snapshot(self) -> dict[str, Any]:
        days_out = {}
        for d in sorted(self._days):
            rec = self._days[d]
            days_out[f"d{d}"] = {
                "plan_jobs_moved": rec["jobs_moved"],
                "plan_jobs_moved_by": dict(sorted(rec["moved"].items())),
                "prev_arcs": dict(sorted(rec["prev_arcs"].items())),
                "fallback_plans": rec["fallbacks"],
                "plan_first_leg_km": (None if rec["first_leg_km"] is None else
                                      {h: round(v, 3) for h, v in rec["first_leg_km"].items()}),
                "versions": rec["versions"],
                "replans_skipped": rec["replans_skipped"],
                "replans_failed": dict(sorted(rec["replans_failed"].items())),
                "planned_last": rec["planned"], "unplanned_last": rec["unplanned"],
            }
        totals: dict[str, Any] = {}
        totals["plan_jobs_moved"] = sum(r["jobs_moved"] for r in self._days.values())
        by: dict[str, int] = {"truck": 0, "position": 0, "to_unplanned": 0}
        for r in self._days.values():
            for k, v in r["moved"].items():
                by[k] = by.get(k, 0) + v
        totals["plan_jobs_moved_by"] = dict(sorted(by.items()))
        totals["versions"] = sum(r["versions"] for r in self._days.values())
        totals["replans_skipped"] = sum(r["replans_skipped"] for r in self._days.values())
        failed: dict[str, int] = {}
        for r in self._days.values():
            for k, v in r["replans_failed"].items():
                failed[k] = failed.get(k, 0) + v
        totals["replans_failed"] = dict(sorted(failed.items()))
        return {
            "schema": SCHEMA, "quality": self._quality(), "ticks": self._ticks, "plan_ticks": self._plan_ticks,
            "restarts": self._restarts, "deterministic": self._deterministic,
            "backends": dict(sorted(self._backends.items())),
            "solve_ms": {"n": len(self._solve_ms), "p50": _pct(self._solve_ms, 0.5),
                         "max": (round(max(self._solve_ms), 1) if self._solve_ms else None)},
            "build_ms": {"p50": _pct(self._build_ms, 0.5),
                         "max": (round(max(self._build_ms), 1) if self._build_ms else None)},
            "wall_ms": {"p50": _pct(self._wall_ms, 0.5),
                        "max": (round(max(self._wall_ms), 1) if self._wall_ms else None),
                        "sum": round(sum(self._wall_ms), 1)},
            "totals": totals, "days": days_out,
            "incidents": {k: (dict(sorted(v.items())) if isinstance(v, dict) else v)
                          for k, v in sorted(self._incidents.items())},
        }
