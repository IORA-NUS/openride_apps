"""Day-plan instance builder (plan §4 ``instance.py``; critique M5, M11, D3, D5, D6, D8, D9, D12f).

Pure data, no OR-Tools: runs under the main venv.
"""

from __future__ import annotations

import copy
import json
import random

import pytest

from apps.container_logistics.policies.solver_lib.day_plan import store as plan_store
from apps.container_logistics.policies.solver_lib.day_plan.instance import (
    DayPlanParams,
    PlanInputs,
    build_inputs,
    class_id,
    store_job_fields,
)
from apps.container_logistics.policies.solver_lib.day_plan.time_model import TimeModel, latency_seconds

from tests._day_plan_fixtures import FACILITIES, STEP_S, oid, order, point, synthetic_day, truck

TM = TimeModel.default()
HOURS = {"axis": "elapsed", "hauliers": {"acme": [6 * 3600, 20 * 3600]}}
CLS = class_id("acme", "20ft", ["MBS", "West Coast"])


def build(trucks, orders, *, engaged=None, state=None, now_s=0.0, day=0, arm=None, hours=None,
          max_wait=None, params=None, cadence=6, **kw):
    return build_inputs(trucks, engaged or {}, orders, state, TM, params, now_s=now_s, day=day,
                        window_end_s=(day + 1) * 86400.0, step_seconds=STEP_S, cooperation_arm=arm,
                        working_hours=hours, max_wait_steps=max_wait, cadence_steps=cadence, **kw)


def job(inputs, n):
    return next(j for j in inputs["jobs"] if j["id"] == oid("order", n))


# ------------------------------------------------------------------------- classes


def test_classes_are_haulier_size_areas_and_owner_without_trucks_gets_a_synthetic_class():
    trucks = [truck(0, "acme"), truck(1, "acme", size="40ft"), truck(2, "acme", areas=()), truck(3, "acme")]
    orders = [order(0, "acme", 100, "port_000", "customer_000"), order(1, "cargo", 100, "port_000", "customer_001")]
    inp = build(trucks, orders, arm={"acme": ["cargo"]})
    assert sorted(inp["classes"]) == sorted([CLS, class_id("acme", "40ft", ["MBS", "West Coast"]),
                                             class_id("acme", "20ft", []), "cargo|*none*"])
    assert inp["classes"][CLS]["trucks"] == sorted([oid("truck", 0), oid("truck", 3)])
    syn = inp["classes"]["cargo|*none*"]
    assert syn["synthetic"] and syn["trucks"] == []
    assert job(inp, 1)["owner_class"] == "cargo|*none*"
    # every class a truck class or the synthetic owner class; the arm lets acme serve cargo
    assert set(job(inp, 1)["classes"]) == {c for c in inp["classes"] if c.startswith("acme")}
    assert inp["class_attrs"][CLS] == ["20ft", ["MBS", "West Coast"], None]


def test_cooperation_is_directional_and_lifted_to_classes():
    trucks = [truck(0, "acme"), truck(1, "cargo")]
    orders = [order(0, "acme", 100, "port_000", "customer_000"), order(1, "cargo", 100, "port_000", "customer_001")]
    inp = build(trucks, orders, arm={"acme": ["cargo"]})  # acme's trucks may serve cargo's orders
    ca, cc = class_id("acme", "20ft", ["MBS", "West Coast"]), class_id("cargo", "20ft", ["MBS", "West Coast"])
    assert job(inp, 0)["classes"] == [ca]
    assert job(inp, 1)["classes"] == sorted([ca, cc])
    assert [cc, ca] in inp["cooperation"] and [ca, cc] not in inp["cooperation"]
    assert inp["components"] == [sorted([ca, cc])]
    own = build(trucks, orders, arm=None)
    assert job(own, 1)["classes"] == [cc] and own["components"] == [[ca], [cc]]


def test_physical_compatibility_size_and_restricted_areas():
    trucks = [truck(0, "acme", areas=("customer_001",))]
    orders = [order(0, "acme", 100, "port_000", "customer_000", size="40ft"),
              order(1, "acme", 100, "port_000", "customer_001"),
              order(2, "acme", 100, "port_000", "customer_002")]
    inp = build(trucks, orders)
    assert inp["excluded"][oid("order", 0)] == "no_eligible_class"   # 20ft chassis, 40ft box
    assert inp["excluded"][oid("order", 1)] == "no_eligible_class"   # restricted facility
    assert [j["id"] for j in inp["jobs"]] == [oid("order", 2)]


# -------------------------------------------------------------- axis and hours


