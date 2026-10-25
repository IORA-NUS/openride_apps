"""Backend choice per component, the MIP quality guard, rounding tolerance and the child
resource guards (plan addendum B2, M5, minor 8).

Skipped where OR-Tools is absent.

Verified to have teeth (mutate / see it fail / restore): making ``mcf_applicable`` ignore
``class_attrs`` failed 13 cases (``test_differing_hours_route_to_the_mip`` among them);
``quality_guard`` always ok failed 2; dropping the depot vehicles from the served-bound
relaxation failed 31 (the bound and tightness tests).
"""

from __future__ import annotations

import random
import subprocess
import sys

import pytest

pytest.importorskip("ortools")

from apps.container_logistics.policies.solver_lib.coop_dispatch import backends  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch import network_flow_solver as nfs  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch.backends import (  # noqa: E402
    component_params,
    quality_guard,
    served_reference,
    served_upper_bound,
    solve_by_component,
)
from apps.container_logistics.policies.solver_lib.coop_dispatch.limits import (  # noqa: E402
    ArcBudgetExceeded,
    check_mip_arc_budget,
    memory_cap_preexec,
    mip_arc_count,
)
from apps.container_logistics.policies.solver_lib.coop_dispatch.min_cost_flow import (  # noqa: E402
    cooperation_components,
    mcf_applicable,
    solve_min_cost_flow,
)
from tests.test_coop_dispatch_options import check_solution, mip, tiny_instance  # noqa: E402


def _served(r, p):
    return len(p["orders"]) - len(r.get("unplanned", []))


def test_mcf_applicable():
    full = {(a, b) for a in "AB" for b in "AB"}
    assert mcf_applicable(["A", "B"], full) == (True, "complete")
    assert mcf_applicable(["A", "B"], full - {("B", "B")})[0] is False      # self pair needed
    assert mcf_applicable(["A", "B"], full - {("A", "B")})[0] is False      # directed
    assert mcf_applicable(["A", "B"], full, {"A": ("20ft", (), "06-20"), "B": ("20ft", (), "06-20")})[0]
    ok, why = mcf_applicable(["A", "B"], full, {"A": ("20ft", (), "06-20"), "B": ("20ft", (), "08-18")})
    assert not ok and "differ" in why
    assert mcf_applicable(["A"], {("A", "A")}, {"A": "x"})[0]


def _hours_instance(seed):
    """Two hauliers in one complete component, different working hours: a job is eligible for
    a haulier only inside its hours, so eligibility differs per class (addendum M5)."""
    rng = random.Random(seed)
    p = tiny_instance(seed, n_jobs=7, n_companies=2, coop_mode="full", start_mode="start",
                      defer=4000.0, eligible_mode="none")
    hours = {"A": (6, 20), "B": (8, 14)}
    hour = {j: rng.randrange(4, 22) for j in p["orders"]}
    p["eligible"] = {(j, c) for j in p["orders"] for c in p["companies"]
                     if hours[c][0] <= hour[j] < hours[c][1]}
    p["class_attrs"] = {c: ("20ft", (), hours[c]) for c in p["companies"]}
    return p


@pytest.mark.parametrize("seed", range(12))
def test_differing_hours_route_to_the_mip(seed):
    p = _hours_instance(seed)
    r_full, _ = mip(p)
    r, s = solve_by_component(p, "auto")
    assert [row["backend"] for row in s["components"]] == ["mip"]
    assert "differ" in s["components"][0]["reason"]
    assert r["objective"] == pytest.approx(r_full["objective"], abs=1e-6)
    with pytest.raises(ValueError):
        solve_min_cost_flow(p)
    # The same instance with shared hours is exact on the flow, and auto takes it.
    q = dict(p, class_attrs={c: ("20ft", (), (6, 20)) for c in p["companies"]},
             eligible={(j, c) for (j, _c) in p["eligible"] for c in p["companies"]})
    rq, sq = solve_by_component(q, "auto")
    assert [row["backend"] for row in sq["components"]] == ["mincostflow"]
    assert rq["objective"] == pytest.approx(mip(q)[0]["objective"], abs=1e-6)


@pytest.mark.parametrize("seed", range(60))
def test_by_component_equals_the_whole_mip(seed):
    p = tiny_instance(2000 + seed, n_jobs=random.Random(seed).randint(3, 10))
    r_full, _ = mip(p)
    for backend in ("auto", "mip"):
        r, s = solve_by_component(p, backend)
        if r_full["status"] != 0:
            assert r["objective"] is None and "routes" not in r
            continue
        assert r["objective"] == pytest.approx(r_full["objective"], abs=1e-6), backend
        check_solution(p, r, r["objective"])
        comps = cooperation_components(p["companies"], p["cooperation"])
        assert len(s["components"]) == len(comps)
        for row in s["components"]:
            if backend == "mip":
                assert row["backend"] == "mip"
            if row["backend"] == "mip" and row["status"] == 0:
                assert "guard" not in row  # the guard is for limit-stopped incumbents only


