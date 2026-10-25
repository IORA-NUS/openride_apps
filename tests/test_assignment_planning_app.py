"""NetworkFlowDay WS-B, app side: planning ticks (F1), prefetch (F3), max orders (F4b), working hours (F5).

Drives the REAL ``AssignmentApp`` (built via ``__new__``, the convention of
tests/test_assignment_cooperation.py) with a recording fake manager, on the test-only
planning toy (tests/planning_toy.py, registered in an isolated registry). Pinned:

- the DEFAULT path (a top without planning, no prefetch, no working hours) makes exactly
  the five reads it always made -- no planning read, no ``orders_by_ids`` --, passes the
  runner no planning member, and its pairs are unchanged;
- ``plan``: every online truck (no busy filter), ``engaged`` from the open hauls (state +
  leg stamps carried as ``open_haul``), orders = open orders up to the window end minus
  the orders of open hauls, a ``kind="plan"`` tick with the right ``PlanningWindow``, no
  publish, the prefetch kept;
- prefetch: fetched on the NEXT tick only when non-empty, ``lookup`` carries
  ``claimed_by`` (critique M4), unassigned unclaimed docs appended AFTER the oldest-N in
  prefetch order, ``created`` / claimed / duplicate docs never appended; a failing read
  degrades to the oldest-N;
- ``PlanningSchedule``: day starts (elapsed axis) and every_steps, the catch-up rule and
  late stamp (minor 4), nothing at or past the horizon (minor 3), the declaration cached
  when the runner is built (minor 2);
- working hours filter dispatch ticks only, only with the key, overnight windows;
- the manager's planning reads: query shapes (paged, ``page_size``, stable ``_id`` sort,
  chunks of 200), and ``_paged_where`` without the new kwargs sends the old params.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from apps.container_logistics.assignment import planning as P
from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.assignment.manager import AssignmentManager
from apps.container_logistics.policies.dispatch.tick import PlanningWindow
from apps.container_logistics.policies.framework.registry import isolated_registry
from tests.planning_toy import register_toys

SIM_CLOCK = "Wed, 01 Jan 2020 08:00:00 GMT"


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid, haulier="acme", lon=103.85, lat=1.30):
    return {"_id": tid, "state": "online", "haulier_id": haulier,
            "profile": {"haulier_id": haulier, "current_loc": _pt(lon, lat), "truck_size": "40ft"}}


def _order(oid, haulier="acme", state="unassigned", lon=103.851, lat=1.30, **extra):
    doc = {"_id": oid, "state": state, "haulier_id": haulier, "pickup_loc": _pt(lon, lat),
           "dropoff_loc": _pt(lon + 0.05, lat), "profile": {"haulier_id": haulier}}
    doc.update(extra)
    return doc


class _Manager:
    """Records every read; serves canned documents."""

    def __init__(self, trucks, unassigned, *, open_orders=(), hauls=(), by_id=None, claimed=(), busy=(),
                 fail_by_ids=False):
        self.trucks, self.unassigned = list(trucks), list(unassigned)
        self.open_orders, self.hauls = list(open_orders), list(hauls)
        self.by_id = dict(by_id or {})
        self.claimed, self.busy = set(claimed), list(busy)
        self.fail_by_ids = fail_by_ids
        self.calls: List[tuple] = []

    def list_trucks(self, projection=None, online_only=True):
        self.calls.append(("list_trucks", online_only))
        return list(self.trucks)

    def list_unassigned_orders(self, projection=None):
        self.calls.append(("list_unassigned_orders",))
        return list(self.unassigned)

    def order_ids_with_open_haul(self):
        self.calls.append(("order_ids_with_open_haul",))
        return set(self.claimed)

    def active_haul_truck_ids(self):
        self.calls.append(("active_haul_truck_ids",))
        return list(self.busy)

    def online_state_name(self):
        self.calls.append(("online_state_name",))
        return "online"

    def resolve_facility_resource_id(self, name):
        return None

    def list_open_orders_until(self, end_step, projection=None):
        self.calls.append(("list_open_orders_until", end_step, tuple(sorted(projection or {}))))
        return list(self.open_orders)

    def open_haul_rows(self):
        self.calls.append(("open_haul_rows",))
        return [dict(h) for h in self.hauls]

    def orders_by_ids(self, ids, projection=None):
        self.calls.append(("orders_by_ids", tuple(ids), tuple(sorted(projection or {}))))
        if self.fail_by_ids:
            raise RuntimeError("HTTP 500")
        return [dict(self.by_id[i]) for i in ids if i in self.by_id]

    def names(self):
        return [c[0] for c in self.calls]


class _Messenger:
    def __init__(self):
        self.published = []
        self.client = self

    def publish(self, topic, message):
        self.published.append((topic, json.loads(message)))


def _profile(top="ToyDayPlan", **extra):
    prof = {"policies_format": 2, "policies": {"planner": {"type": top, "params": {}}},
            "respect_truck_online_state": True, "reject_if_active_haul_trip": True, "use_spatial_matching": True}
    prof.update(extra)
    return prof


def _app(manager, prof, *, run_id="run_t", step_seconds=240, horizon=2520):
    app = AssignmentApp.__new__(AssignmentApp)
    app.behavior = {"profile": prof}
    app.run_id = run_id
    app.manager = manager
    app.messenger = _Messenger()
    app._solver = AssignmentApp._build_dispatch_policy(prof)
    app._solver_params = {}
    app._warned_haulier_block = False
    app._share_tags = {}
    app._step_seconds = step_seconds
    app._sim_horizon_steps = horizon
    app._dispatch_runner(prof)  # the eager bind of __init__ (caches the planning declaration)
    return app


@pytest.fixture
def toys():
    with isolated_registry():
        register_toys()
        yield


def _spy_ticks(app, monkeypatch):
    """Record every ``runner.tick`` call's kwargs (the runner is the app's cached one)."""
    runner = app._dispatch_runner(app.behavior["profile"])
    seen: List[Dict[str, Any]] = []
    real = runner.tick

    def tick(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(runner, "tick", tick)
    return seen


# --- the default path ---------------------------------------------------------------------

_DEFAULT_READS = ["list_trucks", "list_unassigned_orders", "order_ids_with_open_haul",
                  "active_haul_truck_ids", "online_state_name"]


def test_default_path_reads_exactly_what_it_always_read(monkeypatch):
    mgr = _Manager([_truck("t1")], [_order("o1")])
    app = _app(mgr, {"strategy": "GreedyNearest", "respect_truck_online_state": True,
                     "reject_if_active_haul_trip": True, "use_spatial_matching": True})
    seen = _spy_ticks(app, monkeypatch)
    assert app.planning_spec() is None
    assert app.planning_due(0) is None and app.planning_due(360) is None
    pairs = app.assign(SIM_CLOCK, 0)
    assert [(t["_id"], o["_id"]) for t, o in pairs] == [("t1", "o1")]
    assert mgr.names() == _DEFAULT_READS
    [kwargs] = seen
    assert not {"kind", "planning", "engaged", "lookup", "run_id"} & set(kwargs)
    assert app._prefetch == ()
    app.assign(SIM_CLOCK, 28)
    assert mgr.names() == _DEFAULT_READS * 2  # still nothing extra on the next tick


def test_default_max_orders_per_tick_is_500_and_the_profile_key_reaches_the_read():
    from unittest.mock import MagicMock, patch

    user = MagicMock()
    for prof, limit in (({}, 500), ({"max_orders_per_tick": 1234}, 1234)):
        mgr = AssignmentManager("r", SIM_CLOCK, user, profile=prof)
        with patch.object(mgr, "_try_aggregate_items", return_value=[]) as agg:
            mgr.list_unassigned_orders()
        assert agg.call_args[0][1]["$limit"] == limit


# --- planning ticks --------------------------------------------------------------------------

def _plan_world():
    trucks = [_truck("t1"), _truck("t2"), _truck("t3")]  # t3 is busy (engaged)
    hauls = [{"_id": "h1", "truck": "t3", "order": "o_eng", "state": "pickup_in_progress",
              "stats": {"reposition_started_at": "Wed, 01 Jan 2020 08:04:00 GMT"}}]
    open_orders = [_order("o_a", meta={"request_time_step": 10}), _order("o_b", state="created",
                                                                          meta={"request_time_step": 200}),
                   _order("o_eng", meta={"request_time_step": 0})]  # doc lags its haul
    by_id = {"o_eng": _order("o_eng", state="assigned", truck="t3")}
    return trucks, hauls, open_orders, by_id


def test_plan_builds_the_planning_tick_and_publishes_nothing(toys, monkeypatch):
    trucks, hauls, open_orders, by_id = _plan_world()
    mgr = _Manager(trucks, [], open_orders=open_orders, hauls=hauls, by_id=by_id, busy=["t3"])
    app = _app(mgr, _profile())
    seen = _spy_ticks(app, monkeypatch)
    assert app.planning_spec()["foresight"] == "day"
    due = app.planning_due(0)
    assert due == P.Due("day_start", 0, 0, 0, 0)
    outcome = app.plan(SIM_CLOCK, 0, due)
    assert outcome.pairs == [] and app.messenger.published == []
    [kw] = seen
    assert kw["kind"] == "plan" and kw["run_id"] == "run_t"
    assert kw["planning"] == PlanningWindow(reason="day_start", day=0, start_step=0, end_step=360, now_step=0,
                                            step_seconds=240)
    assert [t["_id"] for t in kw["candidates"]] == ["t1", "t2", "t3"]  # busy t3 included (D6)
    assert [o["_id"] for o in kw["orders"]] == ["o_a", "o_b"]  # o_eng has an open haul
    assert set(kw["engaged"]) == {"t3"}
    eng = kw["engaged"]["t3"]
    assert eng["_id"] == "o_eng" and eng["open_haul"] == {
        "_id": "h1", "state": "pickup_in_progress",
        "stats": {"reposition_started_at": "Wed, 01 Jan 2020 08:04:00 GMT"}}
    assert mgr.names() == ["list_trucks", "open_haul_rows", "orders_by_ids", "list_open_orders_until"]
    read = [c for c in mgr.calls if c[0] == "list_open_orders_until"][0]
    assert read[1] == 360 and "meta" in read[2] and "state" in read[2]
    by_ids = [c for c in mgr.calls if c[0] == "orders_by_ids"][0]
    assert by_ids[1] == ("o_eng",) and {"state", "truck", "meta"} <= set(by_ids[2])
    # The toy planned t1 <- o_a, t2 <- o_b (first free same-haulier truck); heads = the prefetch.
    assert app._prefetch == ("o_a", "o_b")
    # Schedule marked: the same day is not due again; the next interval boundary is.
    assert app.planning_due(1) is None and app.planning_due(17) is None
    assert app.planning_due(18).reason == "interval"


def test_plan_window_foresight_none_ends_after_the_current_step(toys, monkeypatch):
    import tests.planning_toy as toy

    monkeypatch.setattr(toy._ToyBase, "planning", {"at_day_start": False, "every_steps": 18,
                                                   "foresight": "none", "max_hold_seconds": 30})
    mgr = _Manager([_truck("t1")], [], open_orders=[_order("o_a")])
    app = _app(mgr, _profile())
    seen = _spy_ticks(app, monkeypatch)
    assert app.planning_due(0).reason == "interval"
    app.plan(SIM_CLOCK, 20)  # a late interval plan (boundary 18)
    w = seen[0]["planning"]
    assert (w.reason, w.day, w.start_step, w.end_step, w.now_step) == ("interval", 0, 18, 21, 20)


def test_then_dispatch_consumes_the_prefetch_with_claimed_by(toys, monkeypatch):
    trucks, hauls, open_orders, by_id = _plan_world()
    mgr = _Manager(trucks, [], open_orders=open_orders, hauls=hauls, by_id=by_id, busy=["t3"])
    app = _app(mgr, _profile())
    app.plan(SIM_CLOCK, 0)
    assert app._prefetch == ("o_a", "o_b")
    # Dispatch tick: o_a is NOT in the oldest-N (e.g. beyond the cap) but still unassigned;
    # o_b is not released yet (``created``): looked up, never in the tick.
    mgr.unassigned = [_order("o_x", haulier="borax")]
    mgr.by_id["o_a"] = _order("o_a", meta={"request_time_step": 10})
    mgr.by_id["o_b"] = _order("o_b", state="created", meta={"request_time_step": 200})
    mgr.calls.clear()
    seen = _spy_ticks(app, monkeypatch)
    pairs = app.assign(SIM_CLOCK, 6)
    assert [(t["_id"], o["_id"]) for t, o in pairs] == [("t1", "o_a")]
    assert mgr.names() == _DEFAULT_READS + ["orders_by_ids", "open_haul_rows"]
    [kw] = seen
    assert [o["_id"] for o in kw["orders"]] == ["o_x", "o_a"]  # appended AFTER the oldest-N
    assert list(kw["lookup"]) == ["o_a", "o_b"] and kw["lookup"]["o_a"]["claimed_by"] is None
    assert kw["lookup"]["o_b"]["state"] == "created"
    assert kw["run_id"] == "run_t" and "kind" not in kw
    # The toy prefetches nothing on a dispatch tick: the next tick reads nothing extra.
    mgr.calls.clear()
    app.assign(SIM_CLOCK, 12)
    assert mgr.names() == _DEFAULT_READS


@pytest.mark.parametrize("state,claimed_by,in_oldest,appended", [
    ("unassigned", None, False, True),
    ("unassigned", "t9", False, False),  # claimed by another truck's open haul (M4)
    ("unassigned", None, True, False),   # already in the oldest-N: never duplicated
    ("created", None, False, False),     # not released: lookup only
    ("assigned", None, False, False),
    ("pickup_in_progress", None, False, False),
    ("in_transit", None, False, False),
    ("dropoff_in_progress", None, False, False),
    ("completed", None, False, False),
    ("cancelled", None, False, False),
])
def test_prefetch_lookup_rows(state, claimed_by, in_oldest, appended):
    hauls = [{"_id": "h9", "truck": claimed_by, "order": "o1", "state": "assigned"}] if claimed_by else []
    mgr = _Manager([], [], hauls=hauls, by_id={"o1": _order("o1", state=state, truck="t2")})
    app = _app(mgr, {"strategy": "GreedyNearest"})
    app._prefetch = ("o1", "o_missing", "o1")
    oldest = [_order("o1")] if in_oldest else []
    lookup, extra = app._prefetch_lookup({"_id": 1}, set(), oldest)
    assert list(lookup) == ["o1"] and lookup["o1"]["state"] == state
    assert lookup["o1"]["claimed_by"] == claimed_by and lookup["o1"]["truck"] == "t2"
    assert [d["_id"] for d in extra] == (["o1"] if appended else [])
    assert app._prefetch == ()  # consumed


def test_prefetch_claimed_set_excludes_and_order_is_prefetch_order():
    by_id = {i: _order(i) for i in ("o3", "o1", "o2")}
    mgr = _Manager([], [], by_id=by_id)
    app = _app(mgr, {"strategy": "GreedyNearest"})
    app._prefetch = ("o3", "o1", "o2")
    lookup, extra = app._prefetch_lookup({"_id": 1}, {"o1"}, [])
    assert [d["_id"] for d in extra] == ["o3", "o2"] and list(lookup) == ["o3", "o1", "o2"]


def test_a_failed_prefetch_read_degrades_to_the_oldest_n(toys):
    mgr = _Manager([_truck("t1")], [_order("o1")], fail_by_ids=True)
    app = _app(mgr, _profile())
    app._prefetch = ("o9",)
    app.assign(SIM_CLOCK, 0)
    assert app._prefetch == () and "orders_by_ids" in mgr.names()


def test_a_discarded_result_clears_the_prefetch(toys):
    with isolated_registry():
        register_toys()
        mgr = _Manager([_truck("t1")], [], open_orders=[_order("o_a")])
        app = _app(mgr, _profile("ToyBadPrefetch"))
        app._prefetch = ()
        app.plan(SIM_CLOCK, 0)  # R7: the result is discarded
        assert app._prefetch == ()


# --- PlanningSchedule ------------------------------------------------------------------------

DECL = {"at_day_start": True, "every_steps": 18, "foresight": "day", "max_hold_seconds": 30}


def _walk(decl, steps, *, step_seconds=240, horizon=None):
    sched, out = P.PlanningSchedule(), []
    for s in steps:
        due = sched.due(decl, s, step_seconds=step_seconds, horizon_steps=horizon)
        if due is not None:
            sched.mark(due, decl, step_seconds=step_seconds)
            out.append((s, due.reason, due.boundary, due.late_steps))
    return sched, out


def test_schedule_day_starts_and_intervals():
    _s, out = _walk(DECL, range(0, 740))
    days = [x for x in out if x[1] == "day_start"]
    assert days == [(0, "day_start", 0, 0), (360, "day_start", 360, 0), (720, "day_start", 720, 0)]
    intervals = [x[0] for x in out if x[1] == "interval"]
    assert intervals == [s for s in range(18, 740, 18) if s % 360]


def test_schedule_catch_up_plans_the_first_step_after_a_missed_boundary_and_stamps_it_late():
    steps = [3, 4, 5, 19, 20, 365, 366]  # booted at 3; 18 and 360 missed
    sched, out = _walk(DECL, steps)
    assert out == [(3, "day_start", 0, 3), (19, "interval", 18, 1), (365, "day_start", 360, 5)]
    assert (sched.plans, sched.late_plans, sched.max_late_steps) == (3, 3, 5)


def test_schedule_skipped_days_plan_only_the_current_day():
    _s, out = _walk({"at_day_start": True, "every_steps": None}, [0, 900])
    assert out == [(0, "day_start", 0, 0), (900, "day_start", 720, 180)]


def test_schedule_nothing_at_or_after_the_horizon():
    _s, out = _walk(DECL, range(2500, 2600), horizon=2520)
    assert all(s < 2520 for s, *_ in out) and out[0] == (2500, "day_start", 2160, 340)


def test_schedule_without_planning_or_a_bad_step():
    sched = P.PlanningSchedule()
    assert sched.due(None, 0, step_seconds=240) is None
    assert sched.due(DECL, None, step_seconds=240) is None and sched.due(DECL, -1, step_seconds=240) is None


def test_steps_per_day_follows_step_seconds():
    _s, out = _walk({"at_day_start": True, "every_steps": None}, range(0, 1500), step_seconds=120)
    assert [x[0] for x in out] == [0, 720, 1440]


def test_planning_declaration_is_cached_when_the_runner_is_built(toys, monkeypatch):
    mgr = _Manager([], [])
    app = _app(mgr, _profile())
    decl = app.planning_spec()
    runner = app._dispatch_runner(app.behavior["profile"])
    monkeypatch.setattr(type(runner), "tree", property(lambda self: (_ for _ in ()).throw(AssertionError)),
                        raising=False)
    assert app.planning_spec() is decl  # read from the cache, no runner touched


# --- working hours (F5) -----------------------------------------------------------------------

def test_on_shift_windows():
    rec = {"axis": "elapsed", "hauliers": {"day": [21600, 72000], "night": [72000, 21600], "all": [0, 86400]}}
    assert P.on_shift(rec, "day", 21600) and not P.on_shift(rec, "day", 72000) and not P.on_shift(rec, "day", 0)
    assert P.on_shift(rec, "night", 80000) and P.on_shift(rec, "night", 100) and not P.on_shift(rec, "night", 30000)
    assert P.on_shift(rec, "all", 86399) and P.on_shift(rec, "nobody", 0) and P.on_shift(None, "day", 0)
    assert P.seconds_of_day(360 + 90, 240) == 90 * 240  # elapsed axis, day 1


def test_working_hours_filter_dispatch_ticks_only(toys, monkeypatch):
    rec = {"axis": "elapsed", "hauliers": {"acme": [21600, 72000]}}
    trucks = [_truck("t_acme", "acme"), _truck("t_borax", "borax")]
    mgr = _Manager(trucks, [_order("o1", "acme"), _order("o2", "borax")], open_orders=[_order("o1", "acme")])
    app = _app(mgr, _profile(working_hours=rec))
    seen = _spy_ticks(app, monkeypatch)
    app.assign(SIM_CLOCK, 0)            # 00:00 elapsed: acme off shift
    app.assign(SIM_CLOCK, 90)           # 06:00: on
    app.assign(SIM_CLOCK, 360 + 300)    # day 1, 20:00: off (end exclusive)
    app.plan(SIM_CLOCK, 0)              # a planning tick sees every online truck
    got = [[t["_id"] for t in kw["candidates"]] for kw in seen]
    assert got == [["t_borax"], ["t_acme", "t_borax"], ["t_borax"], ["t_acme", "t_borax"]]


def test_no_working_hours_key_no_filter():
    mgr = _Manager([_truck("t1", "acme")], [_order("o1", "acme")])
    app = _app(mgr, {"strategy": "GreedyNearest"})
    assert len(app.assign(SIM_CLOCK, 0)) == 1


# --- manager planning reads ------------------------------------------------------------------

class _RecordingManager(AssignmentManager):
    def __init__(self, pages):
        from unittest.mock import MagicMock

        super().__init__("run_m", SIM_CLOCK, MagicMock(), profile={})
        self.pages, self.requests = pages, []

    def _get(self, url, params=None):
        self.requests.append((url, dict(params or {})))
        return {"_items": self.pages.pop(0) if self.pages else []}


def test_paged_where_without_new_kwargs_sends_the_old_params():
    m = _RecordingManager([[{"_id": "a"}]])
    m._paged_where("u", {"x": 1}, projection={"_id": 1})
    assert [set(p) for _u, p in m.requests] == [{"where", "page", "max_results", "projection"}] * 2
    assert m.requests[0][1]["max_results"] == 500


def test_list_open_orders_until_query():
    m = _RecordingManager([[{"_id": "a"}], [{"_id": "b"}]])
    assert [d["_id"] for d in m.list_open_orders_until(360, projection={"_id": 1})] == ["a", "b"]
    url, p = m.requests[0]
    assert url.endswith("/run_m/order") and p["max_results"] == 1000 and json.loads(p["sort"]) == [["_id", 1]]
    assert json.loads(p["where"]) == {"$and": [{"run_id": "run_m"}, {"state": {"$in": ["created", "unassigned"]}},
                                               {"meta.request_time_step": {"$lt": 360}}]}
    assert [r[1]["page"] for r in m.requests] == [1, 2, 3]


def test_open_haul_rows_query():
    m = _RecordingManager([])
    m.open_haul_rows()
    url, p = m.requests[0]
    assert url.endswith("/run_m/truck/trip")
    assert json.loads(p["projection"]) == {"_id": 1, "truck": 1, "order": 1, "state": 1,
                                           "stats.reposition_started_at": 1, "stats.loaded_started_at": 1}
    assert json.loads(p["where"])["$and"][1] == {"state": {"$nin": ["completed", "cancelled"]}}


def test_orders_by_ids_chunks_of_200_deduplicated():
    ids = [f"{i:024x}" for i in range(450)] + ["0" * 24]
    pages = [[{"_id": i} for i in ids[:200]], [{"_id": i} for i in ids[200:400]], [{"_id": i} for i in ids[400:449]],
             []]  # one id of the last chunk does not exist
    m = _RecordingManager(pages)
    out = m.orders_by_ids(ids, projection={"_id": 1, "state": 1})
    assert len(out) == 449
    chunks = [json.loads(p["where"])["$and"][1]["_id"]["$in"] for _u, p in m.requests]
    assert [len(c) for c in chunks] == [200, 200, 50, 50]  # the short chunk asks page 2 once (empty)
    assert chunks[0][0] == ids[0] and [r[1]["page"] for r in m.requests] == [1, 1, 1, 2]
    assert all(json.loads(r[1]["sort"]) == [["_id", 1]] for r in m.requests)
    assert m.orders_by_ids([]) == [] and len(m.requests) == 4
