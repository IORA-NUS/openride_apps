"""OpenRide options of the vendored NetworkFlowSolver, checked against brute force, and the
min-cost-flow backend checked against the MIP (plan N2 tests).

- ``start_nodes`` / ``defer_penalty`` / ``eligible``: the MIP optimum equals an exhaustive
  enumeration of every set of disjoint truck chains on tiny DAG instances (<= 7 jobs,
  3 trucks), and the MIP's own solution, read back through ``chains.extract_chains``, is
  feasible and costs exactly its objective.
- ``accept_feasible`` / ``time_limit_s`` / ``mip_gap``: plumbing and the FEASIBLE path.
- ``min_cost_flow``: same optimum as the MIP on >= 50 random complete-adjacency instances
  (start nodes, depot fleets, deferral, uniform eligibility, cyclic edge sets included).

Skipped where OR-Tools is absent.

Verified to have teeth (mutate / see it fail / restore): halving the defer penalty in the
MIP objective failed 122 cases; a start-node capacity of 2 failed 50; dropping the
``eligible`` filter on ``a`` failed 49; halving the min-cost-flow skip-arc cost failed 49
(the MCF==MIP cases among them).
"""

from __future__ import annotations

import math
import random
import sys
import types
from functools import lru_cache

import pytest

pytest.importorskip("ortools")

from ortools.linear_solver import pywraplp  # noqa: E402

from apps.container_logistics.policies.solver_lib.coop_dispatch import (  # noqa: E402
    base_solver,
    network_flow_solver as nfs,
)
from apps.container_logistics.policies.solver_lib.coop_dispatch.chains import extract_chains  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch.min_cost_flow import (  # noqa: E402
    cooperation_components,
    is_complete,
    solve_min_cost_flow,
)

INF = math.inf


# ---------------------------------------------------------------- instances

def tiny_instance(seed, *, n_jobs=None, n_companies=None, start_mode=None, defer=None,
                  eligible_mode=None, coop_mode=None, complete=False, cyclic=False):
    rng = random.Random(seed)
    n = n_jobs if n_jobs is not None else rng.randint(2, 7)
    nc = n_companies if n_companies is not None else rng.randint(1, 3)
    companies = ["A", "B", "C"][:nc]
    jobs = [f"j{k}" for k in range(n)]
    owner = {j: rng.choice(companies) for j in jobs}

    coop_mode = coop_mode or rng.choice(["self", "full", "oneway", "groups"])
    if complete:
        coop_mode = rng.choice(["self", "full", "groups"])
    if coop_mode == "self":
        coop = {(c, c) for c in companies}
    elif coop_mode == "full":
        coop = {(a, b) for a in companies for b in companies}
    elif coop_mode == "oneway":  # A may serve B's orders but not the reverse: not complete
        coop = {(c, c) for c in companies} | {(b, "A") for b in companies}
    else:  # complete groups {A, B} + {C}
        g = {"A": 0, "B": 0, "C": 1}
        coop = {(a, b) for a in companies for b in companies if g[a] == g[b]}

    edges, cost = [], {}
    for a in range(n):
        for b in range(n):
            if a == b or (b < a and not cyclic):
                continue
            if rng.random() < (0.5 if not cyclic else 0.3):
                e = (jobs[a], jobs[b])
                edges.append(e)
                cost[e] = float(rng.randrange(100, 5000))

    start_mode = start_mode or rng.choice(["start", "depot", "mixed"])
    start_nodes, start_edges, start_cost = {}, [], {}
    vl = {}
    if start_mode == "depot":
        vl = {c: rng.randint(0, 2) for c in companies}
    else:
        sn_cos = companies if start_mode == "start" else companies[: max(1, nc - 1)]
        for k in range(3):
            start_nodes[f"t{k}"] = {"company": rng.choice(sn_cos)}
        for c in companies:
            vl[c] = 0 if c in {s["company"] for s in start_nodes.values()} else rng.randint(0, 2)
        for t in start_nodes:
            for j in jobs:
                if rng.random() < 0.6:
                    start_edges.append((t, j))
                    start_cost[(t, j)] = float(rng.randrange(0, 6000))

    p = {
        "instance_id": f"tiny{seed}",
        "companies": companies,
        "orders": jobs,
        "order_owner": owner,
        "cooperation": coop,
        "vehicle_limit": vl,
        "edges_input": edges,
        "edge_cost_input": cost,
        "edge_trailer_cost_input": {e: 0.0 for e in edges},
        "raw_cost": 0.0,
        "use_trailer_cost": False,
    }
    if start_nodes:
        p.update(start_nodes=start_nodes, start_edges=start_edges, start_edge_cost=start_cost)
    if defer is None:
        defer = rng.choice([None, 800.0, 3000.0, 20000.0])
    elif defer is False:
        defer = None
    if defer is not None:
        p["defer_penalty"] = defer
    eligible_mode = eligible_mode or rng.choice(["none", "none", "random", "uniform"])
    if eligible_mode == "random":
        p["eligible"] = {(j, c) for j in jobs for c in companies if rng.random() < 0.75}
    elif eligible_mode == "uniform":
        p["eligible"] = {(j, c) for j in jobs if rng.random() < 0.8 for c in companies}
    return p


