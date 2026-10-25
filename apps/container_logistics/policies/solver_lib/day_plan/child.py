"""Day-plan solve CHILD process: :class:`instance.PlanInputs` in (stdin), itineraries out (stdout).

Run as ``python -m apps.container_logistics.policies.solver_lib.day_plan.child`` by
:func:`runner.solve`, never in-process (SCIP holds the GIL and N0 measured a MIP child at
11.6 GB RSS; plan §4 ``runner.py``). stdin is one JSON object
``{"inputs": <PlanInputs>, "backend": "auto"|"mincostflow"|"mip"|"greedy",
"time_limit_s": float|null}``; stdout is one JSON object, either
``{"ok": true, "itineraries": {truck: [job, ...]}, "unplanned": [job, ...], "stats": {...}}``
or ``{"ok": false, "reason": str, "stats": {...}}`` (a refusal the parent turns into
``SolveRejected``). Anything else on stdout is a bug: library logging goes to stderr.
Exit codes: 0 answered, 1 crashed (traceback on stderr), 3 out of memory.

Per cooperation component of the class-level cooperation graph (independent, solved one
after another), the path is:

- ``mincostflow`` -- where :func:`coop_dispatch.min_cost_flow.mcf_applicable` holds and
  the backend allows it (``auto`` / ``mincostflow``): exact, integer metre costs, NO K cap
  (N0: K=20 cost +12.6 % empty km). Solved through ``backends.solve_by_component``.
- ``mip`` -- elsewhere, or everywhere under ``backend="mip"``: the library's MIP on K-capped
  arcs (``mip_k_succ`` / ``mip_k_start``, default 10), refused above ``mip_arc_budget``
  (``limits.check_mip_arc_budget``), time-limited, FEASIBLE accepted, checked by the
  quality guard against ``backends.served_reference``.
- ``greedy`` -- the fallback when the MIP has no usable solution, is over budget, or is a
  poor incumbent (and the whole run under ``backend="greedy"``, which needs no OR-Tools):
  jobs in (release, id) order, each to the cheapest feasible arc from a free truck start
  or a chain tail. A greedy fallback that still serves below the guard threshold of the
  served reference refuses the solve (``poor_incumbent``) so the caller keeps its plan.

Arcs come from :func:`coop_dispatch.time_window_edges.build_time_window_edges`, with the
per-job late slack the builder cannot take as a scalar applied exactly (see
:func:`_component_arcs`). Previous-version arcs get the ``stability_bonus_m`` discount
(N0: 1,975 -> 73 jobs moved for +0.2 km); costs stay integer metres, so the flow stays
exact. Reported km always use the undiscounted costs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import resource
import sys
import time
import traceback
from typing import Any, Mapping

import numpy as np

EXIT_OK, EXIT_CRASH, EXIT_OOM = 0, 1, 3
BACKENDS = ("auto", "mincostflow", "mip", "greedy")


def _twe():
    """``time_window_edges`` without OR-Tools when need be: the package ``__init__`` of
    ``coop_dispatch`` imports the MIP (ortools) eagerly, the module itself only numpy."""
    try:
        from ..coop_dispatch import time_window_edges as twe
        return twe
    except ImportError:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "coop_dispatch", "time_window_edges.py")
        spec = importlib.util.spec_from_file_location("_day_plan_time_window_edges", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod


# ----------------------------------------------------------------------------- arcs


_NO_SUCC = 1e15  # an end no release reaches: the job gets no successors in that builder call
_START_CHUNK = 200


def _component_arcs(inputs: Mapping[str, Any], jobs: list[dict], trucks: list[dict], *,
                    k_succ, k_start) -> dict[str, Any]:
    """Job->job and truck->job arcs of one component, with their true costs (m).

    Rule (plan D3, per-job late slack ``s_i`` from the time model, design §6a):
    i -> j iff ``e_i - s_i <= r_j <= e_i + W``, ``(e_j, j) > (e_i, i)`` (the builder's DAG
    order) and some class may serve both; truck t -> j iff j is eligible for t's class,
    ``r_j >= ready_t - s_j`` and (when set) travel <= ``max_reposition_s``.

    The builder takes ONE late slack, so it is called once per distinct slack value: the
    jobs of that value keep their end and get successors under exactly their slack, every
    other job gets an end no release reaches (so it has no successors in that call, and,
    as a target, the true DAG order is re-applied here). The union over the calls is the
    exact arc set, with no widened window; each call's dicts are freed before the next.
    Start arcs come from calls in which every job has that unreachable end (no job arcs
    at all), :data:`_START_CHUNK` trucks at a time. K caps (``_keep_k``, the builder's own
    rule) apply to the final set only, so a cap never spends its K on a rejected arc.
    """
    twe = _twe()
    p = inputs["params"]
    W = float(p["wait_limit_min"]) * 60.0
    travel = twe.haversine_travel(speed_kmh=float(p["speed_kmh"]), detour=float(p["detour"]))
    n = len(jobs)
    out = {"edges": [], "edge_cost": {}, "start_edges": [], "start_cost": {}}
    if n == 0:
        return out
    ids = [j["id"] for j in jobs]
    r = np.array([j["r"] for j in jobs], dtype=float)
    e = np.array([j["e"] for j in jobs], dtype=float)
    s = np.array([j["slack"] for j in jobs], dtype=float)
    cidx = {c: k for k, c in enumerate(sorted({c for j in jobs for c in j["classes"]} |
                                              {t["class"] for t in trucks}))}
    if len(cidx) > 62:
        raise ValueError(f"{len(cidx)} classes in one component: more than the 62 a bit mask holds")
    mask = np.array([sum(1 << cidx[c] for c in j["classes"]) for j in jobs], dtype=np.int64)
    pts = [(tuple(j["pickup"]), tuple(j["dropoff"])) for j in jobs]

    def job_list(ends):
        return [twe.Job(id=k, release_s=j["r"], end_s=float(ends[k]), pickup=pts[k][0], dropoff=pts[k][1])
                for k, j in enumerate(jobs)]

    # ---- job -> job, one builder call per slack value ----
    S, D, C = [], [], []
    for sv in np.unique(s).tolist():
        grp = s == sv
        ends = np.where(grp, e, _NO_SUCC)
        built = twe.build_time_window_edges(job_list(ends), [], travel, wait_limit_s=W, late_slack_s=sv)
        m = len(built.edges)
        if m:
            src = np.fromiter((a for a, _ in built.edges), dtype=np.int64, count=m)
            dst = np.fromiter((b for _, b in built.edges), dtype=np.int64, count=m)
            cost = np.fromiter((built.edge_cost[x] for x in built.edges), dtype=float, count=m)
            S.append(src); D.append(dst); C.append(cost)
        del built
    if S:
        src, dst, cost = np.concatenate(S), np.concatenate(D), np.concatenate(C)
        del S, D, C
        keep = (e[dst] > e[src]) | ((e[dst] == e[src]) & (dst > src))   # the true DAG order
        keep &= (mask[src] & mask[dst]) != 0                             # a class serves both
        src, dst, cost = src[keep], dst[keep], cost[keep]
        if k_succ is not None and src.size:
            m = twe._keep_k(src, cost, dst, k_succ) | twe._keep_k(dst, cost, src, k_succ)
            src, dst, cost = src[m], dst[m], cost[m]
        order = np.lexsort((dst, src))
        src, dst, cost = src[order].tolist(), dst[order].tolist(), cost[order].tolist()
        edges = [(ids[a], ids[b]) for a, b in zip(src, dst)]
        out["edges"] = edges
        out["edge_cost"] = dict(zip(edges, cost))

    # ---- truck -> job ----
    if trucks:
        no_succ = job_list(np.full(n, _NO_SUCC))
        ready = np.array([t["ready_s"] for t in trucks], dtype=float)
        tcls = np.array([cidx[t["class"]] for t in trucks], dtype=np.int64)
        SS, SD, SC = [], [], []
        for lo in range(0, len(trucks), _START_CHUNK):
            chunk = [twe.Start(id=("t", k), ready_s=trucks[k]["ready_s"], location=tuple(trucks[k]["loc"]))
                     for k in range(lo, min(lo + _START_CHUNK, len(trucks)))]
            built = twe.build_time_window_edges(no_succ, chunk, travel, wait_limit_s=0.0, late_slack_s=0.0,
                                                max_reposition_s=p.get("max_reposition_s"),
                                                start_late_slack_s=float(s.max()))
            m = len(built.start_edges)
            if m:
                ss = np.fromiter((a[1] for a, _ in built.start_edges), dtype=np.int64, count=m)
                sd = np.fromiter((b for _, b in built.start_edges), dtype=np.int64, count=m)
                sc = np.fromiter((built.start_edge_cost[x] for x in built.start_edges), dtype=float, count=m)
                keep = (r[sd] >= ready[ss] - s[sd]) & (((mask[sd] >> tcls[ss]) & 1) == 1)
                SS.append(ss[keep]); SD.append(sd[keep]); SC.append(sc[keep])
            del built
        if SS:
            ss, sd, sc = np.concatenate(SS), np.concatenate(SD), np.concatenate(SC)
            if k_start is not None and ss.size:
                m = twe._keep_k(ss, sc, sd, k_start) | twe._keep_k(sd, sc, ss, k_start)
                ss, sd, sc = ss[m], sd[m], sc[m]
            order = np.lexsort((sd, ss))
            ss, sd, sc = ss[order].tolist(), sd[order].tolist(), sc[order].tolist()
            sedges = [(trucks[a]["id"], ids[b]) for a, b in zip(ss, sd)]
            out["start_edges"] = sedges
            out["start_cost"] = dict(zip(sedges, sc))
    return out


def _vehicled(inputs, comp, trucks) -> set:
    return {t["class"] for t in trucks}


def _class_attrs(inputs, comp, trucks) -> dict[str, str]:
    """``class_attrs`` for ``mcf_applicable`` (canonical JSON strings): a vehicle-less
    class takes the attributes of the component's vehicled classes when those agree."""
    vehicled = _vehicled(inputs, comp, trucks)
    attrs = {c: json.dumps(inputs["class_attrs"][c], sort_keys=True) for c in comp}
    real = {attrs[c] for c in vehicled}
    if len(real) == 1:
        ref = next(iter(real))
        for c in comp:
            if c not in vehicled:
                attrs[c] = ref
    return attrs