def test_release_end_slack_on_the_elapsed_axis():
    o = order(0, "acme", 300, "port_000", "customer_000")
    inp = build([truck(0, "acme")], [o], now_s=10 * STEP_S)
    j = job(inp, 0)
    assert j["r_req"] == j["r"] == 300 * STEP_S
    assert j["e"] == pytest.approx(TM.job_end_seconds(300 * STEP_S, "port_000", "customer_000",
                                                      _km("port_000", "customer_000"),
                                                      cadence_steps=6, step_seconds=STEP_S))
    assert j["e"] > j["r"] + latency_seconds(6, STEP_S)
    assert j["slack"] == TM.late_slack_seconds("port_000", "customer_000")
    fixed = build([truck(0, "acme")], [o], params={"late_slack_min": 15})
    assert job(fixed, 0)["slack"] == 900.0
    # an already-released job cannot be dispatched before now (D3)
    late = build([truck(0, "acme")], [o], now_s=320 * STEP_S)
    assert job(late, 0)["r"] == 320 * STEP_S and job(late, 0)["r_req"] == 300 * STEP_S


def _km(a, b):
    from apps.container_logistics.policies.solver_lib.day_plan.time_model import haversine_km
    (lon1, lat1), (lon2, lat2) = FACILITIES[a], FACILITIES[b]
    return haversine_km(lon1, lat1, lon2, lat2)


def test_orders_of_later_days_are_outside_the_window():
    orders = [order(0, "acme", 359, "port_000", "customer_000"), order(1, "acme", 360, "port_000", "customer_000")]
    inp = build([truck(0, "acme")], orders, day=0)
    assert inp["excluded"] == {oid("order", 1): "outside_window"}


def test_working_hours_filter_jobs_per_class_and_set_truck_ready():
    early = order(0, "acme", 60, "port_000", "customer_000")     # 04:00 elapsed: before 06:00
    mid = order(1, "acme", 150, "port_000", "customer_000")      # 10:00
    late = order(2, "acme", 295, "port_000", "customer_000")     # 19:40: ends after 20:00
    inp = build([truck(0, "acme")], [early, mid, late], hours=HOURS)
    assert [j["id"] for j in inp["jobs"]] == [oid("order", 1)]
    assert inp["excluded"][oid("order", 0)] == inp["excluded"][oid("order", 2)] == "no_eligible_class"
    t = inp["trucks"][0]
    assert t["ready_s"] == 6 * 3600 and inp["classes"][CLS]["shift"] == [6 * 3600, 20 * 3600]
    # a planning tick at 00:00 still carries the truck (D6); after 20:00 it is off shift
    off = build([truck(0, "acme")], [mid], hours=HOURS, now_s=20.5 * 3600)
    assert off["excluded_trucks"] == {oid("truck", 0): "off_shift"} and off["jobs"] == []
    # the early job becomes eligible once now is inside the shift (rolled into a re-plan)
    later = build([truck(0, "acme")], [early], hours=HOURS, now_s=7 * 3600)
    assert job(later, 0)["r"] == 7 * 3600


def test_overnight_hours_and_hauliers_without_hours():
    hours = {"axis": "elapsed", "hauliers": {"acme": [22 * 3600, 6 * 3600]}}
    o = order(0, "acme", 15, "port_000", "port_001")   # 01:00 elapsed: inside 22:00-06:00
    inp = build([truck(0, "acme"), truck(1, "cargo")], [o, order(1, "cargo", 150, "port_000", "port_001")],
                hours=hours, now_s=0.0)
    assert inp["classes"][CLS]["shift"] == [-2 * 3600, 6 * 3600]
    cargo = class_id("cargo", "20ft", ["MBS", "West Coast"])
    assert inp["classes"][cargo]["shift"] is None  # no hours: always on
    assert oid("order", 1) in {j["id"] for j in inp["jobs"]}
    with pytest.raises(ValueError, match="axis"):
        build([truck(0, "acme")], [o], hours={"axis": "wall", "hauliers": {}})


def test_overdue_sweep_deadline_excludes_jobs_that_would_be_cancelled_first():
    o = order(0, "acme", 10, "port_000", "customer_000")
    now = (10 + 360 - 3) * STEP_S   # 3 steps before the sweep: dispatch + one cadence does not fit
    inp = build([truck(0, "acme")], [o], now_s=now, max_wait=360)
    assert inp["excluded"] == {oid("order", 0): "deadline"}
    ok = build([truck(0, "acme")], [o], now_s=(10 + 360 - 12) * STEP_S, max_wait=360)
    assert job(ok, 0)["deadline_s"] == (10 + 360) * STEP_S
    assert build([truck(0, "acme")], [o], now_s=now, max_wait=None)["jobs"]


