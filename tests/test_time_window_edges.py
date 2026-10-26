"""``coop_dispatch.time_window_edges`` equals an O(n^2) reference at every K, is monotone in K,
honours its window boundaries exactly, and always yields a DAG (plan N2 tests).

Skipped where OR-Tools is absent (the package imports it at module top).
"""

from __future__ import annotations

import random

import numpy as np
import pytest

pytest.importorskip("ortools")

from apps.container_logistics.policies.solver_lib.coop_dispatch import network_flow_solver as nfs  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch.chains import extract_chains  # noqa: E402
from apps.container_logistics.policies.solver_lib.coop_dispatch.time_window_edges import (  # noqa: E402
    Job,
    Start,
    build_time_window_edges,
    haversine_travel,
)

TRAVEL = haversine_travel(speed_kmh=36.0)  # 10 m/s: seconds = meters / 10


def _rand(seed, n=40, n_starts=6, ties=False):
    rng = random.Random(seed)
    jobs = []
    for k in range(n):
        r = float(rng.randrange(0, 86400, 600 if ties else 1))
        d = float(rng.randrange(1200, 10800, 600 if ties else 1))
        jobs.append(Job(f"j{k}", r, r + d, (1.25 + rng.random() * 0.2, 103.6 + rng.random() * 0.4),
                        (1.25 + rng.random() * 0.2, 103.6 + rng.random() * 0.4)))
    starts = [Start(f"t{k}", float(rng.randrange(0, 40000)),
                    (1.25 + rng.random() * 0.2, 103.6 + rng.random() * 0.4)) for k in range(n_starts)]
    return jobs, starts


def _scalar(a, b):
    m, s = TRAVEL(np.array([a[0]]), np.array([a[1]]), np.array([b[0]]), np.array([b[1]]))
    return float(m[0]), float(s[0])


def _topk(cands, k):
    """cands: {group: [(cost, tie, key)]} -> keys of the k smallest per group."""
    keep = set()
    for lst in cands.values():
        lst.sort()
        keep |= {key for _, _, key in (lst if k is None else lst[:k])}
    return keep


def reference(jobs, starts, *, wait_limit_s, late_slack_s, k_succ=None, k_start=None,
              max_reposition_s=None, start_late_slack_s=None, start_wait_limit_s=None):
    succ, pred, cost = {}, {}, {}
    for i, a in enumerate(jobs):
        for j, b in enumerate(jobs):
            if i == j:
                continue
            if not (a.end_s - late_slack_s <= b.release_s <= a.end_s + wait_limit_s):
                continue
            if not ((b.end_s, j) > (a.end_s, i)):
                continue
            m, _ = _scalar(a.dropoff, b.pickup)
            key = (a.id, b.id)
            cost[key] = m
            succ.setdefault(i, []).append((m, j, key))
            pred.setdefault(j, []).append((m, i, key))
    edges = _topk(succ, k_succ) | _topk(pred, k_succ)
    s_by, j_by, scost = {}, {}, {}
    for si, st in enumerate(starts):
        for j, b in enumerate(jobs):
            if start_late_slack_s is not None and b.release_s < st.ready_s - start_late_slack_s:
                continue
            if start_wait_limit_s is not None and b.release_s - st.ready_s > start_wait_limit_s:
                continue
            m, s = _scalar(st.location, b.pickup)
            if max_reposition_s is not None and s > max_reposition_s:
                continue
            key = (st.id, b.id)
            scost[key] = m
            s_by.setdefault(si, []).append((m, j, key))
            j_by.setdefault(j, []).append((m, si, key))
    sedges = _topk(s_by, k_start) | _topk(j_by, k_start)
    return edges, cost, sedges, scost


