"""Day-plan solve child end to end (plan §4 N3 tests; addendum B2, M5; N0 follow-ups).

Needs OR-Tools: skipped where it is absent (run under the scratch venv). A 200-job
synthetic day through ``runner.solve``: the exact min-cost flow and the MIP agree, every
chain is rooted at a truck and respects the arc rule, differing working hours route a
component to the MIP, a MIP that cannot deliver falls back to greedy, and the stability
bonus stops re-plans reshuffling ties.
"""

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("ortools")

from apps.container_logistics.policies.solver_lib.day_plan import child, runner  # noqa: E402
from apps.container_logistics.policies.solver_lib.day_plan import store as plan_store  # noqa: E402
from apps.container_logistics.policies.solver_lib.day_plan.instance import (  # noqa: E402
    DayPlanParams,
    build_inputs,
    store_job_fields,
)
from apps.container_logistics.policies.solver_lib.day_plan.time_model import TimeModel  # noqa: E402

from tests._day_plan_fixtures import STEP_S, synthetic_day  # noqa: E402

TM = TimeModel.default()
ARM = {"acme": ["cargo"], "cargo": ["acme"]}
HOURS = {"axis": "elapsed", "hauliers": {h: [6 * 3600, 22 * 3600] for h in ("acme", "cargo", "delta")}}


def _build(trucks, orders, *, hours=HOURS, state=None, now_s=0.0, params=None, arm=ARM):
    return build_inputs(trucks, {}, orders, state, TM, params, now_s=now_s, day=0, window_end_s=86400.0,
                        step_seconds=STEP_S, cooperation_arm=arm, working_hours=hours,
                        max_wait_steps=360, cadence_steps=6)


@pytest.fixture(scope="module")
def day200():
    trucks, orders = synthetic_day(21, n_orders=200, n_trucks=40)
    return trucks, orders, _build(trucks, orders, hours=None)


def _check_chains(inp, out):
    """Every chain obeys the arc rule the child documents (an independent re-check)."""
    jobs = {j["id"]: j for j in inp["jobs"]}
    trucks = {t["id"]: t for t in inp["trucks"]}
    W = inp["params"]["wait_limit_min"] * 60
    for tid, chain in out["itineraries"].items():
        t = trucks[tid]
        first = jobs[chain[0]]
        assert t["class"] in first["classes"] and first["r"] >= t["ready_s"] - first["slack"]
        for a, b in zip(chain, chain[1:]):
            ja, jb = jobs[a], jobs[b]
            assert t["class"] in jb["classes"]
            assert ja["e"] - ja["slack"] <= jb["r"] <= ja["e"] + W
            assert (jb["e"], b) > (ja["e"], a) or jb["e"] > ja["e"]


def test_mcf_and_mip_agree_on_a_200_job_day(day200):
    _, _, inp = day200
    mcf = runner.solve(inp, backend="auto", time_limit_s=60)
    same_arcs = copy.deepcopy(inp)  # the MIP's default K caps would solve a smaller arc set
    same_arcs["params"].update(mip_k_succ=None, mip_k_start=None)
    mip = runner.solve(same_arcs, backend="mip", time_limit_s=60)
    assert {r["backend"] for r in mcf["stats"]["components"]} == {"mincostflow"}
    assert {r["backend"] for r in mip["stats"]["components"]} == {"mip"}
    for a, b in zip(mcf["stats"]["components"], mip["stats"]["components"]):
        assert a["classes"] == b["classes"]
        assert b["status"] == 0, "the MIP must reach OPTIMAL on 200 jobs"
        # integer metre costs vs floats: equal within 0.5 m per arc of the plan (M5)
        assert abs(a["objective"] - b["objective"]) <= 0.5 * (a["planned"] + 1) + 1e-6
        assert a["served"] == b["served"]
    assert len(mcf["unplanned"]) == len(mip["unplanned"])
    assert mcf["stats"]["empty_m"] == pytest.approx(mip["stats"]["empty_m"], abs=0.5 * len(inp["jobs"]))
    for out in (mcf, mip):
        assert runner.validate_output(inp, out) == []
        assert out["stats"]["deterministic"] is True
        _check_chains(inp, out)
    # the exact plan is at least as good as greedy on the same arcs
    greedy = runner.solve(inp, backend="greedy")
    pen = inp["params"]["defer_penalty_km"] * 1000
    cost = lambda o: o["stats"]["empty_m"] + pen * len(o["unplanned"])  # noqa: E731
    assert cost(mcf) <= cost(greedy) + 1e-6
    _check_chains(inp, greedy)


