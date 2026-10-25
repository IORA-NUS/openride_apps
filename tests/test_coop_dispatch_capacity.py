"""Start-node ``capacity`` and the compact array arc form (follow-up E2).

- A start node of capacity c equals c identical unit start nodes (same class, same start
  arcs and costs): brute force on the unit expansion == MIP on the merged node == MIP on
  the expansion, and == the min-cost flow where it applies.
- ``capacity`` absent and ``capacity: 1`` build the identical MIP model (LP sha256) and the
  identical output, for the MIP and the flow.
- ``time_window_edges.build_time_window_arrays`` holds exactly the dict API's arcs, in its
  order; the flow solved from index arrays returns exactly what it returns from lists;
  ``backends.solve_by_component`` slices array params like list params.

Skipped where OR-Tools is absent.

Verified to have teeth (mutate / see it fail / restore): the MIP start-node constraint
back to ``<= 1`` failed 5 cases; the flow's ``S -> sid`` capacity back to 1 failed 38. The
array/dict identity shares its core with the dict API, so a wrong window index (off by one)
passed here and failed 98 cases of ``test_time_window_edges.py`` (the O(n^2) reference).
"""

from __future__ import annotations

import hashlib
import random

import numpy as np
import pytest

pytest.importorskip("ortools")

from apps.container_logistics.policies.solver_lib.coop_dispatch import base_solver  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch import network_flow_solver as nfs  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch.backends import solve_by_component  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch.chains import extract_chains  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch.limits import (  # noqa: E402
    arrays_to_lists,
    mip_arc_count,
)
from apps.container_logistics.policies.solver_lib.coop_dispatch.min_cost_flow import (  # noqa: E402
    cooperation_components,
    mcf_applicable,
    solve_min_cost_flow,
)
from apps.container_logistics.policies.solver_lib.coop_dispatch.time_window_edges import (  # noqa: E402
    build_time_window_arrays,
    build_time_window_edges,
)
from tests.test_coop_dispatch_options import brute_force, mip, tiny_instance  # noqa: E402
from tests.test_time_window_edges import KW, TRAVEL, _rand  # noqa: E402


def _with_twins(seed, *, complete=False, n_jobs=None):
    """A start-node instance where t0 has c-1 identical twins; returns (expanded, merged, c)."""
    rng = random.Random(seed)
    p = tiny_instance(seed, n_jobs=n_jobs or rng.randint(3, 6), start_mode="start", complete=complete,
                      eligible_mode="none" if complete else None)
    c = rng.randint(2, 3)
    t0 = "t0"
    company = p["start_nodes"][t0]["company"]
    arcs0 = [(s, j) for (s, j) in p["start_edges"] if s == t0]
    exp = dict(p)
    exp["start_nodes"] = dict(p["start_nodes"])
    exp["start_edges"] = list(p["start_edges"])
    exp["start_edge_cost"] = dict(p["start_edge_cost"])
    for k in range(1, c):
        tw = f"tw{k}"
        exp["start_nodes"][tw] = {"company": company}
        for (_, j) in arcs0:
            exp["start_edges"].append((tw, j))
            exp["start_edge_cost"][(tw, j)] = p["start_edge_cost"][(t0, j)]
    merged = dict(p)
    merged["start_nodes"] = dict(p["start_nodes"])
    merged["start_nodes"][t0] = {"company": company, "capacity": c}
    return exp, merged, c


@pytest.mark.parametrize("seed", range(60))
def test_capacity_equals_identical_unit_starts(seed):
    exp, merged, c = _with_twins(seed)
    ref = brute_force(exp)
    r_exp, _ = mip(exp)
    r_m, s_m = mip(merged)
    if ref == float("inf"):
        assert r_exp["status"] == 2 and r_m["status"] == 2
        return
    assert r_exp["objective"] == pytest.approx(ref, abs=1e-6)
    assert r_m["objective"] == pytest.approx(ref, abs=1e-6)
    ch = extract_chains(r_m, merged["start_nodes"], job_order=merged["orders"])
    assert len(ch.chains_by_start["t0"]) <= c
    assert "t0" not in ch.by_start  # capacity > 1: only in chains_by_start
    served = [j for chains in ch.chains_by_start.values() for chain in chains for j in chain]
    served += [j for chains in ch.depot.values() for chain in chains for j in chain]
    assert len(served) == len(set(served))
    assert sorted(served + ch.unplanned) == sorted(merged["orders"])
    firsts = [chain[0] for chain in ch.chains_by_start["t0"]]
    assert firsts == sorted(firsts, key=merged["orders"].index)


