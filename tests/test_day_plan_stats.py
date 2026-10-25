"""``DayPlanStats``: folding the day-plan notes into ``run_config.meta.day_plan`` (plan §4 ``stats.py``;
critique M11, minor 15). Pure Python, no OR-Tools.
"""

from __future__ import annotations

import json

import pytest

from apps.container_logistics.policies.dispatch import run_stats
from apps.container_logistics.policies.solver_lib.day_plan import store as plan_store
from apps.container_logistics.policies.solver_lib.day_plan.instance import build_inputs, store_job_fields
from apps.container_logistics.policies.solver_lib.day_plan.stats import (
    PUSH_EVERY,
    DayPlanStats,
    events_note,
    merge_notes,
    plan_note,
    rebuild_note,
)
from apps.container_logistics.policies.solver_lib.day_plan.time_model import TimeModel

from tests._day_plan_fixtures import STEP_S, oid, order, truck

TM = TimeModel.default()


class Clock:
    def __init__(self, step, horizon=2520):
        self.time_step, self.horizon_steps = step, horizon


def _inputs(day, orders, trucks, state=None):
    return build_inputs(trucks, {}, orders, state, TM, None, now_s=day * 86400.0, day=day,
                        window_end_s=(day + 1) * 86400.0, step_seconds=STEP_S,
                        cooperation_arm={"acme": ["cargo"], "cargo": ["acme"]}, working_hours=None,
                        max_wait_steps=None, cadence_steps=6)


def _fake_solve(inputs, plan):
    """A runner.solve-shaped answer: ``plan`` = {truck: [job]}."""
    planned = {j for c in plan.values() for j in c}
    return {"itineraries": plan, "unplanned": sorted(j["id"] for j in inputs["jobs"] if j["id"] not in planned),
            "stats": {"solve_ms": 12.0, "build_ms": 3.0, "wall_ms": 40.0, "deterministic": True,
                      "backends": {"mincostflow": 1},
                      "first_leg_m": {t: 1500.0 for t, c in plan.items() if c}}}


O = lambda n: oid("order", n)  # noqa: E731
T = lambda n: oid("truck", n)  # noqa: E731


@pytest.fixture
def day0():
    trucks = [truck(0, "acme"), truck(1, "cargo")]
    orders = [order(0, "acme", 100, "port_000", "customer_000"), order(1, "cargo", 110, "port_000", "customer_001"),
              order(2, "acme", 120, "port_001", "customer_002"), order(3, "cargo", 130, "port_001", "customer_003")]
    inp = _inputs(0, orders, trucks)
    return trucks, orders, inp


def test_plan_and_dispatch_notes_fold_into_day_and_haulier_counts(day0):
    trucks, orders, inp = day0
    agg = DayPlanStats()
    solve = _fake_solve(inp, {T(0): [O(0), O(2)], T(1): [O(1)]})
    assert agg.fold(plan_note(inp, outcome="solved", version="d0v1", solve=solve,
                              write_summary={"jobs_moved": 0, "planned": 3, "unplanned": 1}), Clock(0)) is False
    agg.fold(events_note(0, [(O(0), "dispatched", None, "acme"), (O(1), "failed:cancelled")]), Clock(6))
    agg.fold(events_note(0, [(O(0), "done"), (O(2), "failed", "taken")]), Clock(12))
    snap = agg.snapshot()
    d0 = snap["days"]["d0"]
    assert d0["plan_jobs"] == 4 and d0["plan_jobs_done"] == 1
    assert d0["plan_jobs_failed"] == {"cancelled": 1, "taken": 1}
    assert d0["plan_jobs_undone"] == 1 and d0["versions"] == 1
    assert d0["plan_first_leg_km"] == {"acme": 1.5, "cargo": 1.5}
    assert d0["by_haulier"]["acme"]["plan_jobs_done"] == 1
    assert d0["by_haulier"]["cargo"]["plan_jobs_failed"] == {"cancelled": 1}
    assert snap["totals"]["plan_jobs_done"] == 1 and snap["backends"] == {"mincostflow": 1}
    assert snap["solve_ms"]["n"] == 1 and snap["solve_ms"]["p50"] == 12.0
    json.dumps(snap)


def test_rolled_over_job_is_attributed_to_the_day_that_first_planned_it(day0):
    """M11: done on day 1 counts as day-0 done, never day-0 undone plus day-1 done."""
    trucks, orders, inp = day0
    agg = DayPlanStats()
    agg.fold(plan_note(inp, outcome="solved", version="d0v1", solve=_fake_solve(inp, {T(0): [O(0)]})), Clock(0))
    # day 1 plan: the open ones roll over; a new day-1 order appears
    later = orders + [order(9, "acme", 400, "port_000", "port_001")]
    st = plan_store.MemoryPlanStore("s", kv=plan_store.MemoryKV())
    st.write_version(0, {T(0): [O(0)]}, store_job_fields(inp))
    inp1 = _inputs(1, later, trucks, state=st.state())
    assert not next(j for j in inp1["jobs"] if j["id"] == O(0))["new"]
    forced = agg.fold(plan_note(inp1, outcome="solved", version="d1v1",
                                solve=_fake_solve(inp1, {T(0): [O(0), O(9)]})), Clock(360))
    assert forced is True  # a new day pushes
    snap = agg.snapshot()
    assert snap["days"]["d0"]["open_at_day_end"]["total"] == 4
    assert snap["days"]["d1"]["plan_jobs"] == 1  # only the new order
    agg.fold(events_note(1, [(O(0), "done"), (O(9), "done")]), Clock(370))
    snap = agg.snapshot()
    assert snap["days"]["d0"]["plan_jobs_done"] == 1 and snap["days"]["d0"]["plan_jobs_undone"] == 3
    assert snap["days"]["d0"]["open_at_day_end"]["total"] == 4   # frozen at the day change
    assert snap["days"]["d1"]["plan_jobs_done"] == 1 and snap["days"]["d1"]["plan_jobs_undone"] == 0
    assert snap["totals"]["plan_jobs_done"] == 2 and snap["totals"]["plan_jobs"] == 5


