"""Day-plan instance: the plain-data input of one planning solve (plan §4 ``instance.py``).

Single responsibility: turn the documents a planning tick carries (online trucks, the
engaged order of every busy truck, the day's orders incl. foresight) plus the itinerary
store's state into a JSON-able :class:`PlanInputs` the solve child consumes. It takes
PLAIN DATA, never a tick object, so the wrapper adapts whatever tick type the framework
hands it. Solver helper (``policies/solver_lib``); the framework never imports it.

Not a leaf module: imports ``assignment.constraints`` (the physical-feasibility rules the
framework itself applies) at top, like ``solver_lib.candidates``.

Rules (plan §4, design §6a, critique M11 / D3 / D5 / D6 / D8 / D9 / D12f):

- **classes** -- the commodity of the network model is a truck CLASS
  ``(haulier, truck_size, restricted_areas)`` (D12f); ``class_attrs`` = ``[size, areas,
  shift]`` is what :func:`coop_dispatch.min_cost_flow.mcf_applicable` compares (M5). A job
  owner whose haulier has no truck in the instance gets a vehicle-less SYNTHETIC class, so
  every job has an owner class (the library's ``order_owner`` must be a company).
- **time axis** -- seconds since REFERENCE_TIME (the ELAPSED axis, D5). Working hours are
  ``{"axis": "elapsed", "hauliers": {hid: [start_s, end_s]}}`` (seconds of the elapsed
  day, overnight allowed); a haulier absent from it is always on. A class's ``windows``
  are every window of its hours that ends after ``now_s`` and starts before
  ``window_end_s`` (``shift`` = the first); a class with none is off for this plan (its
  trucks are left out). An overnight shift gives two windows in one plan (minor 6).
- **truck start** -- ``ready = max(now, first usable window start, predicted end of the job
  it is locked into)``; position = that job's dropoff, else the truck's position
  (``constraints.truck_anchor_loc``). The locked job is the ENGAGED order (open haul) or
  else the last ``dispatched`` job of its itinerary in the store (dispatched, not yet
  picked up). Every online truck is offered (D6), not only free ones. Each LATER window
  is a second start ``{truck}@w{k}`` (``of`` = the truck, ready at the window start, at
  the same anchor); the child appends its chain to the truck's.
- **chain propagation** (M3, ``propagate_chains``, OFF by default: it cuts re-plan moves
  ~45 % but costs ~6.5 % of the jobs done, see :class:`DayPlanParams`) -- on a same-day
  re-plan, each truck's
  still-``planned`` jobs of the previous version are walked from the truck's ready time:
  a job's release becomes ``max(r, bound)`` (fitted to the shift), the bound being what the
  arc rule needs -- ``ready - s_head`` for the head, ``e_i - s_i`` after job ``i`` -- and
  its end never moves earlier. A late head's EXCESS lateness flows down the chain, the old
  chain stays a feasible set of arcs, and an unchanged world changes nothing. A job pushed
  more than ``propagate_max_push_min`` past its own start ends the walk (the rest of that
  chain is re-planned freely).
- **jobs** -- orders whose document state is ``created`` / ``unassigned``, not locked in
  the store (``dispatched`` / ``done``), not failed terminally (``cancelled`` /
  ``taken``), with ``request_step * step_seconds < window_end_s``. Release
  ``r = max(request_s, now_s)`` (a job cannot be dispatched before now, D3), end
  ``e = time_model.job_end_seconds(r, ...)`` (release + dispatch latency + predicted
  duration), late slack per pickup/dropoff type pair
  (``time_model.late_slack_seconds``) unless a fixed ``late_slack_min`` is given.
- **eligibility** of (job, class): the class's haulier owns the job or the cooperation arm
  lets it serve the owner (``owner in arm[carrier]``, exactly ``share_eligible``),
  physically allowed (``size_compatible`` and not ``restricted_area_blocks`` against a
  truck of the class), and fitting one of the class's windows: started at
  ``max(r, window start)`` (M4: an order released before the shift waits for it) it ends
  by the window end. The job's ``r`` is the earliest such start over its eligible
  classes, ``e`` the end from it. A job is dropped altogether when its overdue-sweep deadline
  (``(request_step + max_wait_steps) * step_seconds``, the order lifecycle's
  ``UNASSIGNED_ORDER_MAX_WAIT_STEPS``) is before its planned dispatch plus one cadence
  (M11), or when no class may serve it. Dropped jobs are listed in ``excluded`` with a
  reason; they are not part of the solve.
- **order independence** -- every list is sorted by id and every map is keyed, so the
  same documents in any order give byte-identical inputs and the same hash.
- **hash** (skip-if-unchanged) -- over the truck set (id, class, locked job, position
  bucketed to ``hash_position_decimals``), the open-job set with each job's eligible
  classes and any failure status, the excluded set, the cooperation and the params. It
  deliberately leaves out ``now_s`` and every time derived from it, the statuses the
  previous plan itself wrote (``planned`` / ``unplanned``) and the store's version id,
  all of which change on every tick or every write and would make "unchanged" impossible.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Iterable, Mapping, Optional, Sequence

from apps.container_logistics.assignment import constraints as assign_constraints

from .time_model import TimeModel, haversine_km, latency_seconds

__all__ = [
    "DAY_SECONDS",
    "DayPlanParams",
    "INPUTS_SCHEMA",
    "OPEN_ORDER_STATES",
    "PlanInputs",
    "build_inputs",
    "class_id",
    "inputs_hash",
    "store_job_fields",
]

INPUTS_SCHEMA = 1
DAY_SECONDS = 86400
#: Order document states a job may still be planned in.
OPEN_ORDER_STATES = frozenset({"created", "unassigned"})
#: Store statuses that lock a job onto its truck (excluded from the solve).
_LOCKED = frozenset({"dispatched", "done"})
#: ``failed`` reasons after which the order can never be served by the plan again.
_TERMINAL_FAIL_REASONS = frozenset({"cancelled", "taken"})
_VERSION_RE = re.compile(r"^d(\d+)v\d+$")


@dataclass(frozen=True)
class DayPlanParams:
    """Wrapper knobs the instance and the child read (plain values; a mapping works too).

    Defaults follow plan §4 as amended by N0 (orchestrator 2026-10-25): no K cap on the
    exact min-cost-flow path, K <= 10 only on the MIP fallback, 240 min chain wait, a
    50 m stability bonus on the previous version's arcs.
    """

    wait_limit_min: float = 240.0
    late_slack_min: Optional[float] = None      # None: per type pair from the time model
    defer_penalty_km: float = 200.0
    stability_bonus_m: float = 50.0
    k_succ: Optional[int] = None                # min-cost-flow path
    k_start: Optional[int] = None
    mip_k_succ: Optional[int] = 10              # MIP fallback path
    mip_k_start: Optional[int] = 10
    mip_time_limit_s: float = 60.0
    mip_gap: Optional[float] = 0.01
    mip_arc_budget: int = 500_000
    guard_threshold: float = 0.95
    max_reposition_s: Optional[float] = None    # truck -> first job, travel seconds
    detour: float = 1.3                          # haversine x detour = planning km
    speed_kmh: float = 35.0                      # only for max_reposition_s
    replan_idle_trucks: bool = True             # D9
    # M3 (opt-in, a trade-off measured 2026-10-25 on real day 1, full arm, 06-20 hours, the
    # e2e with jobs ending at dispatch + predicted duration): off = 3,682 jobs moved / 1,907
    # done; on = 2,193 moved / 1,783 done (-6.5 %); capped at 10 / 30 / 90 min = 3,572 /
    # 3,147 / 2,257 moved, 1,898 / 1,839 / 1,789 done. The objective is distance only, so a
    # late truck that keeps its chain costs nothing in the model but service in the world.
    propagate_chains: bool = False              # old chains carry their excess lateness
    propagate_max_push_min: Optional[float] = None  # a chain pushed further is re-planned freely
    deadline_margin_steps: Optional[int] = None  # None: one dispatch cadence (M11)
    hash_position_decimals: int = 4             # ~11 m

    @classmethod
    def coerce(cls, value: "DayPlanParams | Mapping[str, Any] | None") -> "DayPlanParams":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"params must be DayPlanParams or a mapping (got {type(value).__name__})")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown day-plan params {unknown}; known: {sorted(known)}")
        return cls(**dict(value))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlanInputs(dict):
    """The JSON-able input of one planning solve (a ``dict``; ``json.dumps`` works as is).

    Keys: ``schema``, ``day``, ``now_s``, ``window_end_s``, ``step_seconds``,
    ``cadence_steps``, ``classes`` {cid: {haulier, size, restricted_areas, shift, windows,
    trucks, synthetic}}, ``class_attrs`` {cid: [size, areas, windows]}, ``cooperation`` [[owner_class,
    carrier_class]], ``components`` [[cid]], ``trucks`` [{id, class, ready_s, loc [lat, lng],
    anchor, anchor_job[, of, window]}], ``jobs`` [{id, owner, owner_class, classes, request_step, r_req,
    r, e, slack, deadline_s, pickup, dropoff, pickup_site, dropoff_site, first_day,
    new, status}], ``excluded`` {job: reason}, ``excluded_trucks`` {truck: reason},
    ``prev_arcs`` [[from, to]], ``params`` (``DayPlanParams.as_dict()``), ``hash``.
    """

    @property
    def hash(self) -> str:
        return self["hash"]

    @property
    def job_ids(self) -> list[str]:
        return [j["id"] for j in self["jobs"]]


# ------------------------------------------------------------------------- helpers


def _id(doc: Mapping[str, Any]) -> str:
    return str(doc.get("_id"))


def _latlng(point: Optional[Mapping[str, Any]]) -> Optional[list[float]]:
    """GeoJSON ``Point`` (lon, lat) -> ``[lat, lng]`` (the edge builder's order)."""
    if point is None:
        return None
    lon, lat = point["coordinates"][0], point["coordinates"][1]
    return [float(lat), float(lon)]


def _profile(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    return doc.get("profile") or {}


def _request_step(order: Mapping[str, Any]) -> Optional[int]:
    meta = order.get("meta") or {}
    step = meta.get("request_time_step")
    if step is None:
        step = order.get("request_time_step")
    if step is None or isinstance(step, bool):
        return None
    return int(step)


def _truck_size(truck: Mapping[str, Any]) -> str:
    return str(_profile(truck).get("truck_size", "20ft"))


def _areas(truck: Mapping[str, Any]) -> list[str]:
    raw = _profile(truck).get("restricted_areas") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return sorted({str(x) for x in raw})


def class_id(haulier: str, size: str, areas: Sequence[str]) -> str:
    """Stable class id ``haulier|size|area+area`` (areas sorted)."""
    return f"{haulier}|{size}|{'+'.join(sorted(areas))}"


def _synthetic_class_id(haulier: str) -> str:
    return f"{haulier}|*none*"


def _windows(hours: Optional[Sequence[float]], now_s: float, window_end_s: float
             ) -> tuple[bool, Optional[list[list[float]]]]:
    """``(on, windows)``: every window ``[start, end]`` of ``hours`` (elapsed seconds of day)
    that ends after ``now_s`` and starts before ``window_end_s``, earliest first (an
    overnight shift gives the plan two: the one running at day start and the evening one);
    ``(True, None)`` for a haulier without hours (always on); ``(False, None)`` when no
    window qualifies."""
    if hours is None:
        return True, None
    start, end = float(hours[0]), float(hours[1])
    length = (end - start) % DAY_SECONDS
    if length == 0:
        return True, None
    first = math.floor(now_s / DAY_SECONDS) - 1
    out = []
    for d in range(first, first + 4):
        s = d * DAY_SECONDS + start
        e = s + length
        if e > now_s and s < window_end_s:
            out.append([s, e])
    return (True, out) if out else (False, None)


def _shift(hours: Optional[Sequence[float]], now_s: float, window_end_s: float
           ) -> tuple[bool, Optional[list[float]]]:
    """``(on, [start, end])`` of the EARLIEST window :func:`_windows` finds (kept for callers
    that need one window)."""
    on, wins = _windows(hours, now_s, window_end_s)
    return on, (wins[0] if wins else None)


def _hours_of(working_hours: Optional[Mapping[str, Any]], haulier: str) -> Optional[list[float]]:
    if not working_hours:
        return None
    axis = working_hours.get("axis", "elapsed")
    if axis != "elapsed":
        raise ValueError(f"working_hours axis {axis!r} is not supported (only 'elapsed', plan D5)")
    h = (working_hours.get("hauliers") or {}).get(haulier)
    if h is None:
        return None
    if not isinstance(h, (list, tuple)) or len(h) != 2:
        raise ValueError(f"working_hours for {haulier!r} must be [start_s, end_s] (got {h!r})")
    return [float(h[0]), float(h[1])]


def _arm_adjacency(arm: Any) -> dict[str, frozenset[str]]:
    """``{carrier: frozenset(owners it may serve)}`` from plain data.

    Accepts ``None`` (own haulier only), a mapping ``{carrier: [owners]}`` (an arm's
    ``adjacency``), or an object with an ``adjacency`` attribute (``cooperation.Arm``)."""
    if arm is None:
        return {}
    adj = getattr(arm, "adjacency", arm)
    if not isinstance(adj, Mapping):
        raise TypeError(f"cooperation_arm must be None or a {{carrier: [owners]}} mapping (got {type(arm).__name__})")
    return {str(k): frozenset(str(x) for x in v) for k, v in adj.items()}


def _components(classes: Sequence[str], coop: Iterable[tuple[str, str]]) -> list[list[str]]:
    parent = {c: c for c in classes}

    def find(c: str) -> str:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for a, b in coop:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, list[str]] = {}
    for c in classes:
        groups.setdefault(find(c), []).append(c)
    return [sorted(g) for _, g in sorted(groups.items())]


def _first_day(job: str, store_cur: Mapping[str, Any] | None, status: Mapping[str, Any], day: int) -> int:
    if store_cur:
        f = (store_cur.get("jobs") or {}).get(job)
        if isinstance(f, Mapping) and isinstance(f.get("first_day"), int):
            return int(f["first_day"])
    rec = status.get(job)
    if rec and isinstance(rec.get("version"), str):
        m = _VERSION_RE.match(rec["version"])
        if m:
            return min(int(m.group(1)), day)
    return day


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def inputs_hash(inputs: Mapping[str, Any]) -> str:
    """``blake2b16:<hex>`` skip-if-unchanged hash (see the module docstring for what it covers)."""
    p = inputs["params"]
    nd = int(p.get("hash_position_decimals", 4))
    trucks = [[t["id"], t["class"], t["anchor_job"],
               [round(t["loc"][0], nd), round(t["loc"][1], nd)]] for t in inputs["trucks"]]
    jobs = [[j["id"], j["classes"], j.get("status")] for j in inputs["jobs"]]
    payload = {
        "schema": inputs["schema"], "day": inputs["day"], "trucks": trucks, "jobs": jobs,
        "excluded": inputs["excluded"], "excluded_trucks": inputs["excluded_trucks"],
        "cooperation": inputs["cooperation"], "params": p,
        "cadence_steps": inputs["cadence_steps"], "step_seconds": inputs["step_seconds"],
    }
    return "blake2b16:" + hashlib.blake2b(_canonical(payload).encode(), digest_size=16).hexdigest()


# --------------------------------------------------------------------------- build


def build_inputs(
    trucks: Sequence[Mapping[str, Any]],
    engaged: Mapping[str, Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    store_state: Optional[Mapping[str, Any]],
    time_model: TimeModel,
    params: "DayPlanParams | Mapping[str, Any] | None",
    *,
    now_s: float,
    day: int,
    window_end_s: float,
    step_seconds: float,
    cooperation_arm: Any,
    working_hours: Optional[Mapping[str, Any]],
    max_wait_steps: Optional[int],
    cadence_steps: int,
    dispatched_at_s: Optional[Mapping[str, float]] = None,
) -> PlanInputs:
    """Build the :class:`PlanInputs` of one planning solve (rules: module docstring).

    ``trucks`` -- raw truck documents (every online truck); ``engaged`` -- truck id -> the
    raw order document of its open haul; ``orders`` -- raw order documents (the day's
    orders incl. not-yet-released ones, plus still-open earlier ones); ``store_state`` --
    ``PlanStore.state()`` or ``None``; ``cooperation_arm`` -- ``None`` or
    ``{carrier: [owners]}`` (``cooperation.Arm`` works too); ``working_hours`` -- the
    compiled ``{"axis": "elapsed", "hauliers": {...}}`` record or ``None``;
    ``max_wait_steps`` -- ``UNASSIGNED_ORDER_MAX_WAIT_STEPS`` (``None``/0: no sweep);
    ``cadence_steps`` -- the dispatch cadence (latency model and deadline margin);
    ``dispatched_at_s`` -- optional order id -> elapsed seconds it was dispatched (a
    better end estimate for engaged / dispatched jobs than the release time).
    """
    p = DayPlanParams.coerce(params)
    now_s, window_end_s, step_seconds = float(now_s), float(window_end_s), float(step_seconds)
    day = int(day)
    if step_seconds <= 0 or cadence_steps < 1:
        raise ValueError("step_seconds must be > 0 and cadence_steps >= 1")
    dispatched_at_s = dict(dispatched_at_s or {})
    adjacency = _arm_adjacency(cooperation_arm)
    state = store_state or {}
    store_cur = state.get("current") or None
    status: Mapping[str, Mapping[str, Any]] = state.get("status") or {}
    same_day = bool(store_cur) and store_cur.get("day") == day
    latency = latency_seconds(cadence_steps, step_seconds)
    margin_steps = cadence_steps if p.deadline_margin_steps is None else int(p.deadline_margin_steps)

    orders_by_id: dict[str, Mapping[str, Any]] = {}
    for o in orders:
        oid = _id(o)
        if oid in orders_by_id:
            raise ValueError(f"order {oid} appears twice")
        orders_by_id[oid] = o

    def job_end(order: Mapping[str, Any], release_s: float) -> float:
        prof = _profile(order)
        pu, do = _latlng(assign_constraints.order_pickup_loc(order)), _latlng(assign_constraints.order_dropoff_loc(order))
        km = haversine_km(pu[1], pu[0], do[1], do[0]) if pu and do else 0.0
        return time_model.job_end_seconds(release_s, prof.get("pickup_facility_name"),
                                          prof.get("dropoff_facility_name"), km,
                                          cadence_steps=cadence_steps, step_seconds=step_seconds)

    # ---- classes -------------------------------------------------------------------------
    by_class: dict[str, list[Mapping[str, Any]]] = {}
    truck_class: dict[str, str] = {}
    seen_trucks: set[str] = set()
    for t in trucks:
        tid = _id(t)
        if tid in seen_trucks:
            raise ValueError(f"truck {tid} appears twice")
        seen_trucks.add(tid)
        h = assign_constraints.haulier_of(t)
        if h is None:
            continue
        cid = class_id(h, _truck_size(t), _areas(t))
        by_class.setdefault(cid, []).append(t)
        truck_class[tid] = cid
    for cid in by_class:
        by_class[cid].sort(key=_id)

    classes: dict[str, dict[str, Any]] = {}
    for cid in sorted(by_class):
        rep = by_class[cid][0]
        h = assign_constraints.haulier_of(rep)
        on, wins = _windows(_hours_of(working_hours, h), now_s, window_end_s)
        classes[cid] = {"haulier": h, "size": _truck_size(rep), "restricted_areas": _areas(rep),
                        "shift": (wins[0] if wins else None), "windows": wins, "on": on,
                        "trucks": [_id(t) for t in by_class[cid]], "synthetic": False}
    hauliers_with_class = {c["haulier"] for c in classes.values()}
    for o in orders_by_id.values():
        h = assign_constraints.haulier_of(o)
        if h is not None and h not in hauliers_with_class:
            classes.setdefault(_synthetic_class_id(h), {
                "haulier": h, "size": None, "restricted_areas": [], "shift": None, "windows": None,
                "on": False, "trucks": [], "synthetic": True})
    classes = dict(sorted(classes.items()))
    owner_class: dict[str, str] = {}
    for cid, c in classes.items():
        owner_class.setdefault(c["haulier"], cid)  # first (sorted) class of the haulier

    def may_serve(owner_h: str, carrier_h: str) -> bool:
        return owner_h == carrier_h or owner_h in adjacency.get(carrier_h, ())

    cooperation = sorted([a, b] for a in classes for b in classes
                         if may_serve(classes[a]["haulier"], classes[b]["haulier"]))
    components = _components(list(classes), [tuple(x) for x in cooperation])

    # ---- locked jobs and truck anchors ------------------------------------------------------
    locked: set[str] = {j for j, rec in status.items() if rec.get("status") in _LOCKED}
    store_itins: Mapping[str, Sequence[str]] = (store_cur or {}).get("itineraries") or {}
    store_jobs: Mapping[str, Mapping[str, Any]] = (store_cur or {}).get("jobs") or {}

    out_trucks: list[dict[str, Any]] = []
    excluded_trucks: dict[str, str] = {}
    for tid in sorted(seen_trucks):
        cid = truck_class.get(tid)
        if cid is None:
            excluded_trucks[tid] = "no_haulier"
            continue
        c = classes[cid]
        if not c["on"]:
            excluded_trucks[tid] = "off_shift"
            continue
        t = next(x for x in by_class[cid] if _id(x) == tid)
        ready = now_s
        anchor, anchor_job, loc = "position", None, _latlng(assign_constraints.truck_anchor_loc(t))
        eng = engaged.get(tid)
        if eng is not None:
            jid = _id(eng)
            r0 = dispatched_at_s.get(jid)
            if r0 is not None:
                r0 = float(r0) - latency  # job_end_seconds adds the dispatch latency to a RELEASE
            else:
                rs = _request_step(eng)
                r0 = (rs * step_seconds) if rs is not None else now_s
            dloc = _latlng(assign_constraints.order_dropoff_loc(eng))
            if dloc is not None:
                anchor, anchor_job, loc = "engaged", jid, dloc
                ready = max(ready, job_end(eng, float(r0)))
        else:
            # the last dispatched (not done) job of its itinerary: dispatched, not yet picked up
            last = None
            for j in store_itins.get(tid, ()):
                rec = status.get(j) or {}
                if rec.get("status") == "dispatched" and (rec.get("truck") in (None, tid)):
                    last = j
            if last is not None:
                f = store_jobs.get(last) or {}
                doc = orders_by_id.get(last)
                dloc = f.get("dropoff") or (_latlng(assign_constraints.order_dropoff_loc(doc)) if doc else None)
                end = f.get("e")
                if last in dispatched_at_s and doc is not None:
                    end = job_end(doc, float(dispatched_at_s[last]) - latency)
                elif end is None and doc is not None:
                    rs = _request_step(doc)
                    end = job_end(doc, max(now_s, rs * step_seconds if rs is not None else now_s))
                if dloc is not None:
                    anchor, anchor_job, loc = "dispatched", last, [float(dloc[0]), float(dloc[1])]
                    if end is not None:
                        ready = max(ready, float(end))
        if loc is None:
            excluded_trucks[tid] = "no_position"
            continue
        wins = c["windows"]
        usable = [w for w in wins if max(ready, w[0]) < w[1]] if wins is not None else None
        if usable is not None and not usable:
            excluded_trucks[tid] = "off_shift"
            continue
        if (same_day and not p.replan_idle_trucks and anchor == "position" and tid in store_itins
                and all((status.get(j) or {}).get("status") in ("done", "failed")
                        for j in store_itins.get(tid, ()))):
            excluded_trucks[tid] = "sleeping"
            continue
        if usable is None:
            out_trucks.append({"id": tid, "class": cid, "ready_s": float(ready), "loc": loc,
                               "anchor": anchor, "anchor_job": anchor_job})
            continue
        out_trucks.append({"id": tid, "class": cid, "ready_s": float(max(ready, usable[0][0])), "loc": loc,
                           "anchor": anchor, "anchor_job": anchor_job})
        # M4/minor 6: a later window of the same plan (the evening half of an overnight
        # shift) is a second START of the truck, ready at that window's start, at the
        # truck's anchor (where it will be is unknown here; the re-plan in that window
        # corrects it). The child appends its chain to the truck's own (``of``).
        for k, w in enumerate(usable[1:], start=1):
            out_trucks.append({"id": f"{tid}@w{k}", "of": tid, "window": k, "class": cid,
                               "ready_s": float(w[0]), "loc": list(loc), "anchor": anchor,
                               "anchor_job": anchor_job})
    working_classes = {t["class"] for t in out_trucks}

    def fit(release: float, wins: Optional[Sequence[Sequence[float]]], order: Mapping[str, Any],
            ends: dict[float, float]) -> Optional[float]:
        """Earliest start ``max(release, window start)`` of the first window the job fits in
        (its end inside the window); ``release`` itself without hours; ``None`` if none."""
        if wins is None:
            return release
        for w in wins:
            r_c = max(release, float(w[0]))
            e_c = ends.get(r_c)
            if e_c is None:
                e_c = ends[r_c] = job_end(order, r_c)
            if e_c <= float(w[1]):
                return r_c
        return None

    # ---- chain propagation (M3): a still-planned job of the previous version starts no
    # earlier than its predecessor's PROPAGATED end, from the truck's ready time on. With
    # fixed job times a late head (an engaged truck ready after its planned end) broke its
    # successors' arcs; propagated, the old chain stays feasible in the new instance.
    # Minimal shift: exactly what the arc rule needs (start arc ``r_j >= ready - s_j``, link
    # ``r_j >= e_i - s_i``), so an unchanged world changes nothing, and an end never moves
    # EARLIER (the time model's hour buckets could shorten a later job and break its
    # successor's ``r_k <= e_j + W``).
    prop_r: dict[str, float] = {}
    prop_e: dict[str, float] = {}

    def slack_of(order: Mapping[str, Any]) -> float:
        if p.late_slack_min is not None:
            return float(p.late_slack_min) * 60.0
        prof = _profile(order)
        return time_model.late_slack_seconds(prof.get("pickup_facility_name"), prof.get("dropoff_facility_name"))

    if same_day and p.propagate_chains:
        ready_of = {t["id"]: t["ready_s"] for t in out_trucks if "of" not in t}
        for tid in sorted(store_itins):
            if tid not in ready_of:
                continue
            wins = classes[truck_class[tid]]["windows"]
            cur = None   # the bound the next job's release must meet
            for j in store_itins[tid]:
                if (status.get(j) or {}).get("status") != "planned":
                    continue
                o = orders_by_id.get(j)
                rs = _request_step(o) if o is not None else None
                if o is None or rs is None or o.get("state") not in OPEN_ORDER_STATES:
                    continue
                if _latlng(assign_constraints.order_pickup_loc(o)) is None \
                        or _latlng(assign_constraints.order_dropoff_loc(o)) is None:
                    continue
                base = max(rs * step_seconds, now_s)
                sj = slack_of(o)
                need = (ready_of[tid] - sj) if cur is None else cur
                rj = fit(max(base, need), wins, o, {})
                if rj is None:
                    break  # the chain falls out of the shift here: the rest is re-planned freely
                own = fit(base, wins, o, {})
                if p.propagate_max_push_min is not None and rj - (base if own is None else own) \
                        > float(p.propagate_max_push_min) * 60.0:
                    break  # too late to keep: free trucks may take the rest at its own release
                ej = job_end(o, rj)
                if rj > base:
                    prop_r[j] = rj
                    ej = max(ej, job_end(o, base))
                    prop_e[j] = ej
                cur = ej - sj

    # ---- jobs ------------------------------------------------------------------------------
    jobs: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}
    for oid in sorted(orders_by_id):
        o = orders_by_id[oid]
        rec = status.get(oid) or {}
        st = rec.get("status")
        if st in _LOCKED or oid in locked:
            excluded[oid] = "locked"
            continue
        if st == "failed" and rec.get("reason") in _TERMINAL_FAIL_REASONS:
            excluded[oid] = f"failed:{rec.get('reason')}"
            continue
        if o.get("state") not in OPEN_ORDER_STATES:
            excluded[oid] = "not_open"
            continue
        owner = assign_constraints.haulier_of(o)
        rs = _request_step(o)
        pu = _latlng(assign_constraints.order_pickup_loc(o))
        do = _latlng(assign_constraints.order_dropoff_loc(o))
        if owner is None or rs is None or pu is None or do is None:
            excluded[oid] = "malformed"
            continue
        r_req = rs * step_seconds
        if r_req >= window_end_s:
            excluded[oid] = "outside_window"
            continue
        r = max(r_req, now_s, prop_r.get(oid, now_s))
        prof = _profile(o)
        pf, df = prof.get("pickup_facility_name"), prof.get("dropoff_facility_name")
        slack = (float(p.late_slack_min) * 60.0 if p.late_slack_min is not None
                 else time_model.late_slack_seconds(pf, df))
        deadline = None
        if max_wait_steps:
            deadline = (rs + int(max_wait_steps)) * step_seconds
            if r + latency + margin_steps * step_seconds > deadline:
                excluded[oid] = "deadline"
                continue
        # M4: per class, the job may start at max(r, window start) -- an order released
        # before a shift starts waits for it -- and must end inside that window.
        elig: list[str] = []
        starts: list[float] = []
        ends: dict[float, float] = {}
        late_for_deadline = False
        for cid in working_classes:
            c = classes[cid]
            if not may_serve(owner, c["haulier"]):
                continue
            rep = by_class[cid][0]
            if not assign_constraints.size_compatible(rep, o) or assign_constraints.restricted_area_blocks(rep, o):
                continue
            r_c = fit(r, c["windows"], o, ends)
            if r_c is None:
                continue
            if deadline is not None and r_c + latency + margin_steps * step_seconds > deadline:
                late_for_deadline = True
                continue
            elig.append(cid)
            starts.append(r_c)
        if not elig:
            excluded[oid] = "deadline" if late_for_deadline else "no_eligible_class"
            continue
        r = min(starts)
        e = ends.get(r)
        if e is None:
            e = job_end(o, r)
        if oid in prop_e and r == prop_r.get(oid):
            e = max(e, prop_e[oid])
        jobs.append({
            "id": oid, "owner": owner, "owner_class": owner_class[owner], "classes": sorted(elig),
            "request_step": rs, "r_req": r_req, "r": r, "e": e, "slack": slack,
            "deadline_s": deadline, "pickup": pu, "dropoff": do,
            "pickup_site": pf, "dropoff_site": df,
            "first_day": _first_day(oid, store_cur, status, day),
            "new": oid not in status,
            "status": (f"failed:{rec.get('reason')}" if st == "failed" else None),
        })

    # ---- previous version's arcs (stability bonus; same day only) ---------------------------
    prev_arcs: list[list[str]] = []
    if same_day:
        open_ids = {j["id"] for j in jobs}
        truck_ids = {t["id"] for t in out_trucks}
        for tid in sorted(store_itins):
            tail = [j for j in store_itins[tid]
                    if j in open_ids and (status.get(j) or {}).get("status") == "planned"]
            if not tail:
                continue
            if tid in truck_ids:
                prev_arcs.append([tid, tail[0]])
            prev_arcs.extend([a, b] for a, b in zip(tail, tail[1:]))
        prev_arcs.sort()

    for t in out_trucks:
        if t["id"] in orders_by_id:
            raise ValueError(f"truck id {t['id']} collides with an order id")

    class_attrs = {cid: [c["size"], c["restricted_areas"], c["windows"]] for cid, c in classes.items()}
    # A synthetic (vehicle-less) owner class takes the attributes of its component's real
    # classes when those agree, so it never blocks the exact flow on its own (M5).
    for comp in components:
        real = {_canonical(class_attrs[c]) for c in comp if not classes[c]["synthetic"]}
        if len(real) == 1:
            ref = json.loads(real.pop())
            for c in comp:
                if classes[c]["synthetic"]:
                    class_attrs[c] = ref
    for c in classes.values():
        c.pop("on")

    inputs = PlanInputs(
        schema=INPUTS_SCHEMA, day=day, now_s=now_s, window_end_s=window_end_s,
        step_seconds=step_seconds, cadence_steps=int(cadence_steps),
        classes=classes, class_attrs=class_attrs, cooperation=cooperation, components=components,
        trucks=out_trucks, jobs=jobs, excluded=excluded, excluded_trucks=excluded_trucks,
        prev_arcs=prev_arcs, params=p.as_dict(), time_model=time_model.digest,
    )
    inputs["hash"] = inputs_hash(inputs)
    # A JSON round trip is the contract (the child reads exactly this); fail here, not there.
    json.dumps(inputs, allow_nan=False)
    return inputs


def store_job_fields(inputs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``jobs`` mapping for ``PlanStore.write_version``: per job its plan-time fields
    (``haulier``, ``r``, ``e``, ``pickup`` / ``dropoff`` [lat, lng], facilities,
    ``first_day``, ``request_step``). None of the store's reserved field names."""
    out = {}
    for j in inputs["jobs"]:
        out[j["id"]] = {"haulier": j["owner"], "r": j["r"], "e": j["e"], "pickup": j["pickup"],
                        "dropoff": j["dropoff"], "pickup_site": j["pickup_site"],
                        "dropoff_site": j["dropoff_site"], "first_day": j["first_day"],
                        "request_step": j["request_step"]}
    return out