def _component_params(inputs, comp, jobs, trucks, arcs) -> dict[str, Any]:
    """``NetworkFlowSolver`` / min-cost-flow params of one component (classes are companies)."""
    p = inputs["params"]
    cset = set(comp)
    vehicled = _vehicled(inputs, comp, trucks)
    vehicleless = [c for c in comp if c not in vehicled]
    bonus = float(p.get("stability_bonus_m") or 0.0)
    prev = {tuple(a) for a in inputs.get("prev_arcs", ())} if bonus else set()
    edge_cost = {k: (v - bonus if k in prev else v) for k, v in arcs["edge_cost"].items()}
    start_cost = {k: (v - bonus if k in prev else v) for k, v in arcs["start_cost"].items()}
    eligible = set()
    for j in jobs:
        ec = set(j["classes"]) & cset
        for c in ec:
            eligible.add((j["id"], c))
        # A vehicle-less class serves nothing; giving it the job iff every vehicled class
        # of the component may serve it keeps a uniform component uniform for the flow.
        if vehicled and vehicled <= ec:
            for c in vehicleless:
                eligible.add((j["id"], c))
    return {
        "instance_id": f"d{inputs['day']}:{'+'.join(comp)}",
        "companies": list(comp),
        "orders": [j["id"] for j in jobs],
        "order_owner": {j["id"]: j["owner_class"] for j in jobs},
        "cooperation": {(a, b) for a, b in inputs["cooperation"] if a in cset and b in cset},
        "vehicle_limit": {c: 0 for c in comp},
        "edges_input": list(arcs["edges"]),
        "edge_cost_input": edge_cost,
        "edge_trailer_cost_input": {k: 0 for k in arcs["edges"]},
        "raw_cost": 0,
        "use_trailer_cost": False,
        "start_nodes": {t["id"]: {"company": t["class"]} for t in trucks},
        "start_edges": list(arcs["start_edges"]),
        "start_edge_cost": start_cost,
        "defer_penalty": float(p["defer_penalty_km"]) * 1000.0,
        "eligible": eligible,
        "class_attrs": _class_attrs(inputs, comp, trucks),
        "time_limit_s": None,
        "mip_gap": p.get("mip_gap"),
        "accept_feasible": True,
    }