# --------------------------------------------------------------- anchoring / locks


def test_engaged_truck_starts_at_dropoff_when_its_job_ends():
    eng = order(9, "acme", 100, "port_000", "customer_002", state="assigned")
    inp = build([truck(0, "acme")], [order(0, "acme", 200, "port_000", "customer_000")],
                engaged={oid("truck", 0): eng}, now_s=110 * STEP_S)
    t = inp["trucks"][0]
    lon, lat = FACILITIES["customer_002"]
    assert t["anchor"] == "engaged" and t["anchor_job"] == oid("order", 9) and t["loc"] == [lat, lon]
    end = TM.job_end_seconds(100 * STEP_S, "port_000", "customer_002", _km("port_000", "customer_002"),
                             cadence_steps=6, step_seconds=STEP_S)
    assert t["ready_s"] == pytest.approx(max(110 * STEP_S, end))
    # a known dispatch moment replaces the release in the estimate
    d = build([truck(0, "acme")], [], engaged={oid("truck", 0): eng}, now_s=110 * STEP_S,
              dispatched_at_s={oid("order", 9): 105 * STEP_S})
    lat_s = latency_seconds(6, STEP_S)
    assert d["trucks"][0]["ready_s"] == pytest.approx(TM.job_end_seconds(
        105 * STEP_S - lat_s, "port_000", "customer_002", _km("port_000", "customer_002"),
        cadence_steps=6, step_seconds=STEP_S))
    assert d["trucks"][0]["ready_s"] == pytest.approx(105 * STEP_S + (end - 100 * STEP_S - lat_s))


def _store_with(itins, jobs_fields, statuses):
    st = plan_store.MemoryPlanStore("t", kv=plan_store.MemoryKV())
    st.write_version(0, itins, jobs_fields)
    if statuses:
        st.set_statuses(statuses)
    return st


def test_locked_jobs_are_excluded_and_dispatched_job_anchors_the_truck():
    t0 = oid("truck", 0)
    orders = [order(k, "acme", 100 + k, "port_000", "customer_000") for k in range(5)]
    first = build([truck(0, "acme")], orders)
    st = _store_with({t0: [oid("order", k) for k in range(5)]}, store_job_fields(first),
                     [(oid("order", 0), "done", None, t0), (oid("order", 1), "dispatched", None, t0),
                      (oid("order", 2), "failed", "taken", None), (oid("order", 3), "failed", "infeasible", None)])
    inp = build([truck(0, "acme")], orders, state=st.state(), now_s=105 * STEP_S)
    ex = inp["excluded"]
    assert ex[oid("order", 0)] == ex[oid("order", 1)] == "locked"
    assert ex[oid("order", 2)] == "failed:taken"
    assert job(inp, 3)["status"] == "failed:infeasible"     # re-plannable
    assert job(inp, 4)["status"] is None and not job(inp, 4)["new"] and job(inp, 4)["first_day"] == 0
    t = inp["trucks"][0]
    f1 = st.job_fields(oid("order", 1))
    assert t["anchor"] == "dispatched" and t["anchor_job"] == oid("order", 1)
    assert t["loc"] == f1["dropoff"] and t["ready_s"] == max(105 * STEP_S, f1["e"])
    # previous version's open tail -> stability arcs (truck -> first open, then consecutive)
    assert inp["prev_arcs"] == [[t0, oid("order", 4)]]


def test_document_state_decides_openness():
    orders = [order(0, "acme", 100, "port_000", "customer_000", state="assigned"),
              order(1, "acme", 100, "port_000", "customer_000", state="cancelled"),
              order(2, "acme", 100, "port_000", "customer_000", state="created"),
              order(3, "acme", 100, "port_000", "customer_000", state="unassigned")]
    inp = build([truck(0, "acme")], orders)
    assert inp["excluded"] == {oid("order", 0): "not_open", oid("order", 1): "not_open"}


def test_sleeping_trucks_only_when_replan_idle_trucks_is_off():
    t0 = oid("truck", 0)
    orders = [order(0, "acme", 100, "port_000", "customer_000"), order(1, "acme", 200, "port_000", "customer_001")]
    first = build([truck(0, "acme")], orders[:1])
    st = _store_with({t0: [oid("order", 0)]}, store_job_fields(first), [(oid("order", 0), "done", None, t0)])
    on = build([truck(0, "acme")], orders, state=st.state(), now_s=150 * STEP_S)
    off = build([truck(0, "acme")], orders, state=st.state(), now_s=150 * STEP_S,
                params={"replan_idle_trucks": False})
    assert [t["id"] for t in on["trucks"]] == [t0]
    assert off["excluded_trucks"] == {t0: "sleeping"} and off["trucks"] == []