def test_reported_km_are_the_true_arc_costs(day200):
    _, _, inp = day200
    out = child.solve_inputs(inp, backend="auto")
    cost = {}
    for comp in inp["components"]:
        jobs = sorted((j for j in inp["jobs"] if j["owner_class"] in comp), key=lambda x: x["id"])
        trucks = sorted((t for t in inp["trucks"] if t["class"] in comp), key=lambda x: x["id"])
        a = child._component_arcs(inp, jobs, trucks, k_succ=None, k_start=None)  # unit trucks
        ids = [x["id"] for x in jobs]
        cost.update({(ids[x], ids[y]): c for x, y, c in zip(a["src"].tolist(), a["dst"].tolist(), a["cost"].tolist())})
        cost.update({(trucks[x]["id"], ids[y]): c
                     for x, y, c in zip(a["ssrc"].tolist(), a["sdst"].tolist(), a["scost"].tolist())})
    total = 0.0
    for tid, chain in out["itineraries"].items():
        total += cost[(tid, chain[0])] + sum(cost[x] for x in zip(chain, chain[1:]))
    assert out["stats"]["empty_m"] == pytest.approx(total, abs=1.0)
    assert sum(out["stats"]["first_leg_m"].values()) <= total + 1e-6


def test_differing_hours_in_one_component_route_to_the_mip():
    trucks, orders = synthetic_day(22, n_orders=80, n_trucks=12, hauliers=("acme", "cargo"))
    hours = {"axis": "elapsed", "hauliers": {"acme": [6 * 3600, 20 * 3600], "cargo": [8 * 3600, 22 * 3600]}}
    inp = _build(trucks, orders, hours=hours)
    out = runner.solve(inp, backend="auto", time_limit_s=30)
    (row,) = out["stats"]["components"]
    assert row["backend"] == "mip" and "differ" in row["reason"]
    _check_chains(inp, out)
    same = _build(trucks, orders, hours={"axis": "elapsed", "hauliers": {h: [6 * 3600, 22 * 3600] for h in ("acme", "cargo")}})
    assert runner.solve(same, backend="auto")["stats"]["components"][0]["backend"] == "mincostflow"


def test_mip_over_its_arc_budget_falls_back_to_greedy(day200):
    _, _, inp = day200
    inp = copy.deepcopy(inp)
    inp["params"].update(mip_arc_budget=10, guard_threshold=0.5)  # greedy serves ~84 % here
    out = runner.solve(inp, backend="mip", time_limit_s=30)
    rows = out["stats"]["components"]
    assert all(r["backend"] == "greedy" and r["fallback"] == "arc_budget" and r["fallback_guard"]["ok"]
               for r in rows if r["orders"])
    assert out["stats"]["status"] == "degraded"
    assert runner.validate_output(inp, out) == []


def test_a_fallback_below_the_quality_guard_refuses_the_solve(day200, monkeypatch):
    _, _, inp = day200
    inp = copy.deepcopy(inp)
    inp["params"]["mip_arc_budget"] = 10
    monkeypatch.setattr(child, "_greedy", lambda params, jobs: {s: [] for s in params["start_nodes"]})
    res = child.solve_inputs(inp, backend="mip")
    assert res["ok"] is False and res["reason"] == "poor_incumbent"
    row = res["stats"]["components"][-1]
    assert row["fallback_guard"]["ok"] is False


