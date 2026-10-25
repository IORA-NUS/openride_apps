"""``NetworkFlowDay`` (``policies/dispatch/library/network_flow_day.py``): the day-plan wrapper.

What is locked (plan §4 N3 tests, addendum M4 / M6 / M7, critique minor 7 / 15):

- the dispatch HEAD RULES, one row per order state of the head (and the retry-once rule
  for a refused dispatch), each with the status the store records;
- the prefetch: each truck's oldest unresolved dispatched job plus its head, capped;
- order independence of the plan and of the dispatch;
- plan -> dispatch end to end through the framework runner with a ``MemoryPlanStore``;
  the locked head survives a re-plan; an unchanged instance is not re-solved;
- a crashed / timed-out solve keeps the previous version and is noted;
- an agent restart (a NEW instance + ``load_current``) dispatches exactly what the old
  instance would have; an unreachable Redis never fails a tick;
- R6: a planning tick that returns a pair is discarded by the framework;
- bind (planning declaration, pairs, run stats, world reads) and the Params rules.

Runs under the main venv (``nf_backend="greedy"`` or a stubbed solve where a backend is
needed); the min-cost-flow path runs only where OR-Tools is importable (scratch venv).
"""

from __future__ import annotations

import copy
import importlib.util

import pytest

from apps.container_logistics.policies.dispatch.library import network_flow_day as nfd
from apps.container_logistics.policies.dispatch.run_stats import record_tick
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import DispatchClock, PlanningWindow
from apps.container_logistics.policies.framework.catalog import policy_catalog
from apps.container_logistics.policies.framework.errors import PolicyParamsError
from apps.container_logistics.policies.framework.registry import discover_builtin_policies, get_policy_spec
from apps.container_logistics.policies.solver_lib.day_plan import runner as plan_runner
from apps.container_logistics.policies.solver_lib.day_plan.stats import DayPlanStats
from apps.container_logistics.policies.solver_lib.day_plan.store import MemoryKV, MemoryPlanStore, RedisPlanStore
from apps.container_logistics.policies.testing import fixtures as fx

from tests._day_plan_fixtures import oid, order, synthetic_day, truck

HAS_ORTOOLS = importlib.util.find_spec("ortools") is not None
SPD = 360
RUN = "run_nfd_test"
GREEDY = {"nf_backend": "greedy"}


def profile(structure=None):
    return fx.world_profile(structure, 10 ** 9)


class Sim:
    """One NetworkFlowDay instance behind the framework runner, its record in a ``MemoryKV``."""

    def __init__(self, params=None, *, kv=None, run_id=RUN, prof=None):
        discover_builtin_policies()
        self.prof = prof if prof is not None else profile()
        self.runner = build_runner_for_policy("NetworkFlowDay", dict(GREEDY, **(params or {})), profile=self.prof)
        self.inst = self.runner.tree.top.instance
        self.kv = kv if kv is not None else MemoryKV()
        self.inst.store_factory = lambda rid: MemoryPlanStore(rid or "local", kv=self.kv)
        self.run_id = run_id
        self.stats: dict = {}

    @property
    def store(self):
        return self.inst._state(self.run_id).store

    def _record(self, out, tick):
        if out.result is not None:
            record_tick(self.stats, DayPlanStats, out.result.tick_notes, tick.clock, lambda *_a, **_k: None)

    def plan(self, trucks, orders, step, *, engaged=None, lookup=None, reason="day_start"):
        day = step // SPD
        window = PlanningWindow(reason=reason, day=day, start_step=day * SPD, end_step=(day + 1) * SPD,
                                now_step=step, step_seconds=240)
        tick = self.runner.tick(candidates=list(trucks), orders=list(orders), profile=self.prof,
                                clock=DispatchClock(sim_clock=fx.SIM_CLOCK, time_step=step), seed=1,
                                kind="plan", planning=window, engaged=engaged, lookup=lookup, run_id=self.run_id)
        out = self.runner.run(tick)
        self._record(out, tick)
        return out

    def dispatch(self, trucks, orders, step, *, lookup=None):
        tick = self.runner.tick(candidates=list(trucks), orders=list(orders), profile=self.prof,
                                clock=DispatchClock(sim_clock=fx.SIM_CLOCK, time_step=step), seed=1,
                                lookup=lookup, run_id=self.run_id)
        out = self.runner.run(tick)
        self._record(out, tick)
        return out

    def install(self, itineraries, orders, day=0):
        """A plan version written straight into the store (the dispatch tests need no solve)."""
        st = self.inst._state(self.run_id)
        jobs = {str(o["_id"]): {"haulier": o["profile"]["haulier_id"]} for o in orders}
        st.store.write_version(day, itineraries, jobs)
        self.inst._refresh_trucks(st)
        return st

    def status(self, job):
        return (self.store.status_of(job) or {}).get("status"), (self.store.status_of(job) or {}).get("reason")