# --------------------------------------------------------------------------- greedy


def _greedy(params: Mapping[str, Any], jobs: list[dict]) -> dict[str, list]:
    """Chains by start node: jobs in (release, id) order, each appended to the chain tail
    with the cheapest feasible arc, else started on the nearest unused truck (extending
    a chain first keeps trucks free for jobs no tail can reach)."""
    elig = params["eligible"]
    company = {sid: spec["company"] for sid, spec in params["start_nodes"].items()}
    preds: dict[Any, list] = {}
    for (i, j) in params["edges_input"]:
        preds.setdefault(j, []).append(i)
    starts_of: dict[Any, list] = {}
    for (sid, j) in params["start_edges"]:
        starts_of.setdefault(j, []).append(sid)
    tail_of: dict[Any, Any] = {}     # chain tail job -> its truck
    used: set = set()
    chains: dict[Any, list] = {sid: [] for sid in sorted(company)}
    for j in sorted(jobs, key=lambda x: (x["r"], x["id"])):
        jid = j["id"]
        best = None
        for sid in starts_of.get(jid, ()):
            if sid in used or (jid, company[sid]) not in elig:
                continue
            cand = (1, params["start_edge_cost"][(sid, jid)], str(sid), sid)
            best = cand if best is None or cand < best else best
        for i in preds.get(jid, ()):
            sid = tail_of.get(i)
            if sid is None or (jid, company[sid]) not in elig:
                continue
            cand = (0, params["edge_cost_input"][(i, jid)], str(sid), sid)
            best = cand if best is None or cand < best else best
        if best is None:
            continue
        sid = best[3]
        if chains[sid]:
            tail_of.pop(chains[sid][-1], None)
        else:
            used.add(sid)
        chains[sid].append(jid)
        tail_of[jid] = sid
    return chains