@pytest.mark.parametrize("seed", range(40))
def test_capacity_on_the_flow_equals_the_mip(seed):
    exp, merged, c = _with_twins(100 + seed, complete=True, n_jobs=random.Random(seed).randint(3, 8))
    comps = cooperation_components(merged["companies"], merged["cooperation"])
    assert all(mcf_applicable(cc, merged["cooperation"])[0] for cc in comps)
    r_mip, _ = mip(exp)
    r_mcf, _ = solve_min_cost_flow(merged)
    if r_mip["status"] != 0:
        assert r_mcf["status"] == 2
        return
    assert r_mcf["objective"] == pytest.approx(r_mip["objective"], abs=1e-6)
    ch = extract_chains(r_mcf, merged["start_nodes"], job_order=merged["orders"])
    assert ch.orphaned == [] and len(ch.chains_by_start["t0"]) <= c


def _lp(params):
    seen = []
    real = base_solver.create_solver_with_fallback
    nfs.create_solver_with_fallback = lambda *a, **k: seen.append(real(*a, **k)) or seen[-1]
    try:
        out = nfs.NetworkFlowSolver().solve(params)
    finally:
        nfs.create_solver_with_fallback = real
    return out, hashlib.sha256(seen[-1].ExportModelAsLpFormat(False).encode()).hexdigest()


def _timeless(stats):
    return {k: v for k, v in stats.items() if k not in ("solve_time_s", "wall_time_ms")}


@pytest.mark.parametrize("seed", range(20))
def test_capacity_one_is_the_absent_capacity(seed):
    p = tiny_instance(seed, start_mode="start")
    q = dict(p, start_nodes={s: dict(spec, capacity=1) for s, spec in p["start_nodes"].items()})
    (r1, s1), lp1 = _lp(p)
    (r2, s2), lp2 = _lp(q)
    assert lp1 == lp2 and r1 == r2 and _timeless(s1) == _timeless(s2)
    if all(mcf_applicable(cc, p["cooperation"])[0] for cc in cooperation_components(p["companies"], p["cooperation"])):
        try:
            m1 = solve_min_cost_flow(p)[0]
        except ValueError:
            return  # non-uniform eligible: not the flow's
        assert m1 == solve_min_cost_flow(q)[0]
        e1, e2 = extract_chains(m1, p["start_nodes"]), extract_chains(m1, q["start_nodes"])
        assert e1.by_start == e2.by_start


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "2", None])
def test_bad_capacity_refused(bad):
    p = tiny_instance(3, n_jobs=3, n_companies=1, start_mode="start", defer=1000.0, eligible_mode="none")
    p["start_nodes"] = dict(p["start_nodes"], t0={"company": "A", "capacity": bad})
    with pytest.raises(ValueError, match="capacity"):
        mip(p)
    with pytest.raises(ValueError, match="capacity"):
        solve_min_cost_flow(p)
    with pytest.raises(ValueError, match="capacity"):
        extract_chains({"routes": {}}, p["start_nodes"])


def test_chains_over_capacity_refused_and_order_is_deterministic():
    sn = {"s": {"company": "A", "capacity": 2}}
    routes = {("s", "b", "A"): 1.0, ("b", "end", "A"): 1.0, ("s", "a", "A"): 1.0, ("a", "c", "A"): 1.0,
              ("c", "end", "A"): 1.0}
    ch = extract_chains({"routes": routes}, sn)
    assert ch.chains_by_start["s"] == [["a", "c"], ["b"]]          # by str(first job)
    ch = extract_chains({"routes": routes}, sn, job_order=["b", "a", "c"])
    assert ch.chains_by_start["s"] == [["b"], ["a", "c"]]          # by job_order
    with pytest.raises(ValueError, match="capacity"):
        extract_chains({"routes": routes | {("s", "d", "A"): 1.0, ("d", "end", "A"): 1.0}}, sn)


# ---------------------------------------------------------------- arrays