# ---------------------------------------------------------------- brute force

def brute_force(p):
    """Minimum over every set of disjoint chains (one per start node, vl[c] per depot company)."""
    jobs = p["orders"]
    owner, coop = p["order_owner"], p["cooperation"]
    eligible = p.get("eligible")
    P = p.get("defer_penalty")
    start_nodes = p.get("start_nodes") or {}
    sn_cos = {s["company"] for s in start_nodes.values()}
    succ = {}
    for (i, j) in p["edges_input"]:
        succ.setdefault(i, []).append(j)
    scost = p.get("start_edge_cost", {})

    def serves(j, c):
        return (owner[j], c) in coop and (eligible is None or (j, c) in eligible)

    slots = [("sid", t, s["company"]) for t, s in start_nodes.items()]
    for c in p["companies"]:
        if c not in sn_cos:
            slots += [("depot", None, c)] * int(p["vehicle_limit"].get(c, 0))

    def chains_from(first, c, used):
        out = []

        def walk(chain, cost):
            out.append((tuple(chain), cost))
            for k in succ.get(chain[-1], []):
                if k not in used and k not in chain and serves(k, c):
                    walk(chain + [k], cost + p["edge_cost_input"][(chain[-1], k)])

        walk([first], 0.0)
        return out

    @lru_cache(maxsize=None)
    def best(slot, used):
        if slot == len(slots):
            unserved = len(jobs) - len(used)
            if unserved and P is None:
                return INF
            return (P or 0.0) * unserved
        kind, t, c = slots[slot]
        val = best(slot + 1, used)  # the truck stays unused
        for j in jobs:
            if j in used or not serves(j, c):
                continue
            if kind == "sid":
                if (t, j) not in scost:
                    continue
                c0 = scost[(t, j)]
            else:
                c0 = 0.0
            for chain, cost in chains_from(j, c, used):
                val = min(val, c0 + cost + best(slot + 1, used | frozenset(chain)))
        return val

    return best(0, frozenset())


def check_solution(p, result, objective):
    """The returned chains are feasible and cost exactly ``objective``."""
    start_nodes = p.get("start_nodes") or {}
    ch = extract_chains(result, start_nodes)
    assert ch.orphaned == []
    owner, coop, eligible = p["order_owner"], p["cooperation"], p.get("eligible")
    edges = set(p["edges_input"])
    seen, total = [], 0.0
    depot_used = {}

    def ok(j, c):
        return (owner[j], c) in coop and (eligible is None or (j, c) in eligible)

    for sid, chain in ch.by_start.items():
        c = start_nodes[sid]["company"]
        if chain:
            total += p["start_edge_cost"][(sid, chain[0])]
        for a, b in zip(chain, chain[1:]):
            assert (a, b) in edges
            total += p["edge_cost_input"][(a, b)]
        assert all(ok(j, c) for j in chain)
        seen += chain
    for c, chains in ch.depot.items():
        assert c not in {s["company"] for s in start_nodes.values()}
        depot_used[c] = len(chains)
        for chain in chains:
            for a, b in zip(chain, chain[1:]):
                assert (a, b) in edges
                total += p["edge_cost_input"][(a, b)]
            assert all(ok(j, c) for j in chain)
            seen += chain
    for c, k in depot_used.items():
        assert k <= p["vehicle_limit"][c]
    assert len(seen) == len(set(seen))
    unplanned = result.get("unplanned", [])
    assert set(seen) | set(unplanned) == set(p["orders"]) and not set(seen) & set(unplanned)
    total += (p.get("defer_penalty") or 0.0) * len(unplanned)
    assert total == pytest.approx(objective, abs=1e-6)