KW = [
    dict(wait_limit_s=7200, late_slack_s=0),
    dict(wait_limit_s=3600, late_slack_s=1800),
    dict(wait_limit_s=14400, late_slack_s=600, max_reposition_s=1500),
    dict(wait_limit_s=7200, late_slack_s=900, start_late_slack_s=3600, start_wait_limit_s=20000),
]


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("kw", KW, ids=[f"kw{k}" for k in range(len(KW))])
@pytest.mark.parametrize("k", [None, 1, 2, 5])
def test_equals_the_quadratic_reference(seed, kw, k):
    jobs, starts = _rand(seed, ties=seed % 2 == 1)
    got = build_time_window_edges(jobs, starts, TRAVEL, k_succ=k, k_start=k, **kw)
    edges, cost, sedges, scost = reference(jobs, starts, k_succ=k, k_start=k, **kw)
    assert set(got.edges) == edges and len(got.edges) == len(edges)
    assert set(got.start_edges) == sedges and len(got.start_edges) == len(sedges)
    for e in got.edges:
        assert got.edge_cost[e] == pytest.approx(cost[e], rel=1e-12)
        assert got.edge_duration[e] == pytest.approx(cost[e] / 10.0, rel=1e-9)
    for e in got.start_edges:
        assert got.start_edge_cost[e] == pytest.approx(scost[e], rel=1e-12)
    # deterministic, sorted by (source, target) input index
    idx = {j.id: n for n, j in enumerate(jobs)}
    sidx = {s.id: n for n, s in enumerate(starts)}
    assert got.edges == sorted(got.edges, key=lambda e: (idx[e[0]], idx[e[1]]))
    assert got.start_edges == sorted(got.start_edges, key=lambda e: (sidx[e[0]], idx[e[1]]))
    again = build_time_window_edges(jobs, starts, TRAVEL, k_succ=k, k_start=k, **kw)
    assert again.edges == got.edges and again.start_edges == got.start_edges


@pytest.mark.parametrize("seed", range(4))
def test_k_cap_is_monotone(seed):
    jobs, starts = _rand(seed, n=60)
    prev_e, prev_s = None, None
    for k in [1, 2, 3, 5, 8, 13, None]:
        got = build_time_window_edges(jobs, starts, TRAVEL, wait_limit_s=7200, late_slack_s=900,
                                      k_succ=k, k_start=k)
        if prev_e is not None:
            assert prev_e <= set(got.edges) and prev_s <= set(got.start_edges)
        prev_e, prev_s = set(got.edges), set(got.start_edges)


def test_every_job_keeps_its_cheapest_predecessors():
    jobs, starts = _rand(9, n=80)
    k = 2
    full = build_time_window_edges(jobs, [], TRAVEL, wait_limit_s=7200, late_slack_s=900)
    capped = build_time_window_edges(jobs, [], TRAVEL, wait_limit_s=7200, late_slack_s=900, k_succ=k)
    kept = set(capped.edges)
    for j in jobs:
        preds = sorted((full.edge_cost[e], e) for e in full.edges if e[1] == j.id)
        assert {e for _, e in preds[:k]} <= kept


def test_window_boundaries_are_inclusive():
    p = (1.3, 103.8)
    base = Job("a", 0.0, 1000.0, p, p)
    late_edge = Job("b", 900.0, 5000.0, p, p)     # r = e_a - late_slack: in
    too_early = Job("c", 899.0, 5000.0, p, p)     # out
    wait_edge = Job("d", 1500.0, 5000.0, p, p)    # r = e_a + wait: in
    too_late = Job("e", 1501.0, 5000.0, p, p)     # out
    got = build_time_window_edges([base, late_edge, too_early, wait_edge, too_late], [], TRAVEL,
                                  wait_limit_s=500, late_slack_s=100)
    out_of_a = {e[1] for e in got.edges if e[0] == "a"}
    assert out_of_a == {"b", "d"}


def test_successor_must_end_after_its_predecessor():
    p = (1.3, 103.8)
    a = Job("a", 0.0, 1000.0, p, p)
    b = Job("b", 950.0, 990.0, p, p)        # in a's window but ends before a ends
    tie1 = Job("t1", 2000.0, 3000.0, p, p)
    tie2 = Job("t2", 2000.0, 3000.0, p, p)  # same end: input order decides, one direction only
    got = build_time_window_edges([a, b, tie1, tie2], [], TRAVEL, wait_limit_s=5000, late_slack_s=1500)
    assert ("a", "b") not in got.edges and ("b", "a") in got.edges
    assert ("t1", "t2") in got.edges and ("t2", "t1") not in got.edges