def ids(pairs):
    return [(str(t["_id"]), str(o["_id"])) for t, o in pairs]


def T(n, h="acme"):
    return truck(n, h)


def O(n, h="acme", state="unassigned", step=0):
    return order(n, h, step, "port_000", "customer_000", state=state)


def L(doc, *, state=None, truck_id=None, claimed=None):
    """A lookup document (what the app reads back for a prefetched id)."""
    d = copy.deepcopy(doc)
    if state is not None:
        d["state"] = state
    d["truck"] = truck_id
    d["claimed_by"] = claimed
    return d


# ---------------------------------------------------------------------------- head rules

T0, T9 = T(0), T(9)
O0, O1 = O(0), O(1)
t0, t9, o0, o1 = (str(x["_id"]) for x in (T0, T9, O0, O1))

#: (row id, lookup doc of the head o0 or None, o0 in the tick's orders, expected o0 status, next job paired)
HEAD_ROWS = [
    ("unassigned_free_in_tick", L(O0, state="unassigned"), True, ("dispatched", None), False),
    ("not_looked_up_in_tick", None, True, ("dispatched", None), False),
    ("not_looked_up_absent_waits", None, False, ("planned", None), False),
    ("created_waits", L(O0, state="created"), False, ("planned", None), False),
    ("cancelled", L(O0, state="cancelled"), False, ("failed", "cancelled"), True),
    ("assigned_other", L(O0, state="assigned", truck_id=t9), False, ("failed", "taken"), True),
    ("pickup_in_progress_other", L(O0, state="pickup_in_progress", truck_id=t9), False, ("failed", "taken"), True),
    ("in_transit_other", L(O0, state="in_transit", truck_id=t9), False, ("failed", "taken"), True),
    ("dropoff_in_progress_other", L(O0, state="dropoff_in_progress", truck_id=t9), False, ("failed", "taken"), True),
    ("completed_other", L(O0, state="completed", truck_id=t9), False, ("failed", "taken"), True),
    ("assigned_unnamed_is_other", L(O0, state="assigned"), False, ("failed", "taken"), True),
    ("assigned_ours", L(O0, state="assigned", truck_id=t0), False, ("dispatched", None), True),
    ("in_transit_ours", L(O0, state="in_transit", truck_id=t0), False, ("dispatched", None), True),
    ("completed_ours", L(O0, state="completed", truck_id=t0), False, ("done", None), True),
    ("unassigned_claimed_other", L(O0, state="unassigned", claimed=t9), False, ("failed", "taken"), True),
    ("unassigned_claimed_ours", L(O0, state="unassigned", claimed=t0), False, ("dispatched", None), True),
    ("unassigned_free_left_out_is_claimed", L(O0, state="unassigned"), False, ("failed", "taken"), True),
    ("unknown_state_waits", L(O0, state="weird"), False, ("planned", None), False),
]