@pytest.mark.parametrize("seed", range(40))
def test_rounded_costs_stay_within_the_integer_tolerance(seed):
    rng = random.Random(seed)
    p = tiny_instance(3000 + seed, n_jobs=rng.randint(3, 12), complete=True, eligible_mode="none")
    p["edge_cost_input"] = {e: v + rng.random() for e, v in p["edge_cost_input"].items()}
    if "start_edge_cost" in p:
        p["start_edge_cost"] = {e: v + rng.random() for e, v in p["start_edge_cost"].items()}
    r_mip, _ = mip(p)
    r_mcf, _ = solve_min_cost_flow(p)
    if r_mip["status"] != 0:
        assert r_mcf["status"] == 2
        return
    arcs = len(p["orders"]) + len(p.get("start_nodes") or {})
    assert r_mcf["objective_float"] >= r_mip["objective"] - 1e-6        # the MIP is optimal
    assert r_mcf["objective_float"] <= r_mip["objective"] + arcs * 1.0  # 2 x 0.5 per arc
    assert abs(r_mcf["objective"] - r_mcf["objective_float"]) <= arcs * 0.5 + 1e-9
    check_solution(p, r_mcf, r_mcf["objective_float"])


@pytest.mark.parametrize("seed", range(60))
def test_served_bound_bounds_every_mip_plan(seed):
    p = tiny_instance(4000 + seed, n_jobs=random.Random(seed).randint(3, 9), defer=500.0)
    r, _ = mip(p)
    assert r["status"] == 0
    assert _served(r, p) <= served_upper_bound(p)
    assert served_reference(p) <= served_upper_bound(p)


@pytest.mark.parametrize("seed", range(30))
def test_served_bound_is_tight_on_complete_components(seed):
    p = tiny_instance(5000 + seed, n_jobs=random.Random(seed).randint(3, 9), complete=True,
                      defer=10_000_000.0, eligible_mode="none")
    r, _ = mip(p)  # a penalty above every cost: the MIP maximises served first
    assert _served(r, p) == served_upper_bound(p)


def test_quality_guard():
    assert quality_guard(95, 100)["ok"] and quality_guard(95, 100)["note"] is None
    g = quality_guard(94, 100)
    assert not g["ok"] and g["note"] == "poor_incumbent" and g["ratio"] == 0.94
    assert quality_guard(0, 0)["ok"]
    assert not quality_guard(148, 4563)["ok"]        # the N0 SCIP-at-60 s incumbent
    with pytest.raises(ValueError):
        quality_guard(-1, 3)


def test_poor_incumbent_is_reported(monkeypatch):
    p = tiny_instance(7, n_jobs=8, n_companies=2, coop_mode="oneway", start_mode="start",
                      defer=3000.0, eligible_mode="none")

    class _Lazy(nfs.NetworkFlowSolver):  # a time-limited incumbent that planned nothing
        def solve(self, params):
            r, s = super().solve(params)
            r = dict(r, status=1, unplanned=list(params["orders"]),
                     routes={k: 0.0 for k in r["routes"]}, assignment={})
            return r, s

    monkeypatch.setattr(backends, "NetworkFlowSolver", _Lazy)
    r, s = solve_by_component(p, "mip")
    assert r["status"] == 1
    assert served_reference(p) <= served_upper_bound(p)
    if served_reference(p):
        assert s["poor_incumbent"] is True
        assert any(row["guard"]["note"] == "poor_incumbent" for row in s["components"])


def test_component_params_partition_the_instance():
    p = tiny_instance(11, n_jobs=9, n_companies=3, coop_mode="groups", start_mode="mixed")
    comps = cooperation_components(p["companies"], p["cooperation"])
    subs = [component_params(p, c) for c in comps]
    assert sorted(o for s in subs for o in s["orders"]) == sorted(p["orders"])
    assert sum(len(s["edges_input"]) for s in subs) <= len(p["edges_input"])
    assert sorted(k for s in subs for k in s.get("start_nodes", {})) == sorted(p.get("start_nodes", {}))


def test_unknown_backend_refused():
    with pytest.raises(ValueError, match="backend"):
        solve_by_component(tiny_instance(1), "gurobi")


# ---------------------------------------------------------------- limits

@pytest.mark.parametrize("seed", range(40))
def test_arc_count_matches_the_built_model(seed):
    p = tiny_instance(6000 + seed)
    p.setdefault("defer_penalty", 1000.0)  # options on: stats carry the variable split
    _, s = mip(p)
    n_a = sum(1 for o in p["orders"] for c in p["companies"]
              if (p["order_owner"][o], c) in p["cooperation"]
              and (p.get("eligible") is None or (o, c) in p["eligible"]))
    assert mip_arc_count(p) == s["num_variables"] - n_a - len(p["orders"])


def test_arc_budget():
    p = tiny_instance(3, n_jobs=7)
    n = mip_arc_count(p)
    assert check_mip_arc_budget(p, budget=n) == n
    with pytest.raises(ArcBudgetExceeded, match="above the budget"):
        check_mip_arc_budget(p, budget=n - 1)


def test_memory_cap_stops_the_child_not_the_parent():
    code = "b = bytearray(2 * 1024 ** 3); print('allocated')"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          preexec_fn=memory_cap_preexec(1024 ** 3))
    assert proc.returncode != 0 and "MemoryError" in proc.stderr
    ok = subprocess.run([sys.executable, "-c", "b = bytearray(64 * 1024 ** 2); print('ok')"],
                        capture_output=True, text=True, timeout=60,
                        preexec_fn=memory_cap_preexec(1024 ** 3))
    assert ok.returncode == 0 and ok.stdout.strip() == "ok"
    with pytest.raises(ValueError):
        memory_cap_preexec(0)