def test_failed_then_replanned_then_done_counts_once_as_done(day0):
    _, _, inp = day0
    agg = DayPlanStats()
    agg.fold(plan_note(inp, outcome="solved", version="d0v1", solve=_fake_solve(inp, {T(0): [O(0)]})), Clock(0))
    agg.fold(events_note(0, [(O(0), "failed", "infeasible")]), Clock(6))
    agg.fold(events_note(0, [(O(0), "planned"), (O(0), "dispatched")]), Clock(18))
    agg.fold(events_note(0, [(O(0), "done")]), Clock(24))
    d0 = agg.snapshot()["days"]["d0"]
    assert d0["plan_jobs_done"] == 1 and d0["plan_jobs_failed"] == {}


def test_skipped_and_failed_replans_and_the_push_cadence(day0):
    _, _, inp = day0
    agg = DayPlanStats()
    assert agg.push_due(force=True) is False  # nothing folded yet
    assert agg.fold(plan_note(inp, outcome="solved", version="d0v1", solve=_fake_solve(inp, {})), Clock(0)) is False
    assert agg.fold(plan_note(None, outcome="skipped", day=0), Clock(18)) is False
    assert agg.fold(plan_note(None, outcome="failed", reason="timeout", day=0), Clock(36)) is True
    d0 = agg.snapshot()["days"]["d0"]
    assert d0["replans_skipped"] == 1 and d0["replans_failed"] == {"timeout": 1}
    assert agg.push_due(force=True)
    agg.mark_pushed()
    for k in range(PUSH_EVERY - 1):
        agg.fold({}, Clock(40 + k))
        assert not agg.push_due()
    agg.fold({}, Clock(99))
    assert agg.push_due()
    with pytest.raises(ValueError):
        plan_note(None, outcome="weird", day=0)


def test_nondeterministic_solve_is_stamped(day0):
    _, _, inp = day0
    agg = DayPlanStats()
    solve = _fake_solve(inp, {})
    solve["stats"]["deterministic"] = False
    agg.fold(plan_note(inp, outcome="solved", version="d0v1", solve=solve), Clock(0))
    assert agg.snapshot()["deterministic"] is False


def test_rebuild_from_the_store_after_a_restart(day0):
    """Minor 15: a new aggregator + rebuild_note(store.state()) gives the same job counts."""
    _, _, inp = day0
    st = plan_store.MemoryPlanStore("s", kv=plan_store.MemoryKV())
    st.write_version(0, {T(0): [O(0), O(2)], T(1): [O(1)]}, store_job_fields(inp))
    st.set_statuses([(O(0), "done", None, T(0)), (O(1), "failed", "cancelled", None)])
    trucks, orders, _ = day0
    replan = _inputs(0, orders, trucks, state=st.state())       # done / cancelled are left out
    assert {O(0), O(1)}.isdisjoint(replan.job_ids)
    st.write_version(0, {T(0): [O(2)]}, store_job_fields(replan))  # a re-plan: d0v2
    live = DayPlanStats()
    live.fold(plan_note(inp, outcome="solved", version="d0v1", solve=_fake_solve(inp, {T(0): [O(0), O(2)]})), Clock(0))
    live.fold(events_note(0, [(O(0), "done"), (O(1), "failed", "cancelled")]), Clock(6))
    # the agent restarts: memory is gone, the Redis record (here MemoryKV) is not
    st2 = plan_store.MemoryPlanStore("s", kv=st.kv)
    st2.load_current()
    fresh = DayPlanStats()
    assert fresh.fold(rebuild_note(st2.state()), Clock(20)) is True
    a, b = live.snapshot(), fresh.snapshot()
    for k in ("plan_jobs", "plan_jobs_done", "plan_jobs_failed", "plan_jobs_undone"):
        assert a["days"]["d0"][k] == b["days"]["d0"][k], k
    assert b["restarts"] == 1 and b["days"]["d0"]["versions"] == 2
    assert b["days"]["d0"]["by_haulier"]["acme"]["plan_jobs_done"] == 1


def test_merge_notes_joins_a_plan_and_a_dispatch(day0):
    _, _, inp = day0
    n = merge_notes(plan_note(inp, outcome="solved", version="d0v1", solve=_fake_solve(inp, {T(0): [O(0)]})),
                    events_note(0, [(O(0), "dispatched")]))
    agg = DayPlanStats()
    agg.fold(n, Clock(0))
    assert agg.snapshot()["days"]["d0"]["plan_jobs_dispatched"] == 1
    with pytest.raises(ValueError):
        merge_notes({"plan": {}}, {"plan": {}})


def test_driven_by_the_framework_run_stats_writer(day0):
    _, _, inp = day0
    run_stats.check_meta_key(DayPlanStats, "test")
    pushed = []
    store = {}
    agg = run_stats.record_tick(store, DayPlanStats, plan_note(inp, outcome="failed", reason="crashed", day=0),
                                Clock(0), lambda k, v: pushed.append((k, v)) or True)
    assert isinstance(agg, DayPlanStats) and pushed and pushed[-1][0] == "day_plan"
    assert pushed[-1][1]["days"]["d0"]["replans_failed"] == {"crashed": 1}
    run_stats.flush(store, lambda k, v: pushed.append((k, v)) or True)
    assert DayPlanStats.empty_snapshot()["totals"]["plan_jobs"] == 0