@pytest.mark.parametrize("row", HEAD_ROWS, ids=[r[0] for r in HEAD_ROWS])
def test_head_rule_table(row):
    _name, doc, in_tick, want, next_paired = row
    sim = Sim()
    sim.install({t0: [o0, o1]}, [O0, O1])
    orders = ([O0] if in_tick else []) + [O1]
    out = sim.dispatch([T0], orders, 6, lookup={o0: doc} if doc is not None else None)
    assert sim.status(o0) == want
    expected = [(t0, o1)] if next_paired else ([(t0, o0)] if want == ("dispatched", None) and in_tick else [])
    assert ids(out.pairs) == expected
    if next_paired:
        assert sim.status(o1) == ("dispatched", None)
    events = out.result.tick_notes.get("events", [])
    if want[0] != "planned":
        assert [o0, want[0], want[1], "acme"] in events


def test_head_not_permitted_is_infeasible_and_the_next_job_is_paired():
    other = O(5, h="cargo")  # another company's order, no cooperation: never permitted for acme
    sim = Sim()
    sim.install({t0: [str(other["_id"]), o1]}, [other, O1])
    out = sim.dispatch([T0], [other, O1], 6)
    assert sim.status(str(other["_id"])) == ("failed", "infeasible")
    assert ids(out.pairs) == [(t0, o1)]


def test_empty_itinerary_sleeps_and_a_busy_truck_gets_nothing():
    sim = Sim()
    sim.install({t0: [o0]}, [O0, O1])
    out = sim.dispatch([T9], [O0, O1], 6)  # t9 has no itinerary, t0 is busy (not in the tick)
    assert out.pairs == [] and sim.status(o0) == ("planned", None)


def test_refused_dispatch_is_retried_once_then_failed_refused():
    sim = Sim()
    sim.install({t0: [o0, o1]}, [O0, O1])
    assert ids(sim.dispatch([T0], [O0, O1], 6).pairs) == [(t0, o0)]
    free = {o0: L(O0, state="unassigned")}
    # the truck is free again and the order still unassigned and unclaimed: refused -> retried
    assert ids(sim.dispatch([T0], [O0, O1], 12, lookup=free).pairs) == [(t0, o0)]
    assert sim.status(o0) == ("dispatched", None)
    # refused again: failed, and the truck moves on to its next job
    out = sim.dispatch([T0], [O0, O1], 18, lookup=free)
    assert sim.status(o0) == ("failed", "refused")
    assert ids(out.pairs) == [(t0, o1)]


def test_dispatched_jobs_settle_from_the_lookup_even_while_the_truck_is_busy():
    sim = Sim()
    o2 = O(2)
    sim.install({t0: [o0, o1], t9: [str(o2["_id"])]}, [O0, O1, o2])
    sim.dispatch([T0, T9], [O0, O1, o2], 6)
    lookup = {o0: L(O0, state="completed", truck_id=t0), str(o2["_id"]): L(o2, state="cancelled")}
    out = sim.dispatch([], [], 12, lookup=lookup)  # both trucks busy
    assert sim.status(o0) == ("done", None)
    assert sim.status(str(o2["_id"])) == ("failed", "cancelled")
    assert out.pairs == []
    # in progress by our truck: stays dispatched (and does not block the truck's next head)
    sim2 = Sim()
    sim2.install({t0: [o0, o1]}, [O0, O1])
    sim2.dispatch([T0], [O0, O1], 6)
    out = sim2.dispatch([T0], [O1], 12, lookup={o0: L(O0, state="dropoff_in_progress", truck_id=t0)})
    assert sim2.status(o0) == ("dispatched", None) and ids(out.pairs) == [(t0, o1)]


# ------------------------------------------------------------------------------ prefetch


def test_prefetch_is_oldest_dispatched_plus_head_per_truck_in_truck_order():
    sim = Sim()
    o2, o3 = O(2), O(3)
    sim.install({t0: [o0, o1, str(o2["_id"])], t9: [str(o3["_id"])]}, [O0, O1, o2, o3])
    out = sim.dispatch([T0], [O0, O1, o2, o3], 6)
    assert ids(out.pairs) == [(t0, o0)]
    want = {t0: [o0, o1], t9: [str(o3["_id"])]}
    assert list(out.result.prefetch) == [j for t in sorted(want) for j in want[t]]
    assert len(out.result.prefetch) <= 2 * 2