@pytest.mark.parametrize("seed", range(5))
def test_arc_set_is_a_dag(seed):
    jobs, _ = _rand(seed, n=120, ties=True)
    got = build_time_window_edges(jobs, [], TRAVEL, wait_limit_s=20000, late_slack_s=20000)
    succ = {}
    for i, j in got.edges:
        succ.setdefault(i, []).append(j)
    state = {}

    def dfs(u):
        state[u] = 1
        for v in succ.get(u, []):
            assert state.get(v) != 1, "cycle"
            if v not in state:
                dfs(v)
        state[u] = 2

    for j in jobs:
        if j.id not in state:
            dfs(j.id)


def test_max_reposition_applies_to_start_edges_only():
    p0, far = (1.30, 103.80), (1.30, 103.90)   # ~11.1 km apart: ~1113 s at 10 m/s
    a = Job("a", 0.0, 100.0, p0, p0)
    b = Job("b", 200.0, 300.0, far, far)
    st = Start("t", 0.0, p0)
    got = build_time_window_edges([a, b], [st], TRAVEL, wait_limit_s=1000, late_slack_s=0, max_reposition_s=1000)
    assert ("a", "b") in got.edges            # job edges are not reposition-capped
    assert got.start_edges == [("t", "a")]
    got2 = build_time_window_edges([a, b], [st], TRAVEL, wait_limit_s=1000, late_slack_s=0, max_reposition_s=1200)
    assert got2.start_edges == [("t", "a"), ("t", "b")]


def test_bad_inputs_refused():
    p = (1.3, 103.8)
    with pytest.raises(ValueError, match="unique"):
        build_time_window_edges([Job("a", 0, 1, p, p), Job("a", 0, 1, p, p)], [], TRAVEL,
                                wait_limit_s=1, late_slack_s=0)
    with pytest.raises(ValueError, match="collide"):
        build_time_window_edges([Job("a", 0, 1, p, p)], [Start("a", 0, p)], TRAVEL,
                                wait_limit_s=1, late_slack_s=0)
    with pytest.raises(ValueError, match="k_succ"):
        build_time_window_edges([], [], TRAVEL, wait_limit_s=1, late_slack_s=0, k_succ=0)
    assert build_time_window_edges([], [], TRAVEL, wait_limit_s=1, late_slack_s=0).edges == []


def test_feeds_the_solver_end_to_end():
    jobs, starts = _rand(3, n=30, n_starts=5)
    tw = build_time_window_edges(jobs, starts, TRAVEL, wait_limit_s=7200, late_slack_s=900,
                                 k_succ=5, k_start=5)
    owner = {j.id: "A" if k % 2 else "B" for k, j in enumerate(jobs)}
    start_nodes = {s.id: {"company": "A" if k % 2 else "B"} for k, s in enumerate(starts)}
    params = {
        "instance_id": "tw", "companies": ["A", "B"], "orders": [j.id for j in jobs],
        "order_owner": owner, "cooperation": {("A", "A"), ("B", "B"), ("A", "B"), ("B", "A")},
        "vehicle_limit": {"A": 0, "B": 0}, "raw_cost": 0.0, "use_trailer_cost": False,
        "start_nodes": start_nodes, "defer_penalty": 100_000.0, **tw.as_solver_params(),
    }
    result, stats = nfs.NetworkFlowSolver().solve(params)
    assert result["status"] == 0
    ch = extract_chains(result, start_nodes)
    assert ch.orphaned == []
    edges = set(tw.edges)
    served = []
    for sid, chain in ch.by_start.items():
        if chain:
            assert (sid, chain[0]) in set(tw.start_edges)
        assert all(e in edges for e in zip(chain, chain[1:]))
        served += chain
    assert len(served) + len(ch.unplanned) == len(jobs) and len(served) > 5
