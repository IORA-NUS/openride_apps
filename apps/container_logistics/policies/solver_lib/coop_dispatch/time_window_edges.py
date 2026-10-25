"""Time-window edge builder for day plans (OpenRide addition, plan N2 item 4; not upstream).

Builds the job->job and truck->job arcs for :class:`NetworkFlowSolver` (``edges_input`` /
``edge_cost_input`` and ``start_edges`` / ``start_edge_cost``) in time linear in
jobs x window, instead of upstream ``EdgeBuilder``'s all-pairs product.

Feasibility (plan D3: a job's window is its release ``r``; ``e`` is its predicted end,
release to completion):

- job i -> job j iff ``e_i - late_slack_s <= r_j <= e_i + wait_limit_s`` and
  ``(e_j, idx_j) > (e_i, idx_i)``. The second condition is an ADDITION to the plan's rule:
  a successor that ends before its predecessor cannot follow it, and the strict order on
  ``(end, input index)`` makes the arc set a DAG, so the solver (which has no subtour
  elimination) can never return a cycle of jobs that no truck reaches.
- truck s -> job j iff ``travel_s(s -> pickup_j) <= max_reposition_s`` (when given), and
  optionally ``r_j >= ready_s - start_late_slack_s`` / ``r_j - ready_s <= start_wait_limit_s``
  (both unbounded by default: a parked truck may wait for, or arrive late to, its first job).

K caps (``None`` = unbounded): each job keeps its ``k_succ`` cheapest successors, and each
job also keeps its ``k_succ`` cheapest predecessors (so a job's in-degree is not starved by
other jobs' successor lists); the arc set is the union. Start arcs: each truck's ``k_start``
nearest feasible jobs, union each job's ``k_start`` nearest trucks. Ties break on input
order, so the result is deterministic. Output arcs are sorted by (source, target) input index.

Arc cost is the empty-leg distance in meters from ``travel_fn``, a vectorised callable
``travel_fn(lat1, lng1, lat2, lng2) -> (meters, seconds)`` over numpy arrays;
:func:`haversine_travel` makes one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Sequence

import numpy as np

TravelFn = Callable[[np.ndarray, np.ndarray, np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]

_EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class Job:
    id: Hashable
    release_s: float
    end_s: float
    pickup: tuple[float, float]   # (lat, lng)
    dropoff: tuple[float, float]  # (lat, lng)


@dataclass(frozen=True)
class Start:
    id: Hashable
    ready_s: float
    location: tuple[float, float]  # (lat, lng)


@dataclass
class TimeWindowEdges:
    edges: list[tuple[Any, Any]] = field(default_factory=list)
    edge_cost: dict[tuple[Any, Any], float] = field(default_factory=dict)
    edge_duration: dict[tuple[Any, Any], float] = field(default_factory=dict)
    start_edges: list[tuple[Any, Any]] = field(default_factory=list)
    start_edge_cost: dict[tuple[Any, Any], float] = field(default_factory=dict)
    start_edge_duration: dict[tuple[Any, Any], float] = field(default_factory=dict)

    def as_solver_params(self) -> dict[str, Any]:
        """The ``NetworkFlowSolver`` params these arcs fill (trailer cost zero everywhere)."""
        return {
            "edges_input": list(self.edges),
            "edge_cost_input": dict(self.edge_cost),
            "edge_trailer_cost_input": {e: 0 for e in self.edges},
            "start_edges": list(self.start_edges),
            "start_edge_cost": dict(self.start_edge_cost),
        }


def haversine_travel(speed_kmh: float = 40.0, detour: float = 1.0) -> TravelFn:
    """Great-circle distance x ``detour``, at a constant ``speed_kmh``."""
    mps = speed_kmh / 3.6

    def travel(lat1, lng1, lat2, lng2):
        p1, p2 = np.radians(lat1), np.radians(lat2)
        dp, dl = p2 - p1, np.radians(lng2) - np.radians(lng1)
        h = np.sin(dp / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2.0) ** 2
        m = 2.0 * _EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0))) * detour
        return m, m / mps

    return travel


def _keep_k(groups: np.ndarray, cost: np.ndarray, tie: np.ndarray, k: int | None) -> np.ndarray:
    """Boolean mask: within each value of ``groups`` keep the ``k`` smallest (cost, tie)."""
    if k is None or groups.size == 0:
        return np.ones(groups.size, dtype=bool)
    order = np.lexsort((tie, cost, groups))
    g = groups[order]
    starts = np.r_[0, np.flatnonzero(g[1:] != g[:-1]) + 1]
    rank = np.arange(g.size) - np.repeat(starts, np.diff(np.r_[starts, g.size]))
    mask = np.zeros(groups.size, dtype=bool)
    mask[order[rank < k]] = True
    return mask


def build_time_window_edges(
    jobs: Sequence[Job],
    starts: Sequence[Start],
    travel_fn: TravelFn,
    *,
    wait_limit_s: float,
    late_slack_s: float,
    k_succ: int | None = None,
    k_start: int | None = None,
    max_reposition_s: float | None = None,
    start_late_slack_s: float | None = None,
    start_wait_limit_s: float | None = None,
) -> TimeWindowEdges:
    for name, k in (("k_succ", k_succ), ("k_start", k_start)):
        if k is not None and (not isinstance(k, int) or k < 1):
            raise ValueError(f"{name} must be a positive int or None (got {k!r})")
    ids = [j.id for j in jobs]
    if len(set(ids)) != len(ids):
        raise ValueError("job ids must be unique")
    n = len(jobs)
    out = TimeWindowEdges()
    if n == 0:
        return out
    r = np.array([j.release_s for j in jobs], dtype=float)
    e = np.array([j.end_s for j in jobs], dtype=float)
    plat = np.array([j.pickup[0] for j in jobs], dtype=float)
    plng = np.array([j.pickup[1] for j in jobs], dtype=float)
    dlat = np.array([j.dropoff[0] for j in jobs], dtype=float)
    dlng = np.array([j.dropoff[1] for j in jobs], dtype=float)

    # ---- job -> job: bisect each job's release window on the release-sorted order ----
    by_r = np.argsort(r, kind="stable")
    r_sorted = r[by_r]
    lo = np.searchsorted(r_sorted, e - late_slack_s, side="left")
    hi = np.searchsorted(r_sorted, e + wait_limit_s, side="right")
    src_parts, dst_parts = [], []
    for i in range(n):
        if hi[i] <= lo[i]:
            continue
        cand = by_r[lo[i]:hi[i]]
        ok = (e[cand] > e[i]) | ((e[cand] == e[i]) & (cand > i))
        cand = cand[ok]
        if cand.size:
            src_parts.append(np.full(cand.size, i, dtype=np.int64))
            dst_parts.append(cand.astype(np.int64))
    if src_parts:
        src = np.concatenate(src_parts)
        dst = np.concatenate(dst_parts)
        m, s = travel_fn(dlat[src], dlng[src], plat[dst], plng[dst])
        m = np.asarray(m, dtype=float)
        s = np.asarray(s, dtype=float)
        keep = _keep_k(src, m, dst, k_succ) | _keep_k(dst, m, src, k_succ)
        src, dst, m, s = src[keep], dst[keep], m[keep], s[keep]
        order = np.lexsort((dst, src))
        for t in order.tolist():
            key = (ids[src[t]], ids[dst[t]])
            out.edges.append(key)
            out.edge_cost[key] = float(m[t])
            out.edge_duration[key] = float(s[t])

    # ---- truck -> job ----
    if starts:
        sids = [st.id for st in starts]
        if len(set(sids)) != len(sids):
            raise ValueError("start ids must be unique")
        if set(sids) & set(ids):
            raise ValueError("start ids must not collide with job ids")
        ss_parts, sd_parts, sm_parts, sdur_parts = [], [], [], []
        for si, st in enumerate(starts):
            ok = np.ones(n, dtype=bool)
            if start_late_slack_s is not None:
                ok &= r >= st.ready_s - start_late_slack_s
            if start_wait_limit_s is not None:
                ok &= (r - st.ready_s) <= start_wait_limit_s
            cand = np.flatnonzero(ok)
            if cand.size == 0:
                continue
            m, s = travel_fn(np.full(cand.size, st.location[0]), np.full(cand.size, st.location[1]),
                             plat[cand], plng[cand])
            m = np.asarray(m, dtype=float)
            s = np.asarray(s, dtype=float)
            if max_reposition_s is not None:
                fit = s <= max_reposition_s
                cand, m, s = cand[fit], m[fit], s[fit]
            if cand.size:
                ss_parts.append(np.full(cand.size, si, dtype=np.int64))
                sd_parts.append(cand.astype(np.int64))
                sm_parts.append(m)
                sdur_parts.append(s)
        if ss_parts:
            ssrc = np.concatenate(ss_parts)
            sdst = np.concatenate(sd_parts)
            sm = np.concatenate(sm_parts)
            sdur = np.concatenate(sdur_parts)
            keep = _keep_k(ssrc, sm, sdst, k_start) | _keep_k(sdst, sm, ssrc, k_start)
            ssrc, sdst, sm, sdur = ssrc[keep], sdst[keep], sm[keep], sdur[keep]
            order = np.lexsort((sdst, ssrc))
            for t in order.tolist():
                key = (sids[ssrc[t]], ids[sdst[t]])
                out.start_edges.append(key)
                out.start_edge_cost[key] = float(sm[t])
                out.start_edge_duration[key] = float(sdur[t])
    return out