def test_prefetch_respects_the_cap(monkeypatch):
    monkeypatch.setattr(nfd, "PREFETCH_MAX", 3)
    trucks = [T(k) for k in range(4)]
    orders = [O(10 + k) for k in range(8)]
    sim = Sim()
    sim.install({str(trucks[k]["_id"]): [str(orders[2 * k]["_id"]), str(orders[2 * k + 1]["_id"])] for k in range(4)},
                orders)
    out = sim.dispatch(trucks, orders, 6)
    assert len(out.result.prefetch) == 3 and len(set(out.result.prefetch)) == 3


# ----------------------------------------------------------------------- order independence


def _day(seed=3, n_orders=40, n_trucks=9):
    trucks, orders = synthetic_day(seed, n_orders=n_orders, n_trucks=n_trucks)
    return trucks, orders


def released(orders, step):
    """The order documents as the sim shows them at ``step`` (released at their request step)."""
    out = []
    for o in orders:
        d = copy.deepcopy(o)
        d["state"] = "unassigned" if o["meta"]["request_time_step"] <= step else "created"
        out.append(d)
    return out


def test_plan_and_dispatch_are_order_independent():
    trucks, orders = _day()
    a, b = Sim(), Sim()
    pa = a.plan(trucks, orders, 0)
    pb = b.plan(list(reversed(trucks)), list(reversed(orders)), 0)
    assert pa.result.prefetch == pb.result.prefetch
    assert a.store.state()["current"]["itineraries"] == b.store.state()["current"]["itineraries"]
    rel = [o for o in released(orders, 200) if o["state"] == "unassigned"]
    lk = {str(o["_id"]): o for o in released(orders, 200) if str(o["_id"]) in set(pa.result.prefetch)}
    da = a.dispatch(trucks, rel, 200, lookup=lk)
    db = b.dispatch(list(reversed(trucks)), list(reversed(rel)), 200, lookup=dict(reversed(list(lk.items()))))
    assert sorted(ids(da.pairs)) == sorted(ids(db.pairs)) and ids(da.pairs) == sorted(ids(da.pairs))
    assert a.store.state()["status"] == b.store.state()["status"]
    assert da.result.tick_notes == db.result.tick_notes


# ------------------------------------------------------------------- plan -> dispatch, re-plan


def test_plan_then_dispatch_through_the_runner_with_a_memory_store():
    trucks, orders = _day()
    sim = Sim()
    p = sim.plan(trucks, orders, 0)
    assert p.pairs == [] and p.result is not None  # a planning tick commits nothing (and is not discarded)
    plan = p.result.tick_notes["plan"]
    assert plan["outcome"] == "solved" and plan["version"] == "d0v1" and set(plan["backends"]) == {"greedy"}
    assert sim.store.current_id == "d0v1" and plan["planned"] > 0
    heads = {t: sim.store.itinerary(t)[0] for t in sim.inst._run.trucks}
    assert set(heads.values()) <= set(p.result.prefetch)
    step = 340
    view = released(orders, step)
    lookup = {str(o["_id"]): o for o in view if str(o["_id"]) in set(p.result.prefetch)}
    d = sim.dispatch(trucks, [o for o in view if o["state"] == "unassigned"], step, lookup=lookup)
    assert d.pairs, "every released head is paired"
    for tid, jid in ids(d.pairs):
        assert heads[tid] == jid and sim.status(jid) == ("dispatched", None)
    snap = sim.stats["day_plan"].snapshot()
    assert snap["totals"]["plan_jobs_dispatched"] == len(d.pairs) and snap["totals"]["versions"] == 1


