"""Day-plan solve CHILD process: :class:`instance.PlanInputs` in (stdin), itineraries out (stdout).

Run as ``python -m apps.container_logistics.policies.solver_lib.day_plan.child`` by
:func:`runner.solve`, never in-process (SCIP holds the GIL and N0 measured a MIP child at
11.6 GB RSS; plan §4 ``runner.py``). stdin is one JSON object
``{"inputs": <PlanInputs>, "backend": "auto"|"mincostflow"|"mip"|"greedy",
"time_limit_s": float|null, "deadline_epoch": float|null, "accept_poor": bool}``; stdout is one JSON object, either
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

Identical trucks (same class, EXACT position, same ready time) are one start node of
``capacity`` = their count (coop_dispatch E2; N0: exact, and rounding the key broke
reachability), so the arc count no longer scales with every parked truck. Its chains go
back to member trucks deterministically: a chain whose first job a member headed in the
previous version stays with that member, the rest go out in truck-id order.

Arcs come from :func:`coop_dispatch.time_window_edges.build_time_window_arrays`, with the
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


def _standalone(name: str):
    """``coop_dispatch.<name>`` without OR-Tools when need be: the package ``__init__``
    imports the MIP (ortools) eagerly, while ``time_window_edges`` (numpy) and ``limits``
    (stdlib) do not need it; on ImportError the module is loaded from its file."""
    try:
        return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.coop_dispatch.{name}")
    except ImportError:
        key = f"_day_plan_coop_{name}"
        if key in sys.modules:
            return sys.modules[key]
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "coop_dispatch", f"{name}.py")
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
        return mod


def _twe():
    return _standalone("time_window_edges")


# ----------------------------------------------------------------------------- arcs


_NO_SUCC = 1e15  # an end no release reaches: the job gets no successors in that builder call
_START_CHUNK = 200


def _merge_trucks(trucks: list[dict]) -> tuple[list[dict], dict]:
    """E2: identical trucks (same class, EXACT loc and ready time) -> one start of capacity c.
    Returns (starts, members): starts carry id = smallest member id, members[sid] = sorted ids."""
    groups: dict = {}
    for t in sorted(trucks, key=lambda x: x["id"]):
        groups.setdefault((t["class"], tuple(t["loc"]), float(t["ready_s"])), []).append(t)
    starts, members = [], {}
    for (cls, loc, ready), ts in groups.items():
        sid = ts[0]["id"]
        starts.append({"id": sid, "class": cls, "ready_s": ready, "loc": list(loc), "capacity": len(ts)})
        members[sid] = [t["id"] for t in ts]
    starts.sort(key=lambda x: x["id"])
    return starts, members


def _component_arcs(inputs: Mapping[str, Any], jobs: list[dict], trucks: list[dict], *,
                    k_succ, k_start) -> dict[str, Any]:
    """Job->job and start->job arcs of one component as index arrays, with true costs (m).

    ``src`` / ``dst`` index ``jobs``; ``ssrc`` indexes ``trucks`` (the merged starts of
    :func:`_merge_trucks`, or unit trucks on the greedy path); ``sdst`` indexes ``jobs``.

    Rule (plan D3, per-job late slack ``s_i`` from the time model, design §6a):
    i -> j iff ``e_i - s_i <= r_j <= e_i + W``, ``(e_j, j) > (e_i, i)`` (the builder's DAG
    order) and some class may serve both; start t -> j iff j is eligible for t's class,
    ``r_j >= ready_t - s_j`` and (when set) travel <= ``max_reposition_s``.

    The builder takes ONE late slack, so it is called once per distinct slack value: the
    jobs of that value keep their end and get successors under exactly their slack, every
    other job gets an end no release reaches (no successors in that call; as a target the
    true DAG order is re-applied here). The union over the calls is the exact arc set.
    Start arcs come from calls in which every job has that unreachable end,
    :data:`_START_CHUNK` starts at a time. K caps (``_keep_k``, the builder's own rule)
    apply to the final set only. Built with ``build_time_window_arrays`` (no per-arc
    Python objects, coop_dispatch E2).
    """
    twe = _twe()
    p = inputs["params"]
    W = float(p["wait_limit_min"]) * 60.0
    travel = twe.haversine_travel(speed_kmh=float(p["speed_kmh"]), detour=float(p["detour"]))
    n = len(jobs)
    z_i, z_f = np.zeros(0, dtype=np.int64), np.zeros(0, dtype=float)
    out = {"src": z_i, "dst": z_i, "cost": z_f, "ssrc": z_i, "sdst": z_i, "scost": z_f}
    if n == 0:
        return out
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

    S, D, C = [], [], []
    for sv in np.unique(s).tolist():
        grp = s == sv
        ends = np.where(grp, e, _NO_SUCC)
        built = twe.build_time_window_arrays(job_list(ends), [], travel, wait_limit_s=W, late_slack_s=sv)
        if built.src.size:
            S.append(built.src); D.append(built.dst); C.append(built.cost)
        del built
    if S:
        src, dst, cost = np.concatenate(S), np.concatenate(D), np.concatenate(C)
        del S, D, C
        keep = (e[dst] > e[src]) | ((e[dst] == e[src]) & (dst > src))
        keep &= (mask[src] & mask[dst]) != 0
        src, dst, cost = src[keep], dst[keep], cost[keep]
        if k_succ is not None and src.size:
            m = twe._keep_k(src, cost, dst, k_succ) | twe._keep_k(dst, cost, src, k_succ)
            src, dst, cost = src[m], dst[m], cost[m]
        order = np.lexsort((dst, src))
        out.update(src=src[order], dst=dst[order], cost=cost[order])

    if trucks:
        no_succ = job_list(np.full(n, _NO_SUCC))
        ready = np.array([t["ready_s"] for t in trucks], dtype=float)
        tcls = np.array([cidx[t["class"]] for t in trucks], dtype=np.int64)
        SS, SD, SC = [], [], []
        for lo in range(0, len(trucks), _START_CHUNK):
            chunk = [twe.Start(id=("t", k), ready_s=trucks[k]["ready_s"], location=tuple(trucks[k]["loc"]))
                     for k in range(lo, min(lo + _START_CHUNK, len(trucks)))]
            built = twe.build_time_window_arrays(no_succ, chunk, travel, wait_limit_s=0.0, late_slack_s=0.0,
                                                 max_reposition_s=p.get("max_reposition_s"),
                                                 start_late_slack_s=float(s.max()))
            if built.ssrc.size:
                ss, sd, sc = built.ssrc + lo, built.sdst, built.scost
                keep = (r[sd] >= ready[ss] - s[sd]) & (((mask[sd] >> tcls[ss]) & 1) == 1)
                SS.append(ss[keep]); SD.append(sd[keep]); SC.append(sc[keep])
            del built
        if SS:
            ss, sd, sc = np.concatenate(SS), np.concatenate(SD), np.concatenate(SC)
            if k_start is not None and ss.size:
                m = twe._keep_k(ss, sc, sd, k_start) | twe._keep_k(sd, sc, ss, k_start)
                ss, sd, sc = ss[m], sd[m], sc[m]
            order = np.lexsort((sd, ss))
            out.update(ssrc=ss[order], sdst=sd[order], scost=sc[order])
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


def _owner(inputs, tid: str) -> str:
    """The real truck of a start id (``T@wk`` -> ``T``; a real truck -> itself)."""
    return inputs.get("_of", {}).get(tid, tid)


def _start_owners(inputs, trucks) -> dict[str, list[int]]:
    """``{real truck id: [index of every start in trucks that is (or holds) it]}``."""
    mem = inputs.get("_members", {})
    out: dict[str, list[int]] = {}
    for k, t in enumerate(trucks):
        for tid in mem.get(t["id"], [t["id"]]):
            out.setdefault(_owner(inputs, tid), []).append(k)
    return out


def _prev_arc_survival(inputs, jobs, trucks, arcs) -> dict[str, int]:
    """M3 diagnostic: of the previous version's arcs inside this component, how many are
    still FEASIBLE arcs of the new instance (``prev_*_feasible``)."""
    jpos = {j["id"]: k for k, j in enumerate(jobs)}
    n = max(len(jobs), 1)
    tpos = _start_owners(inputs, trucks)
    ekeys = set((arcs["src"] * n + arcs["dst"]).tolist())
    skeys = set((arcs["ssrc"] * n + arcs["sdst"]).tolist())
    out = {"prev_job_arcs": 0, "prev_job_arcs_feasible": 0, "prev_start_arcs": 0, "prev_start_arcs_feasible": 0}
    for a, b in inputs.get("prev_arcs", ()):
        if b not in jpos:
            continue
        if a in jpos:
            out["prev_job_arcs"] += 1
            out["prev_job_arcs_feasible"] += int(jpos[a] * n + jpos[b] in ekeys)
        elif a in tpos:
            out["prev_start_arcs"] += 1
            out["prev_start_arcs_feasible"] += int(any(k * n + jpos[b] in skeys for k in tpos[a]))
    return out


def _prev_arcs_kept(inputs, chains: Mapping[str, list]) -> int:
    """Previous-version arcs the new plan uses (truck -> head with the SAME real truck, job -> job)."""
    prev = {tuple(a) for a in inputs.get("prev_arcs", ())}
    kept = 0
    for sid, chain in chains.items():
        if not chain:
            continue
        kept += int((_owner(inputs, sid), chain[0]) in prev)
        kept += sum(1 for a, b in zip(chain, chain[1:]) if (a, b) in prev)
    return kept


def _component_params(inputs, comp, jobs, trucks, arcs) -> dict[str, Any]:
    """``NetworkFlowSolver`` / min-cost-flow params of one component (classes are companies)."""
    p = inputs["params"]
    cset = set(comp)
    vehicled = _vehicled(inputs, comp, trucks)
    vehicleless = [c for c in comp if c not in vehicled]
    bonus = float(p.get("stability_bonus_m") or 0.0)
    prev = {tuple(a) for a in inputs.get("prev_arcs", ())} if bonus else set()
    jid = [j["id"] for j in jobs]
    ecost = arcs["cost"].copy()
    scost = arcs["scost"].copy()
    if prev:
        jpos = {x: k for k, x in enumerate(jid)}
        n = max(len(jid), 1)
        pe = np.array([jpos[a] * n + jpos[b] for a, b in prev if a in jpos and b in jpos], dtype=np.int64)
        if pe.size:
            ecost[np.isin(arcs["src"] * n + arcs["dst"], pe)] -= bonus
        # a merged start gets the bonus if ANY member truck had the arc; a later-window
        # start ``T@wk`` counts as truck ``T`` (the store and prev_arcs know only T)
        tpos = _start_owners(inputs, trucks)
        ps = np.array([k * n + jpos[b] for a, b in prev if a in tpos and b in jpos for k in tpos[a]],
                      dtype=np.int64)
        if ps.size:
            scost[np.isin(arcs["ssrc"] * n + arcs["sdst"], ps)] -= bonus
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
        "edges_array": {"src": arcs["src"], "dst": arcs["dst"], "cost": ecost},
        "raw_cost": 0,
        "use_trailer_cost": False,
        "start_nodes": {t["id"]: {"company": t["class"], "capacity": t.get("capacity", 1)} for t in trucks},
        "start_edges_array": {"src": arcs["ssrc"], "dst": arcs["sdst"], "cost": scost},
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


def _true_km(chains: Mapping[Any, list], arcs, jobs=None, trucks=None, members=None) -> tuple[dict, float]:
    jpos = {j["id"]: k for k, j in enumerate(jobs)}
    n = max(len(jobs), 1)
    ekey = arcs["src"] * n + arcs["dst"]
    eo = np.argsort(ekey); ek = ekey[eo]
    skey = arcs["ssrc"] * n + arcs["sdst"]
    so = np.argsort(skey); sk = skey[so]
    spos = {}
    for k, t in enumerate(trucks):
        for tid in (members or {}).get(t["id"], [t["id"]]):
            spos[tid] = k

    def look(keys, order, sorted_keys, cost, key):
        i = int(np.searchsorted(sorted_keys, key))
        if i >= sorted_keys.size or sorted_keys[i] != key:
            raise RuntimeError("a planned chain uses an arc that is not in the arc set")
        return float(cost[order[i]])

    first, total = {}, 0.0
    for tid, chain in chains.items():
        if not chain:
            continue
        first[tid] = look(skey, so, sk, arcs["scost"], spos[tid] * n + jpos[chain[0]])
        total += first[tid]
        for a, b in zip(chain, chain[1:]):
            total += look(ekey, eo, ek, arcs["cost"], jpos[a] * n + jpos[b])
    return first, total


#: Seconds the child keeps for writing its answer and exiting after the solve deadline.
EXIT_MARGIN_S = 1.0
#: Greedy reserve per remaining job (s): measured ~0.65 ms/job incl. its arc rebuild
#: (real day 1, 2,239 jobs, 1.4 s); doubled, plus a fixed second.
GREEDY_S_PER_JOB = 0.0013
GREEDY_FIXED_S = 1.0
#: pywraplp model build per MIP arc variable (N0: 185 k vars ~3 s), not covered by the
#: solver's own time limit.
MIP_BUILD_S_PER_ARC = 2e-5
#: Below this a MIP component is not attempted (greedy, ``fallback: deadline``).
MIN_MIP_S = 1.0


def solve_inputs(inputs: Mapping[str, Any], *, backend: str = "auto",
                 time_limit_s: float | None = None, deadline_epoch: float | None = None,
                 accept_poor: bool = False) -> dict[str, Any]:
    """Solve every cooperation component (module docstring); returns the child's answer.

    An instance without later-window starts (``T@wk``, ``of`` set: the evening half of an
    overnight shift) is ONE solve (:func:`_solve_once`, unchanged). With them the windows
    are solved in order (review V-m3): window 0 with the trucks' own starts, then each
    later window with its ``T@wk`` starts moved to where truck ``T``'s chain so far ENDS
    (the last job's dropoff, ready at ``max(window start, its predicted end)``) over the
    jobs still unplanned, so the later chain's first empty leg (and ``first_leg_km``) is
    measured from the right place. The arc rule's wait limit already keeps a job that
    fits only a later window off an earlier window's chains, so the split loses no
    pairing the joint model could make across the off-shift gap. Each window gets an even
    share of what is left of the deadline; component rows carry ``window``.
    """
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS} (got {backend!r})")
    later = sorted({int(t.get("window", 0)) for t in inputs["trucks"] if t.get("of")})
    if not later:
        return _solve_once(inputs, backend=backend, time_limit_s=time_limit_s,
                           deadline_epoch=deadline_epoch, accept_poor=accept_poor)
    return _solve_by_window(inputs, [0] + later, backend=backend, time_limit_s=time_limit_s,
                            deadline_epoch=deadline_epoch, accept_poor=accept_poor)


def _window_starts(inputs: Mapping[str, Any], k: int, chains: Mapping[str, list],
                   jobs_by_id: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """The starts of window ``k``: the trucks' own (``k == 0``), else every ``T@wk`` moved to
    the end of ``T``'s chain so far (unchanged when ``T`` has none yet)."""
    if k == 0:
        return [t for t in inputs["trucks"] if not t.get("of")]
    out = []
    for t in inputs["trucks"]:
        if not t.get("of") or int(t.get("window", 0)) != k:
            continue
        t = dict(t)
        chain = chains.get(t["of"]) or ()
        if chain:
            last = jobs_by_id[chain[-1]]
            t.update(loc=[float(last["dropoff"][0]), float(last["dropoff"][1])],
                     ready_s=max(float(t["ready_s"]), float(last["e"])), anchor="chain", anchor_job=last["id"])
        out.append(t)
    return out


def _solve_by_window(inputs: Mapping[str, Any], windows: list[int], *, backend: str,
                     time_limit_s: float | None, deadline_epoch: float | None,
                     accept_poor: bool) -> dict[str, Any]:
    """:func:`solve_inputs` for an instance with later-window starts (V-m3)."""
    t_all = time.time()
    jobs_by_id = {j["id"]: j for j in inputs["jobs"]}
    remaining = list(inputs["jobs"])
    chains: dict[str, list] = {}
    first_leg_m: dict[str, float] = {}
    rows: list = []
    sums = {"build_ms": 0.0, "solve_ms": 0.0, "arcs": 0, "start_arcs": 0, "empty_m": 0.0}
    deterministic = True
    survival: dict[str, int] | None = None
    for i, k in enumerate(windows):
        starts = _window_starts(inputs, k, chains, jobs_by_id)
        deadline = None
        if deadline_epoch is not None:
            deadline = time.time() + (float(deadline_epoch) - time.time()) / (len(windows) - i)
        out = _solve_once(dict(inputs, trucks=starts, jobs=remaining), backend=backend,
                          time_limit_s=time_limit_s, deadline_epoch=deadline, accept_poor=accept_poor)
        st = out.get("stats") or {}
        for r in st.get("components") or ():
            rows.append(dict(r, window=k))
        for key in sums:
            sums[key] += st.get(key, 0) or 0
        deterministic = deterministic and bool(st.get("deterministic", True))
        if st.get("prev_arcs"):
            survival = survival or {}
            for key, v in st["prev_arcs"].items():
                survival[key] = survival.get(key, 0) + int(v)
        if not out.get("ok"):
            return {"ok": False, "reason": out.get("reason"),
                    "stats": _stats(rows, sums["build_ms"], sums["solve_ms"], sums["arcs"], sums["start_arcs"],
                                    deterministic, sums["empty_m"], first_leg_m, t_all, survival)}
        for tid, chain in out["itineraries"].items():  # keyed by the REAL truck (_merge_windows)
            chains.setdefault(tid, []).extend(chain)
        for tid, m in (st.get("first_leg_m") or {}).items():
            first_leg_m.setdefault(tid, float(m))
        planned = {j for c in out["itineraries"].values() for j in c}
        remaining = [j for j in remaining if j["id"] not in planned]
    planned = {j for c in chains.values() for j in c}
    unplanned = sorted(j["id"] for j in inputs["jobs"] if j["id"] not in planned)
    return {"ok": True, "itineraries": dict(sorted((t, c) for t, c in chains.items() if c)), "unplanned": unplanned,
            "stats": _stats(rows, sums["build_ms"], sums["solve_ms"], sums["arcs"], sums["start_arcs"],
                            deterministic, sums["empty_m"], first_leg_m, t_all, survival)}


def _solve_once(inputs: Mapping[str, Any], *, backend: str = "auto",
                time_limit_s: float | None = None, deadline_epoch: float | None = None,
                accept_poor: bool = False) -> dict[str, Any]:
    """One solve of every cooperation component (no window split; see :func:`solve_inputs`).

    ``time_limit_s`` caps each MIP component; ``deadline_epoch`` (``time.time()`` value) is
    ONE deadline for the whole solve (M2): each MIP component gets at most an even share of
    what is left once a greedy reserve for the remaining components is kept, a component
    whose share is under :data:`MIN_MIP_S` goes greedy (``fallback: deadline``), so the
    answer is written before the deadline. ``accept_poor``: a greedy fallback under the
    guard threshold is KEPT (``accepted_poor``) instead of refusing the solve (the caller
    has no plan to keep).
    """
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS} (got {backend!r})")
    p = inputs["params"]
    t_all = time.time()
    deadline = None if deadline_epoch is None else float(deadline_epoch) - EXIT_MARGIN_S
    of_map = {t["id"]: t["of"] for t in inputs["trucks"] if t.get("of")}
    if of_map:
        inputs = dict(inputs, _of=of_map)
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
    comps = [sorted(c) for c in comps]

    def comp_jobs(comp):
        return sorted((j for c in comp for j in jobs_by_class.get(c, ())), key=lambda x: x["id"])

    def comp_trucks(comp):
        return sorted((t for c in comp for t in trucks_by_class.get(c, ())), key=lambda x: x["id"])

    # the path of every component, decided up front (the deadline split needs the MIP count)
    plan = []
    for comp in comps:
        jobs, trucks = comp_jobs(comp), comp_trucks(comp)
        if backend == "greedy":
            path, why = "greedy", "backend greedy"
        else:
            coop_c = {(a, b) for a, b in inputs["cooperation"] if a in set(comp) and b in set(comp)}
            ok, why = mcf_applicable(comp, coop_c, _class_attrs(inputs, comp, trucks))
            path = "mincostflow" if ok and backend != "mip" else "mip"
            if backend == "mip":
                why = "backend mip"
        plan.append((comp, jobs, trucks, path, why))

    itineraries: dict[str, list] = {}
    rows = []
    build_ms = solve_ms = 0.0
    n_arcs = n_start = 0
    deterministic = True
    empty_m = 0.0
    first_leg_m: dict[str, float] = {}
    survival = {"prev_job_arcs": 0, "prev_job_arcs_feasible": 0, "prev_start_arcs": 0,
                "prev_start_arcs_feasible": 0, "prev_arcs_kept": 0}
    for idx, (comp, jobs, trucks, path, why) in enumerate(plan):
        row: dict[str, Any] = {"classes": comp, "orders": len(jobs), "trucks": len(trucks)}
        t0 = time.time()
        caps = ((p.get("k_succ"), p.get("k_start")) if path != "mip"
                else (p.get("mip_k_succ"), p.get("mip_k_start")))
        starts, members = _merge_trucks(trucks)
        arcs = _component_arcs(inputs, jobs, starts, k_succ=caps[0], k_start=caps[1])
        params = _component_params(dict(inputs, _members=members), comp, jobs, starts, arcs)
        row["starts"] = len(starts)
        b_ms = (time.time() - t0) * 1000.0
        build_ms += b_ms
        n_arcs += int(arcs["src"].size)
        n_start += int(arcs["ssrc"].size)
        row.update(arcs=int(arcs["src"].size), start_arcs=int(arcs["ssrc"].size), build_ms=round(b_ms, 1))
        if inputs.get("prev_arcs"):
            surv = _prev_arc_survival(dict(inputs, _members=members), jobs, starts, arcs)
            row.update(surv)
            for k, v in surv.items():
                survival[k] += v
        t1 = time.time()
        chains = None
        fallback = None
        if path in ("mincostflow", "mip") and jobs:
            if path == "mip":
                limit = (float(time_limit_s) if time_limit_s is not None else float(p["mip_time_limit_s"]))
                try:
                    row["mip_arcs"] = check_mip_arc_budget(params, int(p["mip_arc_budget"]))
                except ArcBudgetExceeded as exc:
                    fallback, row["mip_error"] = "arc_budget", str(exc)
                if fallback is None and deadline is not None:
                    rest = plan[idx + 1:]
                    reserve = GREEDY_FIXED_S + GREEDY_S_PER_JOB * (len(jobs) + sum(len(r[1]) for r in rest))
                    n_mip = 1 + sum(1 for r in rest if r[3] == "mip" and r[1])
                    share = (deadline - time.time() - reserve) / n_mip
                    share -= MIP_BUILD_S_PER_ARC * row["mip_arcs"]
                    row["mip_budget_s"] = round(share, 2)
                    if share < MIN_MIP_S:
                        fallback = "deadline"
                    else:
                        limit = min(limit, share)
                params["time_limit_s"] = limit
                row["mip_time_limit_s"] = round(limit, 2)
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
                    order = [j["id"] for j in sorted(jobs, key=lambda x: (x["r"], x["id"]))]
                    ch = extract_chains(result, params["start_nodes"], job_order=order)
                    if ch.orphaned or ch.depot:
                        raise RuntimeError(f"component {comp}: chains not rooted at trucks "
                                           f"(orphaned {ch.orphaned[:5]}, depot {list(ch.depot)[:5]})")
                    prev_head = {}
                    for x, y in inputs.get("prev_arcs", ()):
                        prev_head.setdefault(y, set()).add(x)
                    chains = {}
                    for sid, lst in ch.chains_by_start.items():
                        free = list(members[sid])
                        rest_chains = []
                        for chain in lst:  # a chain keeps the member that headed it last version
                            mine = [t for t in free if _owner(inputs, t) in prev_head.get(chain[0], ())]
                            if mine:
                                chains[mine[0]] = chain
                                free.remove(mine[0])
                            else:
                                rest_chains.append(chain)
                        for tid, chain in zip(free, rest_chains + [[]] * (len(free) - len(rest_chains))):
                            chains[tid] = chain
                    if path == "mip" and result.get("status") != bk.OPTIMAL:
                        deterministic = False
        if chains is None:
            if fallback is not None:
                row["fallback"] = fallback
                path = "greedy"
            # greedy works on UNIT trucks and lists (one chain per truck): rebuild the arcs
            # once without merging and use them both for greedy and for the km below
            starts, members = trucks, {t["id"]: [t["id"]] for t in trucks}
            arcs = _component_arcs(inputs, jobs, trucks, k_succ=caps[0], k_start=caps[1])
            uparams = _standalone("limits").arrays_to_lists(
                _component_params(inputs, comp, jobs, trucks, arcs))
            chains = _greedy(uparams, jobs) if jobs else {t["id"]: [] for t in trucks}
            if fallback is not None and backend != "greedy":
                served = sum(len(c) for c in chains.values())
                ref = bk.served_reference(params)
                g = bk.quality_guard(served, ref, float(p["guard_threshold"]))
                row["fallback_guard"] = g
                if not g["ok"]:
                    if accept_poor:
                        row["accepted_poor"] = True
                    else:
                        s_ms = (time.time() - t1) * 1000.0
                        row.update(backend=path, reason=why, solve_ms=round(s_ms, 1))
                        rows.append(row)
                        return {"ok": False, "reason": bk.POOR_INCUMBENT,
                                "stats": _stats(rows, build_ms, solve_ms + s_ms, n_arcs, n_start,
                                                deterministic, 0.0, {}, t_all, survival)}
        s_ms = (time.time() - t1) * 1000.0
        solve_ms += s_ms
        row.update(backend=path, reason=why, solve_ms=round(s_ms, 1),
                   planned=sum(len(c) for c in chains.values()))
        first, km = _true_km(chains, arcs, jobs, starts, members)
        empty_m += km
        first_leg_m.update(first)
        row["empty_m"] = round(km, 1)
        if inputs.get("prev_arcs"):
            row["prev_arcs_kept"] = _prev_arcs_kept(inputs, chains)
            survival["prev_arcs_kept"] += row["prev_arcs_kept"]
        rows.append(row)
        for sid, chain in chains.items():
            if chain:
                itineraries[sid] = list(chain)

    itineraries, first_leg_m = _merge_windows(inputs, itineraries, first_leg_m)
    planned = {j for c in itineraries.values() for j in c}
    unplanned = sorted(j["id"] for j in inputs["jobs"] if j["id"] not in planned)
    return {"ok": True, "itineraries": dict(sorted(itineraries.items())), "unplanned": unplanned,
            "stats": _stats(rows, build_ms, solve_ms, n_arcs, n_start, deterministic, empty_m,
                            first_leg_m, t_all, survival if inputs.get("prev_arcs") else None)}


def _merge_windows(inputs, itineraries: Mapping[str, list], first_leg_m: Mapping[str, float]
                   ) -> tuple[dict[str, list], dict[str, float]]:
    """A later-window start's chain (``T@wk``) goes after truck ``T``'s own, in window order;
    the truck's first leg is the first leg of its first non-empty chain."""
    of = inputs.get("_of") or {}
    if not of:
        return dict(itineraries), dict(first_leg_m)
    win = {t["id"]: int(t.get("window", 0)) for t in inputs["trucks"]}
    parts: dict[str, list] = {}
    for sid in sorted(itineraries, key=lambda x: (of.get(x, x), win.get(x, 0))):
        parts.setdefault(of.get(sid, sid), []).append(sid)
    out, first = {}, {}
    for tid, sids in parts.items():
        out[tid] = [j for sid in sids for j in itineraries[sid]]
        lead = next((sid for sid in sids if sid in first_leg_m), None)
        if lead is not None:
            first[tid] = first_leg_m[lead]
    return out, first


def _stats(rows, build_ms, solve_ms, n_arcs, n_start, deterministic, empty_m, first_leg_m, t_all,
           survival=None):
    backends: dict[str, int] = {}
    fallbacks: dict[str, int] = {}
    for r in rows:
        backends[r.get("backend", "?")] = backends.get(r.get("backend", "?"), 0) + 1
        if r.get("fallback"):
            fallbacks[r["fallback"]] = fallbacks.get(r["fallback"], 0) + 1
    out = {
        "components": rows, "backends": dict(sorted(backends.items())),
        "status": ("degraded" if any(r.get("fallback") for r in rows) else "ok"),
        "build_ms": round(build_ms, 1), "solve_ms": round(solve_ms, 1),
        "child_ms": round((time.time() - t_all) * 1000.0, 1),
        "arcs": n_arcs, "start_arcs": n_start, "deterministic": deterministic,
        "empty_m": round(empty_m, 1),
        "first_leg_m": {k: round(v, 1) for k, v in sorted(first_leg_m.items())},
        "maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    if fallbacks:
        out["fallbacks"] = dict(sorted(fallbacks.items()))
    if any(r.get("accepted_poor") for r in rows):
        out["accepted_poor"] = True
    if survival is not None:
        out["prev_arcs"] = dict(survival)
    return out


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = sys.stderr  # nothing but the answer reaches stdout
    try:
        req = json.load(sys.stdin)
        out = solve_inputs(req["inputs"], backend=req.get("backend", "auto"),
                           time_limit_s=req.get("time_limit_s"), deadline_epoch=req.get("deadline_epoch"),
                           accept_poor=bool(req.get("accept_poor", False)))
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