# ------------------------------------------------------- determinism and the hash


def test_input_order_does_not_matter():
    trucks, orders = synthetic_day(3, n_orders=120, n_trucks=25)
    a = build(trucks, orders, arm={"acme": ["cargo"], "cargo": ["acme"]}, hours=HOURS, max_wait=360)
    rng = random.Random(7)
    t2, o2 = copy.deepcopy(trucks), copy.deepcopy(orders)
    rng.shuffle(t2)
    rng.shuffle(o2)
    b = build(t2, o2, arm={"cargo": ["acme"], "acme": ["cargo"]}, hours=HOURS, max_wait=360)
    assert json.dumps(a, sort_keys=False) == json.dumps(b, sort_keys=False)
    assert isinstance(a, PlanInputs) and a.hash == b.hash


def test_hash_ignores_time_but_sees_trucks_positions_jobs_and_failures():
    trucks, orders = synthetic_day(4, n_orders=40, n_trucks=6, hauliers=("acme",))
    h0 = build(trucks, orders).hash
    assert build(trucks, orders, now_s=500.0).hash == h0              # time alone: unchanged
    assert build(trucks + [truck(99, "acme")], orders).hash != h0      # a truck boots in (D8)
    moved = copy.deepcopy(trucks)
    moved[0]["profile"]["current_loc"]["coordinates"][0] += 0.01
    assert build(moved, orders).hash != h0
    nudged = copy.deepcopy(trucks)
    nudged[0]["profile"]["current_loc"]["coordinates"][0] += 1e-7     # below the position bucket
    assert build(nudged, orders).hash == h0
    assert build(trucks, orders + [order(999, "acme", 200, "port_000", "port_001")]).hash != h0
    assert build(trucks, orders, params={"wait_limit_min": 120}).hash != h0


def test_planned_statuses_do_not_change_the_hash_but_failures_do():
    t0 = oid("truck", 0)
    orders = [order(k, "acme", 100 + k, "port_000", "customer_000") for k in range(3)]
    first = build([truck(0, "acme")], orders)
    st = _store_with({t0: [oid("order", 0)]}, store_job_fields(first), [])
    second = build([truck(0, "acme")], orders, state=st.state())
    assert second.hash == build([truck(0, "acme")], orders, state=st.state()).hash
    assert second.hash == first.hash  # planned / unplanned statuses are the plan's own output
    st.set_status(oid("order", 1), "failed", "infeasible")
    assert build([truck(0, "acme")], orders, state=st.state()).hash != second.hash


def test_inputs_are_json_and_real_id_formats_pass_the_store_regex():
    trucks, orders = synthetic_day(5, n_orders=30, n_trucks=5)
    inp = build(trucks, orders)
    assert json.loads(json.dumps(inp)) == inp
    # real ids: Mongo ObjectId hex (run_20261021_175341) and bundle agent ids (N0 extract)
    for rid in ("6ad88bacdebbc8505fe6d397", "6ad88bacdebbc8505fe6cf73", "order_000000", "truck_000999"):
        assert plan_store._ID_RE.match(rid)
    for x in inp["jobs"] + inp["trucks"]:
        assert plan_store._ID_RE.match(x["id"])


def test_params_coerce_rejects_unknown_keys():
    assert DayPlanParams.coerce({"wait_limit_min": 60}).wait_limit_min == 60
    assert DayPlanParams().wait_limit_min == 240 and DayPlanParams().k_succ is None
    with pytest.raises(ValueError, match="unknown"):
        DayPlanParams.coerce({"nope": 1})


def test_duplicate_documents_are_refused():
    t = truck(0, "acme")
    with pytest.raises(ValueError, match="twice"):
        build([t, t], [])
    o = order(0, "acme", 100, "port_000", "customer_000")
    with pytest.raises(ValueError, match="twice"):
        build([t], [o, o])


def test_store_job_fields_avoid_reserved_names():
    trucks, orders = synthetic_day(6, n_orders=10, n_trucks=3)
    f = store_job_fields(build(trucks, orders))
    for v in f.values():
        assert not set(v) & {"truck", "seq", "status", "reason"}
    _ = point  # fixture helper kept importable