def mip(p):
    return nfs.NetworkFlowSolver().solve(p)


# ---------------------------------------------------------------- MIP options vs brute force

@pytest.mark.parametrize("seed", range(120))
def test_mip_options_match_brute_force(seed):
    p = tiny_instance(seed)
    result, stats = mip(p)
    ref = brute_force(p)
    if ref == INF:
        assert result["status"] == pywraplp.Solver.INFEASIBLE, (result, stats)
        assert stats.get("status_name", "INFEASIBLE") == "INFEASIBLE"
        return
    assert result["status"] == pywraplp.Solver.OPTIMAL, (result, stats)
    assert stats.get("status_name", "OPTIMAL") == "OPTIMAL"
    # stats gain keys only when an option is given (options-off output is upstream's)
    has_opt = any(k in p for k in ("start_nodes", "defer_penalty", "eligible"))
    assert ("status_name" in stats) == has_opt
    assert result["objective"] == pytest.approx(ref, abs=1e-6)
    check_solution(p, result, result["objective"])
    if "defer_penalty" in p:
        assert stats["num_unplanned"] == len(result["unplanned"])


def test_brute_force_covers_every_option():
    kinds = set()
    for seed in range(120):
        p = tiny_instance(seed)
        kinds.add(("start" if "start_nodes" in p else "depot",
                   "defer" if "defer_penalty" in p else "strict",
                   "eligible" if "eligible" in p else "all"))
        if brute_force(p) == INF:
            kinds.add("infeasible")
    assert "infeasible" in kinds
    assert {(a, b, c) for a in ("start", "depot") for b in ("defer", "strict")
            for c in ("eligible", "all")} <= kinds


def test_defer_penalty_is_taken_only_when_it_pays():
    p = tiny_instance(3, n_jobs=1, n_companies=1, start_mode="start", defer=1000.0, eligible_mode="none")
    p["start_edges"] = [("t0", "j0")]
    p["start_edge_cost"] = {("t0", "j0"): 999.0}
    r, _ = mip(p)
    assert r["unplanned"] == [] and r["objective"] == 999.0
    p["start_edge_cost"] = {("t0", "j0"): 1001.0}
    r, _ = mip(p)
    assert r["unplanned"] == ["j0"] and r["objective"] == 1000.0


def test_start_node_company_has_no_depot_arcs_and_no_vehicle_limit():
    p = tiny_instance(5, n_jobs=3, n_companies=1, start_mode="start", defer=False, eligible_mode="none")
    p["vehicle_limit"] = {"A": 0}  # would make the depot model infeasible
    p["start_nodes"] = {"t0": {"company": "A"}, "t1": {"company": "A"}, "t2": {"company": "A"}}
    p["start_edges"] = [(t, j) for t in p["start_nodes"] for j in p["orders"]]
    p["start_edge_cost"] = {e: 10.0 for e in p["start_edges"]}
    r, s = mip(p)
    assert r["status"] == 0
    assert not any(k[0] == "start" and v > 0.5 for k, v in r["routes"].items())
    assert not any(k[0] == "start" for k in r["routes"])
    assert s["num_start_nodes"] == 3 and s["num_start_arcs"] == 9


