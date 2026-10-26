"""Order outcomes: released / done same day / done later / undone at day end / failed by reason.

These are **SIM KPIs**, computed for every run regardless of the solver, so arms are
comparable on them. No planner may re-derive them (a planner's run stats are
planner-only; see ``docs/policy_authoring.md`` §14).

Definitions (all on the ELAPSED axis from ``REFERENCE_TIME``, parsed as naive UTC --
CLAUDE.md §6.20: never local time):

* ``t_rel = meta.request_time_step * STEP_INTERVAL`` -- the scenario's NOMINAL release,
  identical for every arm. Release day ``d = floor(t_rel / 86400)``; day n is
  ``[n*86400, (n+1)*86400)``, the same as the dashboard's day markers.
* ``t_done = order.sim_clock - REFERENCE_TIME`` for ``state == completed`` with a BSON date
  ``sim_clock`` (``completed`` is hard-terminal, so it never moves). Any other type is
  ``completed_untimed`` and fails an invariant.
* Day-end deadline ``min((d+1)*86400, H)`` with ``H`` the horizon, strict ``<``.
  ``done_same_day``: completed with ``t_done < deadline``; ``done_later``: completed at or
  after it (post-horizon drain completions included).
* ``undone_at_day_end = released - done_same_day`` (= done_later + failed + open).
  **failed is a SUBSET of undone_at_day_end** for the same release day -- never add them.
* ``failed`` = ``state == cancelled``, partitioned exhaustively, first match wins:

  1. ``cancelled_in_progress`` -- the order reached a truck: the doc has a non-null
     ``truck``, or ``str(order._id)`` is the ``order`` of a haul trip that is NOT
     completed (``haul_trip.order`` is a STRING, ``order._id`` an ObjectId). Sub-counts:
     ``started`` (that haul carries a reposition/pickup-queue/loaded start stamp) and
     ``at_horizon`` (doc ``sim_clock >= REF + H``, the ``unserved_at_horizon`` marker, or
     that haul's own ``sim_clock >= REF + H``: truck close + bulk cancel). On a default run (cancel probabilities 0) EVERY one of
     these is a horizon artefact.
  2. ``overdue_unassigned`` -- the order-lifecycle sweep's marker (``SWEEP_CANCEL_REASON``).
  3. ``unserved_at_horizon`` -- ``order/bulk_cancel.py``'s marker (``HORIZON_CANCEL_REASON``).
  4. ``other`` -- anything else. Runs from before the bulk-cancel marker existed land their
     unserved-at-horizon cancels HERE (they were never stamped); they are not re-guessed.

* Attribution per haulier is the ORDER's OWNER (``profile.haulier_id``) -- NOT the carrying
  truck's haulier that the haulier row's ``num_orders_completed`` counts.

Live values are PROVISIONAL: a sweep cancel can be resurrected by a real haul, so a live
failed count can FALL. The authoritative recount runs at finalize, after
``_finalize_unserved_orders``, when nothing can resurrect anything any more.

Cost: one ``$group`` aggregate over the run's orders (~100-200 rows), one small haul-trip
``find`` (non-completed hauls only) and one ``find`` bounded by that haul list. Direct
Mongo, read-only, ``connect=False`` -- the same data path the order-lifecycle service
writes through (there is no grouping REST endpoint, and adding an Eve aggregate would be
guarded by the manager's GLOBAL ``_aggregate_counts_enabled`` flag).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

from apps.container_logistics.order.bulk_cancel import HORIZON_CANCEL_REASON
from apps.container_logistics.order_lifecycle.batch_writer import SWEEP_CANCEL_REASON

logger = logging.getLogger(__name__)

__all__ = [
    "DAY_SECONDS",
    "FAILURE_REASONS",
    "HORIZON_CANCEL_REASON",
    "META_KEY",
    "SCHEMA",
    "SWEEP_CANCEL_REASON",
    "OrderOutcomeReader",
    "classify",
    "closed_days",
    "compute_order_outcomes",
    "done_fields",
    "fold_outcomes",
    "haulier_row_fields",
    "parse_reference_time",
    "record_from_doc",
    "scalars_from_outcomes",
]

SCHEMA = 1
DAY_SECONDS = 86400
#: ``run_config.meta.<META_KEY>`` -- reserved in ``policies/dispatch/run_stats.py``.
META_KEY = "order_outcomes"

IN_PROGRESS = "cancelled_in_progress"
OVERDUE = SWEEP_CANCEL_REASON  # "overdue_unassigned"
UNSERVED = HORIZON_CANCEL_REASON  # "unserved_at_horizon"
OTHER = "other"
#: Every failure reason, in precedence order. ``sum(by_reason) == failed.total`` always.
FAILURE_REASONS = (IN_PROGRESS, OVERDUE, UNSERVED, OTHER)

_COMPLETED = "completed"
_CANCELLED = "cancelled"
_CREATED = "created"
_ORDER_COLLECTION = "container_logistics_order"
_HAUL_COLLECTION = "container_logistics_haul_trip"
_START_STAMPS = ("reposition_started_at", "pickup_queue_arrival_time", "loaded_started_at")


# --------------------------------------------------------------------------- time axis


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_reference_time(value: Any) -> datetime:
    """``REFERENCE_TIME`` as a NAIVE UTC datetime (never local time, CLAUDE.md §6.20).

    The value is zone-less (``"2020-01-01 08:00:00"``) and means UTC. ``datetime``
    inputs are normalized (aware -> UTC). Raises ``ValueError`` for anything else.
    """
    if isinstance(value, datetime):
        return _naive_utc(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
        try:
            return _naive_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            pass
        try:
            return _naive_utc(parsedate_to_datetime(text))
        except (TypeError, ValueError):
            pass
    raise ValueError(f"cannot read REFERENCE_TIME from {value!r}")


def _as_time(value: Any) -> Optional[datetime]:
    """A stored/passed clock as naive UTC, or ``None`` (an RFC-1123 string is accepted)."""
    if isinstance(value, datetime):
        return _naive_utc(value)
    if isinstance(value, str):
        try:
            return parse_reference_time(value)
        except ValueError:
            return None
    return None


def days_count(horizon_s: float) -> int:
    return max(1, int(math.ceil(float(horizon_s) / DAY_SECONDS)))


def closed_days(elapsed_s: float, horizon_s: float) -> int:
    """How many run days have ENDED at ``elapsed_s``. Day d ends at ``min((d+1)*86400, H)``."""
    if elapsed_s >= horizon_s:
        return days_count(horizon_s)
    return max(0, int(elapsed_s // DAY_SECONDS))


def done_fields(t_done_s: float, t_rel_s: float, horizon_s: float) -> Dict[str, Any]:
    """The completion fields the Mongo pipeline groups on, computed the same way in Python.

    ``dd`` = completion day (floor), ``ph`` = completed at/after the horizon,
    ``e`` = completed BEFORE its own release (an invariant violation).
    """
    return {
        "dd": int(t_done_s // DAY_SECONDS),
        "ph": t_done_s >= horizon_s,
        "e": t_done_s < t_rel_s,
    }


def _done_same_day(rel_day: int, dd: int, ph: bool, horizon_s: float) -> bool:
    """Completed strictly before ``min((rel_day+1)*86400, H)``.

    ``dd <= rel_day`` encodes ``t_done < (rel_day+1)*86400`` (strict: a completion at
    exactly the boundary floors to the NEXT day). On a partial last day the deadline is
    ``H`` itself, which ``ph`` (``t_done >= H``) decides.
    """
    if dd > rel_day:
        return False
    if ph and (rel_day + 1) * DAY_SECONDS > horizon_s:
        return False
    return True


# --------------------------------------------------------------------------- classification


def classify(
    *,
    state: Any,
    cancel_reason: Any = None,
    has_truck: bool = False,
    has_haul: bool = False,
) -> Optional[str]:
    """The failure reason of one order, ``None`` when it is not failed (not cancelled).

    First match wins. Haul evidence ranks above the sweep marker on purpose: a real
    assignment outranks advisory demand-shedding (batch_writer's resurrection rule). A
    resurrected doc carries ``cancel_reason: None`` and is never read as overdue.
    """
    if state != _CANCELLED:
        return None
    if has_truck or has_haul:
        return IN_PROGRESS
    if cancel_reason == OVERDUE:
        return OVERDUE
    if cancel_reason == UNSERVED:
        return UNSERVED
    return OTHER


def _haul_evidence(
    hauls: Iterable[Mapping[str, Any]],
    *,
    reference_time: Optional[datetime] = None,
    horizon_s: Optional[float] = None,
) -> Dict[str, Dict[str, bool]]:
    """``str(order id) -> {"started", "at_horizon"}`` over NON-completed haul trips.

    The join key is ``str()`` on purpose: ``haul_trip.order`` is a STRING while
    ``order._id`` is an ObjectId, so an un-stringified join matches nothing.
    """
    out: Dict[str, Dict[str, bool]] = {}
    for h in hauls:
        if h.get("state") == _COMPLETED:
            continue
        oid = h.get("order")
        if oid is None or oid == "":
            continue
        stats = h.get("stats") or {}
        started = any(stats.get(k) not in (None, 0, "") for k in _START_STAMPS)
        clock = h.get("sim_clock")
        at_h = (
            reference_time is not None
            and horizon_s is not None
            and isinstance(clock, datetime)
            and (_naive_utc(clock) - reference_time).total_seconds() >= horizon_s
        )
        ev = out.setdefault(str(oid), {"started": False, "at_horizon": False})
        ev["started"] = ev["started"] or started
        ev["at_horizon"] = ev["at_horizon"] or bool(at_h)
    return out


def record_from_doc(
    doc: Mapping[str, Any],
    *,
    reference_time: datetime,
    step_interval: float,
    horizon_s: float,
    haul: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One order doc -> one fold record (``n == 1``), the per-doc twin of the pipeline row."""
    profile = doc.get("profile") or {}
    meta = doc.get("meta") or {}
    state = doc.get("state")
    reason = meta.get("cancel_reason")
    rts = meta.get("request_time_step")
    t_rel = None if rts is None else float(rts) * float(step_interval)
    rec: Dict[str, Any] = {
        "h": profile.get("haulier_id"),
        "hn": profile.get("haulier_name"),
        "rd": None if t_rel is None else int(t_rel // DAY_SECONDS),
        "s": state,
        "r": reason,
        "t": doc.get("truck") is not None,
        "haul": haul is not None,
        "started": bool(haul and haul.get("started")),
        "dd": None,
        "ph": None,
        "e": None,
        "ah": False,
        "n": 1,
    }
    clock = doc.get("sim_clock")
    if state == _COMPLETED and isinstance(clock, datetime) and t_rel is not None:
        rec.update(done_fields((_naive_utc(clock) - reference_time).total_seconds(), t_rel, horizon_s))
    if state == _CANCELLED:
        horizon_at = isinstance(clock, datetime) and (
            (_naive_utc(clock) - reference_time).total_seconds() >= horizon_s
        )
        rec["ah"] = bool(horizon_at or reason == UNSERVED or (haul and haul.get("at_horizon")))
    return rec


# --------------------------------------------------------------------------- fold


def _blank_failed() -> Dict[str, int]:
    return {"total": 0, **{r: 0 for r in FAILURE_REASONS}}


def _blank_day() -> Dict[str, Any]:
    return {
        "released": 0,
        "done_same_day": 0,
        "done_later": 0,
        "undone_at_day_end": 0,
        "open": 0,
        "failed": _blank_failed(),
    }


def _blank_totals() -> Dict[str, Any]:
    t = _blank_day()
    t.update(
        {
            "completed_untimed": 0,
            "early_completions": 0,
            "cancelled_in_progress_started": 0,
            "cancelled_in_progress_at_horizon": 0,
        }
    )
    return t


def _add_into(dst: Dict[str, Any], src: Mapping[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict):
            _add_into(dst.setdefault(k, {}), v)
        elif k in dst or isinstance(v, (int, float)):
            dst[k] = dst.get(k, 0) + v


def _invariant_errors(label: str, t: Mapping[str, Any]) -> List[str]:
    errs: List[str] = []
    f = t["failed"]
    lhs = t["done_same_day"] + t["done_later"] + f["total"] + t["open"]
    if t["released"] != lhs:
        errs.append(
            f"{label}: released {t['released']} != done_same_day + done_later + failed + open = {lhs}"
        )
    if t["undone_at_day_end"] != t["released"] - t["done_same_day"]:
        errs.append(f"{label}: undone_at_day_end != released - done_same_day")
    if sum(f[r] for r in FAILURE_REASONS) != f["total"]:
        errs.append(f"{label}: failed reasons do not sum to failed.total")
    return errs


def fold_outcomes(
    records: Iterable[Mapping[str, Any]],
    *,
    horizon_s: float,
    step_interval: float,
    reference_time: Any,
    final: bool,
    closed: Optional[int] = None,
) -> Dict[str, Any]:
    """Pure fold of order records into the ``run_config.meta.order_outcomes`` shape.

    ``final=False`` (live) counts only release days that have CLOSED (``d < closed``); a
    closed day's done_same_day / undone_at_day_end never change afterwards (a later
    completion turns undone into done_later, and it stays undone at its day end).
    """
    if not final and closed is None:
        raise ValueError("a live fold needs `closed` (the number of closed run days)")
    ref_label = reference_time if isinstance(reference_time, str) else (
        _naive_utc(reference_time).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(reference_time, datetime) else str(reference_time)
    )
    totals = _blank_totals()
    days: Dict[int, Dict[str, Any]] = {}
    by_h: Dict[str, Dict[str, Any]] = {}
    release_unknown = 0

    for rec in records:
        n = int(rec.get("n", 1))
        if n <= 0:
            continue
        state = rec.get("s")
        if state in (None, _CREATED):
            continue  # not released
        rd = rec.get("rd")
        if rd is None:
            release_unknown += n
            continue
        rd = int(rd)
        if not final and rd >= closed:
            continue
        hid = rec.get("h")
        hkey = "_unattributed" if hid in (None, "") else str(hid)
        h = by_h.get(hkey)
        if h is None:
            h = by_h[hkey] = {"haulier_name": rec.get("hn"), **_blank_totals(), "days": {}}
        elif h.get("haulier_name") is None and rec.get("hn") is not None:
            h["haulier_name"] = rec.get("hn")
        delta = _blank_totals()
        delta["released"] = n
        if state == _COMPLETED:
            if rec.get("dd") is None:
                delta["completed_untimed"] = n
            else:
                if rec.get("e"):
                    delta["early_completions"] = n
                if _done_same_day(rd, int(rec["dd"]), bool(rec.get("ph")), horizon_s):
                    delta["done_same_day"] = n
                else:
                    delta["done_later"] = n
        elif state == _CANCELLED:
            reason = classify(
                state=state,
                cancel_reason=rec.get("r"),
                has_truck=bool(rec.get("t")),
                has_haul=bool(rec.get("haul")),
            )
            delta["failed"]["total"] = n
            delta["failed"][reason] = n
            if reason == IN_PROGRESS:
                if rec.get("started"):
                    delta["cancelled_in_progress_started"] = n
                if rec.get("ah"):
                    delta["cancelled_in_progress_at_horizon"] = n
        else:
            delta["open"] = n
        delta["undone_at_day_end"] = n - delta["done_same_day"]

        day_delta = {k: delta[k] for k in _blank_day()}
        _add_into(totals, delta)
        _add_into(h, delta)
        _add_into(days.setdefault(rd, _blank_day()), day_delta)
        _add_into(h["days"].setdefault(rd, _blank_day()), day_delta)

    errors: List[str] = []
    errors += _invariant_errors("totals", totals)
    for d, row in sorted(days.items()):
        errors += _invariant_errors(f"d{d}", row)
    for hkey, h in sorted(by_h.items()):
        errors += _invariant_errors(f"haulier {hkey}", h)
    if totals["early_completions"]:
        errors.append(f"{totals['early_completions']} completion(s) earlier than their own release")
    if totals["completed_untimed"]:
        errors.append(f"{totals['completed_untimed']} completed order(s) with no date sim_clock")
    if release_unknown:
        errors.append(f"{release_unknown} released order(s) with no meta.request_time_step")
    if final and totals["open"]:
        errors.append(
            f"{totals['open']} order(s) still open at the final recount "
            "(the unserved-orders bulk cancel did not run or did not finish)"
        )

    d_count = days_count(horizon_s)
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "final": bool(final),
        "axis": "elapsed",
        "axis_origin": ref_label,
        "day_seconds": DAY_SECONDS,
        "day_seconds_last": int(horizon_s - (d_count - 1) * DAY_SECONDS),
        "step_interval": int(step_interval) if float(step_interval).is_integer() else step_interval,
        "horizon_s": int(horizon_s) if float(horizon_s).is_integer() else horizon_s,
        "days_count": d_count,
        "attribution": "owner",
        "totals": totals,
        "release_unknown": release_unknown,
        "days": {f"d{d}": days[d] for d in sorted(days)},
        "by_haulier": {
            hkey: {**{k: v for k, v in h.items() if k != "days"},
                   "days": {f"d{d}": h["days"][d] for d in sorted(h["days"])}}
            for hkey, h in sorted(by_h.items())
        },
        "invariants_ok": not errors,
        "invariant_errors": errors,
    }
    if not final:
        out["closed_days"] = int(closed)
    return out


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def scalars_from_outcomes(outcomes: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The three closed-day scalar KPIs (0 until day 0 has closed)."""
    t = (outcomes or {}).get("totals") or {}
    same = int(t.get("done_same_day", 0))
    rel = int(t.get("released", 0))
    return {
        "num_orders_done_same_day_total": same,
        "num_orders_undone_at_day_end_total": int(t.get("undone_at_day_end", 0)),
        "same_day_completion_rate": _rate(same, rel),
    }


def haulier_row_fields(outcomes: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``haulier_id -> kpi_breakdown haulier-row fields`` (OWNER attribution).

    Named ``orders_*`` so they cannot be confused with the row's carrier-based
    ``num_orders_completed``.
    """
    final = bool(outcomes.get("final"))
    out: Dict[str, Dict[str, Any]] = {}
    for hid, h in (outcomes.get("by_haulier") or {}).items():
        f = h.get("failed") or {}
        out[hid] = {
            "orders_released": h.get("released", 0),
            "orders_done_same_day": h.get("done_same_day", 0),
            "orders_done_later": h.get("done_later", 0),
            "orders_undone_at_day_end": h.get("undone_at_day_end", 0),
            "orders_failed": f.get("total", 0),
            "orders_failed_overdue_unassigned": f.get(OVERDUE, 0),
            "orders_failed_cancelled_in_progress": f.get(IN_PROGRESS, 0),
            "orders_failed_unserved_at_horizon": f.get(UNSERVED, 0),
            "orders_failed_other": f.get(OTHER, 0),
            "same_day_completion_rate": _rate(h.get("done_same_day", 0), h.get("released", 0)),
            "order_outcomes_final": final,
        }
    return out


def zero_haulier_row_fields(final: bool) -> Dict[str, Any]:
    """Fields for a haulier that owns no order in the counted days (a real zero)."""
    return {
        "orders_released": 0,
        "orders_done_same_day": 0,
        "orders_done_later": 0,
        "orders_undone_at_day_end": 0,
        "orders_failed": 0,
        "orders_failed_overdue_unassigned": 0,
        "orders_failed_cancelled_in_progress": 0,
        "orders_failed_unserved_at_horizon": 0,
        "orders_failed_other": 0,
        "same_day_completion_rate": 0.0,
        "order_outcomes_final": bool(final),
    }


# --------------------------------------------------------------------------- Mongo reader


def _mongo_client(server_selection_timeout_ms: int = 3000):
    """Same construction as batch_writer (``connect=False``), with a SHORT server-selection
    timeout: this runs on the analytics tick and must never stall it for pymongo's 30 s."""
    from pymongo import MongoClient

    from apps.config import kpi_sink_settings

    uri = kpi_sink_settings.get("mongo_uri")
    if uri:
        return MongoClient(uri, connect=False, serverSelectionTimeoutMS=server_selection_timeout_ms)
    return MongoClient(
        kpi_sink_settings["mongo_host"],
        int(kpi_sink_settings["mongo_port"]),
        connect=False,
        serverSelectionTimeoutMS=server_selection_timeout_ms,
    )


def _as_object_id(value: Any) -> Any:
    from bson import ObjectId

    try:
        return ObjectId(str(value))
    except Exception:
        return value


class OrderOutcomeReader:
    """Read-only Mongo reader. One client PER CALL (created and closed in ``read``), so
    nothing leaks across runs in a long-lived celery worker."""

    def __init__(self, run_id: str, *, client_factory=None, db_name: Optional[str] = None):
        self.run_id = run_id
        self._client_factory = client_factory or _mongo_client
        self._db_name = db_name

    def _db(self, client):
        if self._db_name is None:
            from apps.config import kpi_sink_settings

            self._db_name = kpi_sink_settings["mongo_db"]
        return client[self._db_name]

    def pipeline(
        self, *, reference_time: datetime, step_interval: float, horizon_s: float, exclude_ids: List[Any]
    ) -> List[Dict[str, Any]]:
        """``$match`` on ``run_id`` FIRST (index prefix), then one ``$group``.

        The completion fields mirror :func:`done_fields` exactly (ms arithmetic).
        """
        step_ms = float(step_interval) * 1000.0
        day_ms = DAY_SECONDS * 1000.0
        h_ms = float(horizon_s) * 1000.0
        is_date = {"$eq": [{"$type": "$sim_clock"}, "date"]}
        t_ms = {"$subtract": ["$sim_clock", reference_time]}
        t_rel_ms = {"$multiply": ["$meta.request_time_step", step_ms]}
        done = {"$and": [{"$eq": ["$state", _COMPLETED]}, is_date]}
        cancelled = {"$eq": ["$state", _CANCELLED]}
        match: Dict[str, Any] = {"run_id": self.run_id, "state": {"$ne": _CREATED}}
        if exclude_ids:
            match["$nor"] = [{"state": _CANCELLED, "_id": {"$in": exclude_ids}}]
        return [
            {"$match": match},
            {
                "$group": {
                    "_id": {
                        "h": "$profile.haulier_id",
                        "hn": "$profile.haulier_name",
                        "rd": {
                            "$cond": [
                                {"$eq": [{"$ifNull": ["$meta.request_time_step", None]}, None]},
                                None,
                                {"$floor": {"$divide": [t_rel_ms, day_ms]}},
                            ]
                        },
                        "s": "$state",
                        "r": {"$ifNull": ["$meta.cancel_reason", None]},
                        "t": {"$ne": [{"$ifNull": ["$truck", None]}, None]},
                        "dd": {"$cond": [done, {"$floor": {"$divide": [t_ms, day_ms]}}, None]},
                        "ph": {"$cond": [done, {"$gte": [t_ms, h_ms]}, None]},
                        "e": {"$cond": [done, {"$lt": [t_ms, t_rel_ms]}, None]},
                        "ah": {
                            "$cond": [
                                cancelled,
                                {
                                    "$or": [
                                        {"$and": [is_date, {"$gte": [t_ms, h_ms]}]},
                                        {"$eq": [{"$ifNull": ["$meta.cancel_reason", None]}, UNSERVED]},
                                    ]
                                },
                                False,
                            ]
                        },
                    },
                    "n": {"$sum": 1},
                }
            },
        ]

    def read(
        self,
        *,
        reference_time: datetime,
        step_interval: float,
        horizon_s: float,
        per_doc: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fold records for this run. ``per_doc=True`` folds every order doc in Python
        instead of grouping in Mongo -- a cross-check of the pipeline, not for production."""
        client = self._client_factory()
        try:
            db = self._db(client)
            hauls = list(
                db[_HAUL_COLLECTION].find(
                    {"run_id": self.run_id, "state": {"$ne": _COMPLETED}, "order": {"$ne": None}},
                    {"order": 1, "state": 1, "sim_clock": 1, **{f"stats.{k}": 1 for k in _START_STAMPS}},
                )
            )
            evidence = _haul_evidence(hauls, reference_time=reference_time, horizon_s=horizon_s)
            projection = {
                "profile.haulier_id": 1,
                "profile.haulier_name": 1,
                "meta.request_time_step": 1,
                "meta.cancel_reason": 1,
                "state": 1,
                "truck": 1,
                "sim_clock": 1,
            }
            kw = dict(reference_time=reference_time, step_interval=step_interval, horizon_s=horizon_s)
            if per_doc:
                return [
                    record_from_doc(doc, haul=evidence.get(str(doc["_id"])), **kw)
                    for doc in db[_ORDER_COLLECTION].find(
                        {"run_id": self.run_id, "state": {"$ne": _CREATED}}, projection
                    )
                ]
            oids = [_as_object_id(o) for o in evidence]
            records: List[Dict[str, Any]] = []
            if oids:
                # Cancelled orders a non-completed haul points at: classified per doc (bounded
                # by the haul list), and EXCLUDED from the group below so none counts twice.
                for doc in db[_ORDER_COLLECTION].find(
                    {"run_id": self.run_id, "state": _CANCELLED, "_id": {"$in": oids}}, projection
                ):
                    records.append(record_from_doc(doc, haul=evidence.get(str(doc["_id"])), **kw))
            for row in db[_ORDER_COLLECTION].aggregate(
                self.pipeline(exclude_ids=oids, **kw), allowDiskUse=False
            ):
                key = dict(row["_id"])
                rec = {
                    "h": key.get("h"),
                    "hn": key.get("hn"),
                    "rd": None if key.get("rd") is None else int(key["rd"]),
                    "s": key.get("s"),
                    "r": key.get("r"),
                    "t": bool(key.get("t")),
                    "haul": False,
                    "started": False,
                    "dd": None if key.get("dd") is None else int(key["dd"]),
                    "ph": key.get("ph"),
                    "e": key.get("e"),
                    "ah": bool(key.get("ah")),
                    "n": int(row.get("n", 0)),
                }
                records.append(rec)
            return records
        finally:
            client.close()


def compute_order_outcomes(
    run_id: str,
    *,
    step_interval: float,
    reference_time: Any,
    horizon_s: float,
    final: bool,
    closed: Optional[int] = None,
    reader: Optional[OrderOutcomeReader] = None,
    per_doc: bool = False,
) -> Dict[str, Any]:
    """Read + fold. Raises on a read failure -- callers decide how to contain it."""
    ref = parse_reference_time(reference_time)
    reader = reader or OrderOutcomeReader(run_id)
    records = reader.read(
        reference_time=ref, step_interval=step_interval, horizon_s=horizon_s, per_doc=per_doc
    )
    return fold_outcomes(
        records,
        horizon_s=horizon_s,
        step_interval=step_interval,
        reference_time=reference_time if isinstance(reference_time, str) else ref,
        final=final,
        closed=closed,
    )