def test_locked_head_survives_a_replan_and_unchanged_instances_are_skipped():
    trucks, orders = _day()
    sim = Sim()
    p = sim.plan(trucks, orders, 0)
    again = sim.plan(trucks, orders, 0, reason="interval")
    assert again.result.tick_notes["plan"]["outcome"] == "skipped"
    step = 340
    view = released(orders, step)
    lookup = {str(o["_id"]): o for o in view if str(o["_id"]) in set(p.result.prefetch)}
    d = sim.dispatch(trucks, [o for o in view if o["state"] == "unassigned"], step, lookup=lookup)
    dispatched = dict(ids(d.pairs))
    assert dispatched
    # the dispatched trucks are busy now; their orders are engaged hauls
    engaged = {str(t["_id"]): dict(o, state="assigned", truck=str(t["_id"])) for t, o in d.pairs}
    open_orders = [o for o in view if str(o["_id"]) not in set(dispatched.values())]
    r = sim.plan(trucks, open_orders, 342, engaged=engaged, reason="interval")
    note = r.result.tick_notes["plan"]
    assert note["outcome"] == "solved" and note["version"] == "d0v2"
    for tid, jid in dispatched.items():
        assert sim.store.itinerary(tid)[0] == jid and sim.status(jid) == ("dispatched", None)


def test_a_failed_solve_keeps_the_previous_version(monkeypatch):
    trucks, orders = _day()
    sim = Sim()
    sim.plan(trucks, orders, 0)
    before = sim.store.state()
    real = plan_runner.solve

    def crashing(inputs, **kw):
        return real(inputs, **dict(kw, child_args=["-c", "import sys; sys.exit(1)"]))

    def hanging(inputs, **kw):
        return real(inputs, **dict(kw, timeout_s=0.5, child_args=["-c", "import time; time.sleep(30)"]))

    for fake, reason in ((crashing, "crashed"), (hanging, "timeout")):
        monkeypatch.setattr(plan_runner, "solve", fake)
        out = sim.plan(trucks[:-1], orders, 18, reason="interval")  # a changed truck set: not skipped
        note = out.result.tick_notes["plan"]
        assert (note["outcome"], note["reason"]) == ("failed", reason)
        assert out.pairs == [] and sim.store.state() == before
        assert out.result.prefetch  # the previous version still dispatches
    snap = sim.stats["day_plan"].snapshot()
    assert snap["totals"]["replans_failed"] == {"crashed": 1, "timeout": 1}


def test_agent_restart_reloads_the_plan_and_dispatches_identically():
    trucks, orders = _day()
    sim = Sim()
    p = sim.plan(trucks, orders, 0)
    view = released(orders, 200)
    lookup = {str(o["_id"]): o for o in view if str(o["_id"]) in set(p.result.prefetch)}
    sim.dispatch(trucks, [o for o in view if o["state"] == "unassigned"], 200, lookup=lookup)
    kv_before = copy.deepcopy(sim.kv)
    view = released(orders, 340)
    lookup = {str(o["_id"]): o for o in view}
    open_ = [o for o in view if o["state"] == "unassigned"]
    old = sim.dispatch(trucks, open_, 340, lookup=lookup)
    restarted = Sim(kv=kv_before)  # a new instance; the record is all it has
    new = restarted.dispatch(trucks, open_, 340, lookup=lookup)
    assert ids(new.pairs) == ids(old.pairs) and new.pairs
    assert restarted.store.state()["status"] == sim.store.state()["status"]
    assert "rebuild" in new.result.tick_notes and "rebuild" not in old.result.tick_notes
    assert restarted.stats["day_plan"].snapshot()["restarts"] == 1


class _DeadRedis:
    """A redis client whose every call fails like a refused connection."""

    def __getattr__(self, name):
        def fail(*_a, **_k):
            raise ConnectionRefusedError("redis down")
        return fail


def test_an_unreachable_redis_never_fails_a_tick():
    trucks, orders = _day()
    sim = Sim()
    sim.inst.store_factory = lambda rid: RedisPlanStore(rid, client=_DeadRedis())
    p = sim.plan(trucks, orders, 0)
    assert p.result is not None and p.result.tick_notes["plan"]["outcome"] == "solved"
    assert "store_failed" in p.result.tick_notes and sim.store.dirty
    view = released(orders, 340)
    d = sim.dispatch(trucks, [o for o in view if o["state"] == "unassigned"], 340)
    assert d.pairs and "store_failed" in d.result.tick_notes