@pytest.mark.parametrize("bad, msg", [
    ({"start_nodes": {"j0": {"company": "A"}}}, "collides"),
    ({"start_nodes": {"t0": {"company": "Z"}}}, "not in companies"),
    ({"start_edges": [("tX", "j0")]}, "unknown start node"),
    ({"start_edges": [("t0", "nope")]}, "unknown order"),
    ({"start_edges": [("t0", "j0"), ("t0", "j0")]}, "duplicate"),
])
def test_start_node_validation(bad, msg):
    p = tiny_instance(1, n_jobs=2, n_companies=1, start_mode="start", defer=False, eligible_mode="none")
    p["start_nodes"] = {"t0": {"company": "A"}}
    p["start_edges"] = [("t0", "j0")]
    p["start_edge_cost"] = {("t0", "j0"): 1.0}
    p.update(bad)
    with pytest.raises(ValueError, match=msg):
        mip(p)


def test_start_edges_without_start_nodes_refused():
    p = tiny_instance(1, start_mode="depot")
    p["start_edges"] = [("t0", "j0")]
    with pytest.raises(ValueError, match="without start_nodes"):
        mip(p)


# ---------------------------------------------------------------- limits

def test_accept_feasible_extracts_the_incumbent(monkeypatch):
    p = tiny_instance(11, n_jobs=6, start_mode="start", defer=5000.0, eligible_mode="none")
    real = base_solver.solve_model

    def feasible_only(solver):  # what a solve that hit its time limit reports
        real(solver)
        return pywraplp.Solver.FEASIBLE

    monkeypatch.setattr(nfs, "solve_model", feasible_only)
    r, s = mip(dict(p, accept_feasible=True))
    assert r["status"] == pywraplp.Solver.FEASIBLE and s["status_name"] == "FEASIBLE"
    assert r["objective"] is not None and "routes" in r
    check_solution(p, r, r["objective"])
    r2, s2 = mip(p)
    assert r2 == {"status": pywraplp.Solver.FEASIBLE, "message": "No optimal solution"}
    assert s2["objective"] is None


def test_tiny_time_limit_never_returns_a_wrong_plan():
    """A 1 ms limit on a larger instance: whatever SCIP returns, a usable status carries a
    feasible plan whose cost is its objective, and an unusable one carries none."""
    p = tiny_instance(2, n_jobs=7, start_mode="start", defer=5000.0, eligible_mode="none")
    rng = random.Random(0)
    # widen it: 40 jobs on a chain-friendly DAG
    jobs = [f"j{k}" for k in range(40)]
    p["orders"] = jobs
    p["order_owner"] = {j: rng.choice(p["companies"]) for j in jobs}
    p["edges_input"] = [(jobs[a], jobs[b]) for a in range(40) for b in range(a + 1, min(40, a + 8))]
    p["edge_cost_input"] = {e: float(rng.randrange(100, 5000)) for e in p["edges_input"]}
    p["edge_trailer_cost_input"] = {e: 0.0 for e in p["edges_input"]}
    p["start_edges"] = [(t, j) for t in p["start_nodes"] for j in jobs[:10]]
    p["start_edge_cost"] = {e: float(rng.randrange(0, 6000)) for e in p["start_edges"]}
    r, s = mip(dict(p, time_limit_s=0.001, accept_feasible=True))
    assert s["time_limit_s"] == 0.001
    if r["status"] in (0, 1):
        check_solution(p, r, r["objective"])
    else:
        assert "routes" not in r


def test_time_limit_and_gap_reach_the_backend(monkeypatch):
    limits = []
    real_set = pywraplp.Solver.SetTimeLimit
    monkeypatch.setattr(pywraplp.Solver, "SetTimeLimit", lambda self, ms: (limits.append(ms), real_set(self, ms))[1])
    solves = []
    real_solve = pywraplp.Solver.Solve
    monkeypatch.setattr(pywraplp.Solver, "Solve", lambda self, *a: (solves.append(a), real_solve(self, *a))[1])
    p = tiny_instance(4, start_mode="start", defer=3000.0)
    mip(p)
    mip(dict(p, time_limit_s=7.5, mip_gap=0.02))
    assert limits == [5400 * 1000, 7500]
    assert solves[0] == () and len(solves[1]) == 1
    mp = solves[1][0]
    assert mp.GetDoubleParam(pywraplp.MPSolverParameters.RELATIVE_MIP_GAP) == pytest.approx(0.02)