# ---------------------------------------------------------------------------- solve


def _true_km(chains: Mapping[Any, list], arcs) -> tuple[dict, float]:
    first, total = {}, 0.0
    for sid, chain in chains.items():
        if not chain:
            continue
        first[sid] = arcs["start_cost"][(sid, chain[0])]
        total += first[sid]
        for a, b in zip(chain, chain[1:]):
            total += arcs["edge_cost"][(a, b)]
    return first, total


def solve_inputs(inputs: Mapping[str, Any], *, backend: str = "auto",
                 time_limit_s: float | None = None) -> dict[str, Any]:
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS} (got {backend!r})")
    p = inputs["params"]
    t_all = time.time()
    jobs_by_class: dict[str, list] = {}
    for j in inputs["jobs"]:
        jobs_by_class.setdefault(j["owner_class"], []).append(j)
    trucks_by_class: dict[str, list] = {}
    for t in inputs["trucks"]:
        trucks_by_class.setdefault(t["class"], []).append(t)
    if backend != "greedy":
        from ..coop_dispatch import backends as bk
        from ..coop_dispatch.chains import extract_chains
        from ..coop_dispatch.limits import ArcBudgetExceeded, check_mip_arc_budget
        from ..coop_dispatch.min_cost_flow import cooperation_components, mcf_applicable
        comps = cooperation_components(list(inputs["classes"]), [tuple(x) for x in inputs["cooperation"]])
    else:
        comps = [list(c) for c in inputs["components"]]

    itineraries: dict[str, list] = {}
    rows = []
    build_ms = solve_ms = 0.0
    n_arcs = n_start = 0
    deterministic = True
    empty_m = 0.0
    first_leg_m: dict[str, float] = {}
    for comp in comps:
        comp = sorted(comp)
        jobs = sorted((j for c in comp for j in jobs_by_class.get(c, ())), key=lambda x: x["id"])
        trucks = sorted((t for c in comp for t in trucks_by_class.get(c, ())), key=lambda x: x["id"])
        row: dict[str, Any] = {"classes": comp, "orders": len(jobs), "trucks": len(trucks)}
        if backend == "greedy":
            path, why = "greedy", "backend greedy"
        else:
            coop_c = {(a, b) for a, b in inputs["cooperation"] if a in set(comp) and b in set(comp)}
            ok, why = mcf_applicable(comp, coop_c, _class_attrs(inputs, comp, trucks))
            path = "mincostflow" if ok and backend != "mip" else "mip"
            if backend == "mip":
                why = "backend mip"
        t0 = time.time()
        caps = ((p.get("k_succ"), p.get("k_start")) if path != "mip"
                else (p.get("mip_k_succ"), p.get("mip_k_start")))
        arcs = _component_arcs(inputs, jobs, trucks, k_succ=caps[0], k_start=caps[1])
        params = _component_params(inputs, comp, jobs, trucks, arcs)
        b_ms = (time.time() - t0) * 1000.0
        build_ms += b_ms
        n_arcs += len(arcs["edges"])
        n_start += len(arcs["start_edges"])
        row.update(arcs=len(arcs["edges"]), start_arcs=len(arcs["start_edges"]), build_ms=round(b_ms, 1))
        t1 = time.time()
        chains = None
        fallback = None
        if path in ("mincostflow", "mip") and jobs:
            if path == "mip":
                params["time_limit_s"] = (float(time_limit_s) if time_limit_s is not None
                                          else float(p["mip_time_limit_s"]))
                try:
                    row["mip_arcs"] = check_mip_arc_budget(params, int(p["mip_arc_budget"]))
                except ArcBudgetExceeded as exc:
                    fallback, row["mip_error"] = "arc_budget", str(exc)
            if fallback is None:
                result, cstats = bk.solve_by_component(
                    params, "auto" if path == "mincostflow" else "mip",
                    guard_threshold=float(p["guard_threshold"]))
                crow = cstats["components"][0] if cstats["components"] else {}
                row.update(status=crow.get("status"), objective=crow.get("objective"),
                           served=crow.get("served"))
                if crow.get("backend") and crow["backend"] != path:
                    raise RuntimeError(f"component {comp}: expected {path}, solve_by_component ran {crow['backend']}")
                if crow.get("guard"):
                    row["guard"] = crow["guard"]
                if result.get("routes") is None:
                    fallback = "no_solution"
                elif crow.get("guard", {}).get("ok") is False:
                    fallback = "poor_incumbent"
                else:
                    ch = extract_chains(result, params["start_nodes"])
                    if ch.orphaned or ch.depot:
                        raise RuntimeError(f"component {comp}: chains not rooted at trucks "
                                           f"(orphaned {ch.orphaned[:5]}, depot {list(ch.depot)[:5]})")
                    chains = ch.by_start
                    if path == "mip" and result.get("status") != bk.OPTIMAL:
                        deterministic = False
        if chains is None:
            if fallback is not None:
                row["fallback"] = fallback
                path = "greedy"
            chains = _greedy(params, jobs) if jobs else {t["id"]: [] for t in trucks}
            if fallback is not None and backend != "greedy":
                served = sum(len(c) for c in chains.values())
                ref = bk.served_reference(params)
                g = bk.quality_guard(served, ref, float(p["guard_threshold"]))
                row["fallback_guard"] = g
                if not g["ok"]:
                    s_ms = (time.time() - t1) * 1000.0
                    row.update(backend=path, reason=why, solve_ms=round(s_ms, 1))
                    rows.append(row)
                    return {"ok": False, "reason": bk.POOR_INCUMBENT,
                            "stats": _stats(rows, build_ms, solve_ms + s_ms, n_arcs, n_start,
                                            deterministic, 0.0, {}, t_all)}
        s_ms = (time.time() - t1) * 1000.0
        solve_ms += s_ms
        row.update(backend=path, reason=why, solve_ms=round(s_ms, 1),
                   planned=sum(len(c) for c in chains.values()))
        first, km = _true_km(chains, arcs)
        empty_m += km
        first_leg_m.update(first)
        row["empty_m"] = round(km, 1)
        rows.append(row)
        for sid, chain in chains.items():
            if chain:
                itineraries[sid] = list(chain)

    planned = {j for c in itineraries.values() for j in c}
    unplanned = sorted(j["id"] for j in inputs["jobs"] if j["id"] not in planned)
    return {"ok": True, "itineraries": dict(sorted(itineraries.items())), "unplanned": unplanned,
            "stats": _stats(rows, build_ms, solve_ms, n_arcs, n_start, deterministic, empty_m,
                            first_leg_m, t_all)}