def test_redis_store_uses_short_timeouts_and_offline_stores_are_private():
    store = nfd.default_store_factory("run_x")
    assert isinstance(store, RedisPlanStore) and store._socket_timeout == 2.0
    a, b = nfd.default_store_factory(""), nfd.default_store_factory("")
    assert isinstance(a, MemoryPlanStore) and a.kv is not b.kv


# ------------------------------------------------------------------------------- R6, bind


def test_a_planning_tick_with_pairs_is_discarded_r6(monkeypatch):
    from apps.container_logistics.policies.dispatch.result import Result

    sim = Sim()
    monkeypatch.setattr(type(sim.inst), "_plan_tick",
                        lambda self, tick, params, st, notes: Result(pairs=((t0, o0),)))
    before = sim.runner.stats["violations"]
    out = sim.plan([T0], [O0], 0)
    assert out.pairs == [] and out.result is None and sim.runner.stats["violations"] == before + 1


def test_bind_declares_planning_pairs_run_stats_and_reads_its_world_keys():
    structure = fx.cooperation_structure([("acme", "cargo")], ["acme", "cargo"])
    prof = dict(profile(structure), working_hours={"axis": "elapsed", "hauliers": {"acme": [21600, 72000]}},
                unassigned_order_max_wait_steps=360)
    runner = build_runner_for_policy("NetworkFlowDay", {"nf_replan_every_steps": 12, "nf_max_hold_s": 40},
                                     profile=prof)
    decl = runner.tree.declaration
    assert dict(decl.planning) == {"at_day_start": True, "every_steps": 12, "foresight": "day",
                                   "max_hold_seconds": 40}
    assert decl.cross_company == frozenset({("acme", "cargo"), ("cargo", "acme")})
    assert decl.run_stats is DayPlanStats and decl.run_name_segments == ("coop",)
    assert {"cooperation", "working_hours", "unassigned_order_max_wait_steps"} <= set(runner.tree.world_reads)
    prov = runner.tree.top.declaration.provenance
    assert prov["badge"] == "central" and prov["backend"] == "auto" and prov["time_model"].startswith("blake2b")


def test_params_validate_backend_and_hold_and_offer_no_choice_arms():
    spec = get_policy_spec("planner", "NetworkFlowDay")
    with pytest.raises(PolicyParamsError, match="nf_backend"):
        spec.params_cls.from_mapping({"nf_backend": "cplex"}, where="t")
    with pytest.raises(PolicyParamsError, match="nf_time_limit_s"):
        spec.params_cls.from_mapping({"nf_time_limit_s": 30.0, "nf_max_hold_s": 30}, where="t")
    for b in nfd.BACKENDS:
        spec.params_cls.from_mapping({"nf_backend": b}, where="t")
    schema = spec.params_cls.json_schema()
    assert all("enum" not in p for p in schema["properties"].values())  # M7: no battery arm per backend
    assert spec.top_only and spec.requires == ("ortools",)


def test_catalog_reports_availability_honestly():
    entry = next(p for p in policy_catalog()["planner"]["policies"] if p["name"] == "NetworkFlowDay")
    assert entry["top_only"] is True
    assert entry["available"] is HAS_ORTOOLS and (entry["missing"] == ([] if HAS_ORTOOLS else ["ortools"]))


# ---------------------------------------------------------------------- OR-Tools (scratch venv)


@pytest.mark.skipif(not HAS_ORTOOLS, reason="OR-Tools not importable here (scratch venv only)")
def test_min_cost_flow_plan_is_exact_and_deterministic():
    trucks, orders = _day(n_orders=60, n_trucks=12)
    a, b = Sim({"nf_backend": "auto"}), Sim({"nf_backend": "auto"})
    pa, pb = a.plan(trucks, orders, 0), b.plan(list(reversed(trucks)), orders, 0)
    na = pa.result.tick_notes["plan"]
    assert na["outcome"] == "solved" and set(na["backends"]) == {"mincostflow"} and na["deterministic"]
    assert a.store.state()["current"]["itineraries"] == b.store.state()["current"]["itineraries"]
    g = Sim()
    g.plan(trucks, orders, 0)
    assert na["planned"] >= g.store.kpi(0)["planned"]