def test_gurobi_adapter_takes_the_limits(monkeypatch):
    class _Params:
        pass

    class _Model:
        def __init__(self):
            self.Params = _Params()

    fake = types.SimpleNamespace(Model=_Model, GRB=None, quicksum=sum)
    monkeypatch.setitem(sys.modules, "gurobipy", fake)
    s = base_solver.create_solver_with_fallback(time_limit_s=12, mip_gap=0.01)
    assert isinstance(s, base_solver.GurobiSolverAdapter)
    assert s._model.Params.TimeLimit == 12.0 and s._model.Params.MIPGap == 0.01
    plain = base_solver.create_solver_with_fallback()
    assert not hasattr(plain._model.Params, "TimeLimit") and not hasattr(plain._model.Params, "MIPGap")


# ---------------------------------------------------------------- min cost flow vs MIP

MCF_SEEDS = range(1000, 1080)


@pytest.mark.parametrize("seed", MCF_SEEDS)
def test_min_cost_flow_equals_mip(seed):
    rng = random.Random(seed)
    p = tiny_instance(seed, n_jobs=rng.randint(3, 12), complete=True, cyclic=seed % 5 == 0,
                      eligible_mode=rng.choice(["none", "uniform"]))
    for comp in cooperation_components(p["companies"], p["cooperation"]):
        assert is_complete(comp, p["cooperation"])
    r_mip, s_mip = mip(p)
    r_mcf, s_mcf = solve_min_cost_flow(p)
    if r_mip["status"] != 0:
        assert r_mip["status"] == pywraplp.Solver.INFEASIBLE
        assert r_mcf["status"] == 2, (r_mcf, s_mcf)
        return
    assert r_mcf["status"] == 0, (r_mcf, s_mcf)
    assert r_mcf["objective"] == pytest.approx(r_mip["objective"], abs=1e-6), (seed, r_mip["objective"], r_mcf["objective"])
    if seed % 5:  # DAG: the flow's chains are a feasible plan of that cost
        check_solution(p, r_mcf, r_mcf["objective"])
        assert s_mcf["num_orphaned"] == 0


def test_min_cost_flow_sample_is_varied():
    """The equivalence sample above holds >= 50 solved instances and exercises every mode."""
    solved, modes = 0, set()
    for seed in MCF_SEEDS:
        rng = random.Random(seed)
        p = tiny_instance(seed, n_jobs=rng.randint(3, 12), complete=True, cyclic=seed % 5 == 0,
                          eligible_mode=rng.choice(["none", "uniform"]))
        r, _ = mip(p)
        solved += r["status"] == 0
        modes.add(("start_nodes" in p, "defer_penalty" in p, "eligible" in p,
                   len(cooperation_components(p["companies"], p["cooperation"])) > 1))
    assert solved >= 50
    assert len(modes) >= 10


def test_min_cost_flow_refuses_what_it_cannot_solve_exactly():
    p = tiny_instance(7, n_companies=2, coop_mode="oneway", start_mode="depot")
    with pytest.raises(ValueError, match="not complete"):
        solve_min_cost_flow(p)
    p = tiny_instance(7, n_companies=2, coop_mode="full", start_mode="depot", eligible_mode="none")
    with pytest.raises(ValueError, match="use_trailer_cost"):
        solve_min_cost_flow(dict(p, use_trailer_cost=True))
    e = p["edges_input"][0]
    with pytest.raises(ValueError, match="integer"):
        solve_min_cost_flow(dict(p, edge_cost_input=dict(p["edge_cost_input"], **{}) | {e: 10.5}))
    with pytest.raises(ValueError, match="distinct"):
        solve_min_cost_flow(dict(p, edges_input=p["edges_input"] + [e]))
    with pytest.raises(ValueError, match="not uniform"):
        solve_min_cost_flow(dict(p, eligible={(p["orders"][0], "A")}))


def test_min_cost_flow_is_deterministic():
    p = tiny_instance(1010, n_jobs=12, complete=True)
    assert solve_min_cost_flow(p)[0] == solve_min_cost_flow(p)[0]