@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("kw", KW, ids=[f"kw{k}" for k in range(len(KW))])
@pytest.mark.parametrize("k", [None, 1, 3])
def test_arrays_hold_exactly_the_dict_arcs(seed, kw, k):
    jobs, starts = _rand(seed, ties=seed % 2 == 1)
    d = build_time_window_edges(jobs, starts, TRAVEL, k_succ=k, k_start=k, **kw)
    a = build_time_window_arrays(jobs, starts, TRAVEL, k_succ=k, k_start=k, **kw)
    ids, sids = [j.id for j in jobs], [s.id for s in starts]
    assert [(ids[x], ids[y]) for x, y in zip(a.src.tolist(), a.dst.tolist())] == d.edges
    assert [(sids[x], ids[y]) for x, y in zip(a.ssrc.tolist(), a.sdst.tolist())] == d.start_edges
    assert a.cost.tolist() == [d.edge_cost[e] for e in d.edges]
    assert a.scost.tolist() == [d.start_edge_cost[e] for e in d.start_edges]
    assert a.duration.tolist() == [d.edge_duration[e] for e in d.edges]
    assert a.src.dtype == np.int64 and a.ssrc.dtype == np.int64


def _to_arrays(p):
    """List-form params -> array-form params (same arcs, same order)."""
    oi = {o: k for k, o in enumerate(p["orders"])}
    si = {s: k for k, s in enumerate(p.get("start_nodes") or {})}
    q = {k: v for k, v in p.items() if k not in ("edges_input", "edge_cost_input", "edge_trailer_cost_input",
                                                 "start_edges", "start_edge_cost")}
    q["edges_array"] = {"src": np.array([oi[i] for i, _ in p["edges_input"]], dtype=np.int64),
                        "dst": np.array([oi[j] for _, j in p["edges_input"]], dtype=np.int64),
                        "cost": np.array([p["edge_cost_input"][e] for e in p["edges_input"]], dtype=float)}
    if p.get("start_nodes"):
        se = p.get("start_edges", [])
        q["start_edges_array"] = {"src": np.array([si[s] for s, _ in se], dtype=np.int64),
                                  "dst": np.array([oi[j] for _, j in se], dtype=np.int64),
                                  "cost": np.array([p["start_edge_cost"][e] for e in se], dtype=float)}
    return q


@pytest.mark.parametrize("seed", range(40))
def test_flow_from_arrays_equals_flow_from_lists(seed):
    rng = random.Random(seed)
    if seed % 2:
        _, p, _ = _with_twins(500 + seed, complete=True, n_jobs=rng.randint(3, 10))
    else:
        p = tiny_instance(700 + seed, n_jobs=rng.randint(3, 10), complete=True, eligible_mode="none")
    q = _to_arrays(p)
    r1, s1 = solve_min_cost_flow(p)
    r2, s2 = solve_min_cost_flow(q)
    assert r1 == r2
    assert {k: v for k, v in s1.items() if k != "solve_time_s"} == {k: v for k, v in s2.items() if k != "solve_time_s"}
    back = arrays_to_lists(q)
    assert back["edges_input"] == p["edges_input"]
    assert back.get("start_edges", []) == p.get("start_edges", [])
    assert mip_arc_count(q) == mip_arc_count(p)


@pytest.mark.parametrize("seed", range(30))
def test_by_component_from_arrays_equals_from_lists(seed):
    p = tiny_instance(900 + seed, n_jobs=random.Random(seed).randint(3, 9), start_mode="start")
    q = _to_arrays(p)
    r1, s1 = solve_by_component(p, "auto")
    r2, s2 = solve_by_component(q, "auto")
    assert r1 == r2
    assert [(c["backend"], c["objective"]) for c in s1["components"]] == \
        [(c["backend"], c["objective"]) for c in s2["components"]]


def test_both_forms_at_once_refused():
    p = tiny_instance(5, start_mode="start", complete=True, eligible_mode="none")
    q = _to_arrays(p)
    with pytest.raises(ValueError, match="not both"):
        solve_min_cost_flow(dict(q, edges_input=p["edges_input"] or [("j0", "j1")]))
    bad = dict(q, edges_array={"src": np.array([0]), "dst": np.array([99]), "cost": np.array([1.0])})
    with pytest.raises(ValueError, match="out of range"):
        solve_min_cost_flow(bad)
