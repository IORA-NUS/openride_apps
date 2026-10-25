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
    arcs_by_comp = {}
    for comp in inp["components"]:
        jobs = sorted((j for j in inp["jobs"] if j["owner_class"] in comp), key=lambda x: x["id"])
        trucks = sorted((t for t in inp["trucks"] if t["class"] in comp), key=lambda x: x["id"])
        arcs_by_comp[tuple(comp)] = child._component_arcs(inp, jobs, trucks, k_succ=None, k_start=None)
    total = 0.0
    for tid, chain in out["itineraries"].items():
        a = next(v for v in arcs_by_comp.values() if (tid, chain[0]) in v["start_cost"])
        total += a["start_cost"][(tid, chain[0])] + sum(a["edge_cost"][x] for x in zip(chain, chain[1:]))
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


def test_stability_bonus_keeps_an_equal_cost_previous_plan_in_place():
    """Two identical trucks make two optimal plans (swap their days). The previous version
    holds the one the solver would NOT pick on its own: without the bonus the re-plan
    swaps them back (churn, N0: 1,975 moved jobs); with it, nothing moves."""
    trucks, orders = synthetic_day(23, n_orders=150, n_trucks=30, jitter=False)
    first = _build(trucks, orders, params={"stability_bonus_m": 0.0})
    out = runner.solve(first, backend="auto")
    by_key = {}
    for t in first["trucks"]:
        if t["id"] in out["itineraries"]:
            by_key.setdefault((t["class"], tuple(t["loc"]), t["ready_s"]), []).append(t["id"])
    a, b = next(v for v in by_key.values() if len(v) >= 2)[:2]
    prev = dict(out["itineraries"])
    prev[a], prev[b] = out["itineraries"][b], out["itineraries"][a]
    moved = {}
    for bonus in (0.0, 50.0):
        st = plan_store.MemoryPlanStore("stab", kv=plan_store.MemoryKV())
        st.write_version(0, prev, store_job_fields(first))
        again = _build(trucks, orders, state=st.state(), params={"stability_bonus_m": bonus})
        assert again["prev_arcs"], "the previous version's arcs reach the child"
        re = runner.solve(again, backend="auto")
        moved[bonus] = st.write_version(0, re["itineraries"], store_job_fields(again))["jobs_moved"]
        # the bonus changes which optimum is picked, never its true cost
        assert re["stats"]["empty_m"] == pytest.approx(out["stats"]["empty_m"], abs=1.0)
    assert moved[0.0] >= len(prev[a]) + len(prev[b]) > 0
    assert moved[50.0] == 0


def test_greedy_backend_runs_without_importing_the_solvers(day200):
    _, _, inp = day200
    out = child.solve_inputs(json.loads(json.dumps(inp)), backend="greedy")
    assert out["ok"] and set(out["stats"]["backends"]) == {"greedy"}