def test_merged_identical_trucks_keep_their_previous_chains():
    """Identical trucks are one start node of capacity c (coop_dispatch E2). Even when the
    previous version gave a member the chain the solver would hand another member, the
    split back to trucks follows the previous head, so an unchanged re-plan moves nothing
    with or without the stability bonus."""
    trucks, orders = synthetic_day(23, n_orders=150, n_trucks=30, jitter=False)
    first = _build(trucks, orders, params={"stability_bonus_m": 0.0})
    out = runner.solve(first, backend="auto")
    assert any(r["starts"] < r["trucks"] for r in out["stats"]["components"]), "trucks were merged"
    by_key = {}
    for t in first["trucks"]:
        if t["id"] in out["itineraries"]:
            by_key.setdefault((t["class"], tuple(t["loc"]), t["ready_s"]), []).append(t["id"])
    a, b = next(v for v in by_key.values() if len(v) >= 2)[:2]
    prev = dict(out["itineraries"])
    prev[a], prev[b] = out["itineraries"][b], out["itineraries"][a]
    for bonus in (0.0, 50.0):
        st = plan_store.MemoryPlanStore("stab", kv=plan_store.MemoryKV())
        st.write_version(0, prev, store_job_fields(first))
        again = _build(trucks, orders, state=st.state(), params={"stability_bonus_m": bonus})
        re = runner.solve(again, backend="auto")
        assert st.write_version(0, re["itineraries"], store_job_fields(again))["jobs_moved"] == 0, bonus
        assert re["stats"]["empty_m"] == pytest.approx(out["stats"]["empty_m"], abs=1.0)


def _two_truck_tie(prev_arcs, bonus):
    """Two trucks of one class at the SAME spot but different ready times (so NOT merged),
    two jobs picked up right there at the same time: swapping them costs exactly the same."""
    cls = "acme|20ft|"
    loc = [1.3, 103.8]
    jobs = [dict(id=f"j{k}", owner="acme", owner_class=cls, classes=[cls], r=10_000.0, e=20_000.0,
                 slack=0.0, pickup=loc, dropoff=[1.3 + 0.01 * (k + 1), 103.8]) for k in range(2)]
    trucks = [dict(id="t0", ready_s=0.0, loc=loc, anchor="position", anchor_job=None, **{"class": cls}),
              dict(id="t1", ready_s=500.0, loc=loc, anchor="position", anchor_job=None, **{"class": cls})]
    params = DayPlanParams(stability_bonus_m=bonus, wait_limit_min=1.0).as_dict()
    inp = dict(day=0, params=params, classes={cls: {"haulier": "acme"}}, class_attrs={cls: ["20ft", [], None]},
               cooperation=[[cls, cls]], components=[[cls]], trucks=trucks, jobs=jobs, prev_arcs=prev_arcs)
    return inp, runner.solve(inp, backend="auto")


def test_stability_bonus_keeps_an_equal_cost_previous_plan_on_distinct_trucks():
    """Where merging cannot help (different ready times), the bonus is what keeps the
    previous one of two equal-cost plans (N0: 1,975 -> 73 moved)."""
    _, base = _two_truck_tie([], 0.0)
    assert base["stats"]["components"][0]["starts"] == 2  # not merged
    canon = base["itineraries"]
    assert sorted(len(c) for c in canon.values()) == [1, 1]
    swapped = {"t0": canon["t1"], "t1": canon["t0"]}
    prev_arcs = sorted([[t, c[0]] for t, c in swapped.items()])
    _, no_bonus = _two_truck_tie(prev_arcs, 0.0)
    _, bonus = _two_truck_tie(prev_arcs, 50.0)
    assert no_bonus["itineraries"] == canon != swapped     # without it the tie flips back
    assert bonus["itineraries"] == swapped                 # with it the previous plan stays
    assert bonus["stats"]["empty_m"] == pytest.approx(no_bonus["stats"]["empty_m"])


def test_greedy_backend_runs_without_importing_the_solvers(day200):
    _, _, inp = day200
    out = child.solve_inputs(json.loads(json.dumps(inp)), backend="greedy")
    assert out["ok"] and set(out["stats"]["backends"]) == {"greedy"}


# ----------------------------------------------------------------- FX2: M2 deadline, windows


def _staggered(seed=31, n_orders=150, n_trucks=24):
    """Three hauliers, own-only cooperation, hours staggered per haulier: three MIP components."""
    trucks, orders = synthetic_day(seed, n_orders=n_orders, n_trucks=n_trucks)
    hours = {"axis": "elapsed", "hauliers": {"acme": [6 * 3600, 22 * 3600], "cargo": [7 * 3600, 23 * 3600],
                                             "delta": [5 * 3600, 21 * 3600]}}
    return _build(trucks, orders, hours=hours, arm={"acme": ["cargo"], "cargo": ["acme"]})


