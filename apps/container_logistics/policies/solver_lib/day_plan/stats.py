"""``DayPlanStats``: a day-plan solver's run statistics (``run_config.meta.day_plan``).

Single responsibility: fold the day-plan wrapper's per-tick ``Result.tick_notes`` into run
level KPI (design §6a / §7, plan §4 ``stats.py``, critique M11 + minor 15), decide when
they are pushed. A ``dispatch.run_stats.RunStatsAggregator`` with ``meta_key =
"day_plan"``. Solver helper; stdlib only; the framework drives it without knowing it.

**Notes vocabulary** (build them with :func:`plan_note`, :func:`events_note`,
:func:`rebuild_note`; :func:`merge_notes` joins several into one tick's notes)::

    {"day": n,                                    # run day of the tick (elapsed axis)
     "plan": {"outcome": "solved"|"skipped"|"failed", "reason": str|null,
              "version": "d3v2"|null, "jobs_moved": int, "planned": int, "unplanned": int,
              "new_jobs": {job: haulier},          # first planned in this version
              "first_leg_km": {haulier: km},       # this version's truck -> first job km
              "solve_ms": float, "build_ms": float, "wall_ms": float,
              "deterministic": bool, "backends": {backend: n_components}},
     "events": [[job, status, reason|null, haulier|null], ...],
     "rebuild": {"jobs": {job: [haulier, first_day, status, reason]},
                 "kpi": {"d3": {"versions": k, "jobs_moved": m}}}}

``status`` of an event is a store status (``dispatched``, ``done``, ``failed``,
``planned`` for a retry that went back to the plan). Every key is optional.

**Attribution (M11).** A job belongs to the day of the version that FIRST planned it
(``new_jobs``), for ever: a job rolled over from day n and done on day n+1 counts as
``done`` for day n, never as day-n ``undone`` and day-(n+1) ``done``. The per-job status is
the latest one, so a job that failed (``infeasible``), was re-planned and then done counts
once, as done. ``plan_jobs_undone`` (per day, at snapshot time) = the day's jobs that are
neither ``done``, ``dispatched`` nor ``failed``; ``open_at_day_end`` freezes the same count
when the next day's first plan tick arrives (the design's "undone at day end").

**Restart (minor 15).** The aggregator is in-memory. After an agent restart the wrapper
folds :func:`rebuild_note` (from ``PlanStore.state()``, the Redis record) once: per-job
status and attribution come back exactly where the record carries ``first_day`` in the job
fields (else the day of the job's status version); per-day ``versions`` / ``jobs_moved``
come back from the store's KPI hash; solve times and skip / failure counters of the lost
process cannot, so ``restarts`` is stamped.

**Push cadence.** Forced (``fold`` returns ``True``) on a new day, a failed re-plan and a
rebuild; otherwise every :data:`PUSH_EVERY` folded ticks.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

__all__ = ["DayPlanStats", "PUSH_EVERY", "events_note", "merge_notes", "plan_note", "rebuild_note"]

PUSH_EVERY = 10
SCHEMA = 1
_DONE, _DISPATCHED, _FAILED = "done", "dispatched", "failed"
_VERSION_RE = re.compile(r"^d(\d+)v\d+$")


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
        jobs = {j["id"]: j for j in inputs["jobs"]}
        truck_h = {t["id"]: inputs["classes"][t["class"]]["haulier"] for t in inputs["trucks"]}
        first: dict[str, float] = {}
        for tid, m in (st.get("first_leg_m") or {}).items():
            h = truck_h.get(tid)
            first[h] = first.get(h, 0.0) + float(m) / 1000.0
        ws = write_summary or {}
        plan.update(
            jobs_moved=int(ws.get("jobs_moved", 0)),
            planned=int(ws.get("planned", sum(len(c) for c in solve.get("itineraries", {}).values()))),
            unplanned=int(ws.get("unplanned", len(solve.get("unplanned", ())))),
            new_jobs={j: jobs[j]["owner"] for j in sorted(jobs) if jobs[j].get("new")},
            first_leg_km={h: round(v, 3) for h, v in sorted(first.items())},
            solve_ms=float(st.get("solve_ms", 0.0)), build_ms=float(st.get("build_ms", 0.0)),
            wall_ms=float(st.get("wall_ms", 0.0)), deterministic=bool(st.get("deterministic", True)),
            backends=dict(st.get("backends") or {}))
    return {"day": d, "plan": plan}


def events_note(day: int, events: Iterable[Iterable[Any]]) -> dict[str, Any]:
    """Notes of a dispatch tick: ``[(job, status, reason, haulier), ...]`` status changes."""
    out = []
    for ev in events:
        ev = list(ev)
        ev += [None] * (4 - len(ev))
        job, status, reason, haulier = ev[:4]
        if status.startswith("failed:"):
            status, reason = _FAILED, reason or status.split(":", 1)[1]
        out.append([str(job), str(status), reason, haulier])
    return {"day": int(day), "events": out}


def rebuild_note(store_state: Mapping[str, Any]) -> dict[str, Any]:
    """Notes that restore the aggregator from ``PlanStore.state()`` after a restart."""
    cur = store_state.get("current") or {}
    fields = cur.get("jobs") or {}
    jobs = {}
    for j, rec in sorted((store_state.get("status") or {}).items()):
        f = fields.get(j) or {}
        d = f.get("first_day")
        if not isinstance(d, int):
            m = _VERSION_RE.match(str(rec.get("version") or ""))
            d = int(m.group(1)) if m else None
        jobs[j] = [f.get("haulier"), d, rec.get("status"), rec.get("reason")]
    kpi = {k: {"versions": int(v.get("versions", 0)), "jobs_moved": int(v.get("jobs_moved", 0))}
           for k, v in sorted((store_state.get("kpi") or {}).items())}
    return {"day": cur.get("day"), "rebuild": {"jobs": jobs, "kpi": kpi}}


def merge_notes(*notes: Mapping[str, Any]) -> dict[str, Any]:
    """One tick's notes from several (a planning step that also dispatched)."""
    out: dict[str, Any] = {}
    for n in notes:
        for k, v in n.items():
            if k == "events":
                out.setdefault("events", []).extend(v)
            elif k in out and k != "day":
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
        self._jobs: dict[str, list] = {}       # job -> [haulier, first_day, status, reason]
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

    # -- protocol -------------------------------------------------------------------
    @classmethod
    def empty_snapshot(cls) -> dict[str, Any]:
        return cls().snapshot()

    def fold(self, tick_notes: Mapping[str, Any], clock: Any) -> bool:
        self._ticks += 1
        self._since_push += 1
        self._folded = True
        forced = False
        day = tick_notes.get("day")
        if "rebuild" in tick_notes:
            self._rebuild(tick_notes["rebuild"])
            forced = True
        plan = tick_notes.get("plan")
        if plan is not None:
            if day is None:
                raise ValueError("a plan note needs its day")
            forced |= self._fold_plan(int(day), plan)
        for ev in tick_notes.get("events") or ():
            self._fold_event(day, ev)
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
                "planned": None, "unplanned": None, "first_leg_km": None, "open_at_day_end": None}

    def _day(self, d: int) -> dict[str, Any]:
        rec = self._days.get(d)
        if rec is None:
            rec = self._days[d] = self._blank_day()
        return rec

    def _close_days_before(self, day: int) -> None:
        for d in sorted(self._days):
            if d < day and self._days[d]["open_at_day_end"] is None:
                self._days[d]["open_at_day_end"] = self._open_counts(d)

    def _fold_plan(self, day: int, plan: Mapping[str, Any]) -> bool:
        self._plan_ticks += 1
        forced = False
        if self._last_day is None or day > self._last_day:
            if self._last_day is not None:
                forced = True
            self._close_days_before(day)
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
            rec["planned"] = int(plan.get("planned", 0))
            rec["unplanned"] = int(plan.get("unplanned", 0))
            if rec["first_leg_km"] is None:  # the day plan (v1); re-plans start mid-day
                rec["first_leg_km"] = {h: float(v) for h, v in sorted((plan.get("first_leg_km") or {}).items())}
            for j, h in (plan.get("new_jobs") or {}).items():
                if j not in self._jobs:
                    self._jobs[j] = [h, day, "planned", None]
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

    def _fold_event(self, day: Any, ev: Iterable[Any]) -> None:
        ev = list(ev) + [None] * 4
        job, status, reason, haulier = ev[0], ev[1], ev[2], ev[3]
        rec = self._jobs.get(job)
        if rec is None:  # a job the aggregator never saw planned (restart without rebuild)
            rec = self._jobs[job] = [haulier, int(day) if day is not None else None, None, None]
        elif rec[0] is None and haulier is not None:
            rec[0] = haulier
        rec[2], rec[3] = status, (reason if status == _FAILED else None)

    def _rebuild(self, data: Mapping[str, Any]) -> None:
        self._restarts += 1
        for j, (h, d, s, r) in (data.get("jobs") or {}).items():
            old = self._jobs.get(j)
            self._jobs[j] = [h if h is not None else (old[0] if old else None),
                             d if d is not None else (old[1] if old else None), s, r]
        for k, v in (data.get("kpi") or {}).items():
            rec = self._day(int(str(k).lstrip("d")))
            rec["versions"] = max(rec["versions"], int(v.get("versions", 0)))
            rec["jobs_moved"] = max(rec["jobs_moved"], int(v.get("jobs_moved", 0)))
        days = [d for d in self._days]
        if days:
            self._last_day = max(days + ([self._last_day] if self._last_day is not None else []))

    # -- views ----------------------------------------------------------------------
    def _open_counts(self, d: int) -> dict[str, Any]:
        total, by_h = 0, {}
        for h, fd, s, _ in self._jobs.values():
            if fd == d and s not in (_DONE, _DISPATCHED, _FAILED):
                total += 1
                by_h[str(h)] = by_h.get(str(h), 0) + 1
        return {"total": total, "by_haulier": dict(sorted(by_h.items()))}

    @staticmethod
    def _counts(rows: Iterable[list]) -> dict[str, Any]:
        c = {"plan_jobs": 0, "plan_jobs_done": 0, "plan_jobs_dispatched": 0,
             "plan_jobs_failed": {}, "plan_jobs_undone": 0}
        for _, _, s, r in rows:
            c["plan_jobs"] += 1
            if s == _DONE:
                c["plan_jobs_done"] += 1
            elif s == _DISPATCHED:
                c["plan_jobs_dispatched"] += 1
            elif s == _FAILED:
                key = str(r or "unknown")
                c["plan_jobs_failed"][key] = c["plan_jobs_failed"].get(key, 0) + 1
            else:
                c["plan_jobs_undone"] += 1
        c["plan_jobs_failed"] = dict(sorted(c["plan_jobs_failed"].items()))
        return c

    def snapshot(self) -> dict[str, Any]:
        by_day: dict[int, list] = {}
        for row in self._jobs.values():
            by_day.setdefault(row[1], []).append(row)
        days_out = {}
        for d in sorted(set(self._days) | {k for k in by_day if k is not None}):
            rec = self._days.get(d) or self._blank_day()
            rows = by_day.get(d, [])
            hs: dict[str, list] = {}
            for row in rows:
                hs.setdefault(str(row[0]), []).append(row)
            days_out[f"d{d}"] = {
                **self._counts(rows),
                "plan_jobs_moved": rec["jobs_moved"],
                "plan_first_leg_km": (None if rec["first_leg_km"] is None else
                                      {h: round(v, 3) for h, v in rec["first_leg_km"].items()}),
                "versions": rec["versions"],
                "replans_skipped": rec["replans_skipped"],
                "replans_failed": dict(sorted(rec["replans_failed"].items())),
                "planned_last": rec["planned"], "unplanned_last": rec["unplanned"],
                "open_at_day_end": rec["open_at_day_end"],
                "by_haulier": {h: self._counts(r) for h, r in sorted(hs.items())},
            }
        totals = self._counts(self._jobs.values())
        totals["plan_jobs_moved"] = sum(r["jobs_moved"] for r in self._days.values())
        totals["versions"] = sum(r["versions"] for r in self._days.values())
        totals["replans_skipped"] = sum(r["replans_skipped"] for r in self._days.values())
        failed: dict[str, int] = {}
        for r in self._days.values():
            for k, v in r["replans_failed"].items():
                failed[k] = failed.get(k, 0) + v
        totals["replans_failed"] = dict(sorted(failed.items()))
        return {
            "schema": SCHEMA, "ticks": self._ticks, "plan_ticks": self._plan_ticks,
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
        }