def _stats(rows, build_ms, solve_ms, n_arcs, n_start, deterministic, empty_m, first_leg_m, t_all):
    backends: dict[str, int] = {}
    for r in rows:
        backends[r.get("backend", "?")] = backends.get(r.get("backend", "?"), 0) + 1
    return {
        "components": rows, "backends": dict(sorted(backends.items())),
        "status": ("degraded" if any(r.get("fallback") for r in rows) else "ok"),
        "build_ms": round(build_ms, 1), "solve_ms": round(solve_ms, 1),
        "child_ms": round((time.time() - t_all) * 1000.0, 1),
        "arcs": n_arcs, "start_arcs": n_start, "deterministic": deterministic,
        "empty_m": round(empty_m, 1),
        "first_leg_m": {k: round(v, 1) for k, v in sorted(first_leg_m.items())},
        "maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # nothing but the answer reaches stdout
    try:
        req = json.load(sys.stdin)
        out = solve_inputs(req["inputs"], backend=req.get("backend", "auto"),
                           time_limit_s=req.get("time_limit_s"))
    except MemoryError:
        traceback.print_exc(file=sys.stderr)
        return EXIT_OOM
    except BaseException:  # noqa: BLE001 - the parent reads the traceback from stderr
        traceback.print_exc(file=sys.stderr)
        return EXIT_CRASH
    json.dump(out, real_stdout, separators=(",", ":"), allow_nan=False)
    real_stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
