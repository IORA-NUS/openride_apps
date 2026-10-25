"""When the assignment agent plans, and who is on shift (NetworkFlowDay F1 / F5; app side).

Pure helpers, no I/O and no ``policies.dispatch`` import (the import-cycle rule of
``assignment/app.py``):

- :class:`PlanningSchedule` decides whether a step is PLANNING-DUE for a top's
  ``Declaration.planning`` (``{at_day_start, every_steps, foresight, max_hold_seconds}``).
  Day boundaries are on the ELAPSED axis (steps since the run's reference time,
  ``steps_per_day = 86400 // step_seconds``), like the demand curve and the compiled
  working hours. A boundary is planned at the FIRST step at or after it that has not
  yet been planned (critique minor 4: an agent still booting at step 0, or pruned on a
  boundary step, catches up on its next step instead of skipping the day), and such a
  late plan is reported (``Due.late_steps``). Nothing is planned at or after the
  horizon (minor 3: the post-horizon drain).
- :func:`on_shift` reads the compiled working-hours record
  (``{"axis": "elapsed", "hauliers": {hid: [start_s, end_s]}}``, ``datagen.hauliers``):
  ``end_s < start_s`` is an overnight window, ``end_s`` may be 86400, a haulier that is
  absent is always on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

SECONDS_PER_DAY = 86400

__all__ = ["Due", "PlanningSchedule", "on_shift", "seconds_of_day", "steps_per_day"]


def steps_per_day(step_seconds: int) -> int:
    """Steps in one elapsed day (at least 1)."""
    return max(1, SECONDS_PER_DAY // max(1, int(step_seconds)))


def seconds_of_day(time_step: int, step_seconds: int) -> int:
    """Seconds into the ELAPSED day of ``time_step``."""
    return (int(time_step) * int(step_seconds)) % SECONDS_PER_DAY


@dataclass(frozen=True)
class Due:
    """One planning-due step: why, which boundary, which run day, and how late."""

    reason: str  # "day_start" | "interval" (dispatch.tick.PLANNING_REASONS)
    boundary: int  # the boundary step being planned (<= the current step)
    day: int  # the run day of the current step (elapsed axis)
    step: int  # the current step
    late_steps: int  # step - boundary (0 when on time)


class PlanningSchedule:
    """Which planning boundaries have been planned; :meth:`due` / :meth:`mark`."""

    def __init__(self) -> None:
        self.last_day: Optional[int] = None  # the last day whose day-start plan ran
        self.last_boundary: Optional[int] = None  # the latest boundary (any kind) covered
        self.plans = 0
        self.late_plans = 0
        self.max_late_steps = 0

    def due(
        self,
        planning: Optional[Mapping[str, Any]],
        step: Any,
        *,
        step_seconds: Any,
        horizon_steps: Any = None,
    ) -> Optional[Due]:
        """The :class:`Due` for ``step`` under ``planning``, or ``None`` (not planning-due)."""
        if not planning or type(step) is not int or step < 0:
            return None
        try:
            horizon = None if horizon_steps is None else int(horizon_steps)
        except (TypeError, ValueError):
            horizon = None
        if horizon is not None and step >= horizon:
            return None
        spd = steps_per_day(int(step_seconds or 0) or 1)
        day = step // spd
        if planning.get("at_day_start") and (self.last_day is None or day > self.last_day):
            boundary = day * spd
            return Due("day_start", boundary, day, step, step - boundary)
        every = planning.get("every_steps")
        if every:
            boundary = (step // int(every)) * int(every)
            if self.last_boundary is None or boundary > self.last_boundary:
                return Due("interval", boundary, day, step, step - boundary)
        return None

    def mark(self, due: Due, planning: Mapping[str, Any], *, step_seconds: Any) -> None:
        """Record that ``due`` was planned: every boundary at or before its step is covered."""
        spd = steps_per_day(int(step_seconds or 0) or 1)
        latest = []
        if planning.get("at_day_start"):
            self.last_day = due.day if self.last_day is None else max(self.last_day, due.day)
            latest.append(due.day * spd)
        every = planning.get("every_steps")
        if every:
            latest.append((due.step // int(every)) * int(every))
        latest.append(due.boundary)
        top = max(latest)
        self.last_boundary = top if self.last_boundary is None else max(self.last_boundary, top)
        self.plans += 1
        if due.late_steps > 0:
            self.late_plans += 1
            self.max_late_steps = max(self.max_late_steps, due.late_steps)


def on_shift(record: Optional[Mapping[str, Any]], haulier_id: Any, second_of_day: int) -> bool:
    """Whether ``haulier_id`` is inside its working hours at ``second_of_day`` (elapsed axis).

    ``record`` is the compiled ``profile["working_hours"]``; no record, or a haulier it
    does not name, is always on. ``[start, end)``; ``end < start`` wraps midnight.
    """
    if not record:
        return True
    window = (record.get("hauliers") or {}).get(haulier_id) if haulier_id is not None else None
    if not window:
        return True
    start, end = int(window[0]), int(window[1])
    s = int(second_of_day) % SECONDS_PER_DAY
    if start == end:  # refused at compile (R9); read as always on rather than never
        return True
    if start < end:
        return start <= s < end
    return s >= start or s < end