def test_one_deadline_is_shared_across_mip_components():
    """M2: the per-component limit no longer adds up past the child's kill. With a deadline
    the MIP components share what is left, the rest goes greedy, and the answer comes in time."""
    import time

    inp = _staggered()
    t = time.time()
    out = child.solve_inputs(inp, backend="mip", time_limit_s=60.0, deadline_epoch=t + 6.0, accept_poor=True)
    assert out["ok"] and time.time() - t < 6.0
    rows = [r for r in out["stats"]["components"] if r["orders"]]
    assert len(rows) >= 2
    budgets = [r["mip_time_limit_s"] for r in rows if "mip_time_limit_s" in r]
    assert sum(budgets) <= 6.0 and all(b < 60 for b in budgets)
    # a deadline no MIP fits into: every component greedy, stamped
    tight = child.solve_inputs(inp, backend="mip", time_limit_s=60.0, deadline_epoch=time.time() + 1.5,
                               accept_poor=True)
    assert tight["ok"] and {r.get("fallback") for r in tight["stats"]["components"] if r["orders"]} == {"deadline"}
    assert tight["stats"]["fallbacks"]["deadline"] >= 2 and tight["stats"]["status"] == "degraded"


def test_the_runner_hands_the_child_its_deadline():
    inp = _staggered()
    out = runner.solve(inp, backend="mip", time_limit_s=60.0, timeout_s=8.0, accept_poor=True)
    assert out["stats"]["wall_ms"] < 8000
    assert all(r.get("mip_time_limit_s", 0) <= 8 for r in out["stats"]["components"])


def test_accept_poor_keeps_a_greedy_plan_under_the_guard(day200, monkeypatch):
    _, _, inp = day200
    inp = copy.deepcopy(inp)
    inp["params"]["mip_arc_budget"] = 10
    real = child._greedy
    monkeypatch.setattr(child, "_greedy", lambda params, jobs: {
        s: c[:1] for s, c in real(params, jobs).items()})  # one job per truck: under the guard
    assert child.solve_inputs(inp, backend="mip")["ok"] is False
    kept = child.solve_inputs(inp, backend="mip", accept_poor=True)
    assert kept["ok"] and kept["stats"]["accepted_poor"] is True and kept["itineraries"]
    assert any(r.get("accepted_poor") for r in kept["stats"]["components"])


def test_overnight_second_window_chains_join_the_trucks_itinerary():
    trucks, orders = synthetic_day(33, n_orders=120, n_trucks=10, hauliers=("acme",))
    hours = {"axis": "elapsed", "hauliers": {"acme": [18 * 3600, 6 * 3600]}}
    inp = _build(trucks, orders, hours=hours, arm=None)
    virtual = [t for t in inp["trucks"] if t.get("of")]
    assert virtual and all(t["id"] == f"{t['of']}@w1" for t in virtual)
    out = runner.solve(inp, backend="auto")
    assert runner.validate_output(inp, out) == []
    assert set(out["itineraries"]) <= {t["id"] for t in inp["trucks"] if not t.get("of")}
    jobs = {j["id"]: j for j in inp["jobs"]}
    both = [c for c in out["itineraries"].values()
            if any(jobs[j]["r"] < 6 * 3600 for j in c) and any(jobs[j]["r"] >= 18 * 3600 for j in c)]
    assert both, "a truck works both windows of one plan"
    for c in both:  # its morning chain comes first
        rs = [jobs[j]["r"] for j in c]
        k = next(i for i, r in enumerate(rs) if r >= 18 * 3600)
        assert all(r < 18 * 3600 for r in rs[:k]) and all(r >= 18 * 3600 for r in rs[k:])
    assert set(out["stats"]["first_leg_m"]) <= set(out["itineraries"])


def test_replan_reports_previous_arc_survival():
    trucks, orders = synthetic_day(23, n_orders=150, n_trucks=30, jitter=False)
    first = _build(trucks, orders)
    out = runner.solve(first, backend="auto")
    assert "prev_arcs" not in out["stats"]
    st = plan_store.MemoryPlanStore("surv", kv=plan_store.MemoryKV())
    st.write_version(0, out["itineraries"], store_job_fields(first))
    again = runner.solve(_build(trucks, orders, state=st.state()), backend="auto")
    pa = again["stats"]["prev_arcs"]
    assert pa["prev_job_arcs"] == pa["prev_job_arcs_feasible"] > 0
    assert pa["prev_start_arcs"] == pa["prev_start_arcs_feasible"] > 0
    assert pa["prev_arcs_kept"] == pa["prev_job_arcs"] + pa["prev_start_arcs"]
