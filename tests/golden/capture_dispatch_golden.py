"""Capture the pre-policy-slots dispatch golden fixture (plan Step G0).

Responsibility: compile two shipped scenarios, derive deterministic tick states
from them, run a grid of assignment configurations through the REAL
``AssignmentApp`` (via ``tests/golden/harness.py``) and write every observable
output to ``tests/golden/dispatch_golden_v1.json.gz``, byte-stably.

Run from the repo root on an UNMODIFIED production tree::

    python -m tests.golden.capture_dispatch_golden

The script refuses to run when tracked files under ``apps/`` differ from HEAD,
because the fixture is only meaningful as a record of the code at the recorded
commit. It imports only pre-change public APIs (``compile_spec_to_bundle``,
``datagen.hauliers`` through the compiled profile, and the harness).

Tick states use a private ``random.Random(state_seed)``; the module RNG is left
to the harness, which reseeds it per case. Teeth checks (plan Step G0 item 5)
fail the capture rather than write a fixture that cannot detect a regression.
"""

from __future__ import annotations

import copy
import gzip
import json
import os
import random
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = Path(__file__).resolve().parent / "dispatch_golden_v1.json.gz"
FORMAT_VERSION = 1
GENERATOR_COMMAND = "python -m tests.golden.capture_dispatch_golden"
DOMAIN = "container-logistics-sim"

COLLAB_SLUG = "collab_scale_500t_v2"
MULTI_SLUG = "multi_haulier_greedy_solver_500_trucks"

# (state id, world, order count, state seed). Dense = more orders than free
# trucks (orders compete); sparse = fewer (trucks compete).
TICK_STATE_PLAN = [
    ("A", COLLAB_SLUG, 500, 101),
    ("B", COLLAB_SLUG, 120, 202),
    ("C", MULTI_SLUG, 400, 303),
]

STRATEGIES = ("GreedyNearest", "RandomAssignment")
TOPOLOGIES = ("partitioned", "pooled")
COLLAB_STRUCTURES = ("chain", "no-coop")
SPATIAL = (True, False)
CAPS = (500, 40)
SAMPLE_CAP = 40

__all__ = ["build_fixture", "encode_fixture", "main"]


# --- worlds -------------------------------------------------------------------

def _compile_world(slug: str) -> dict[str, Any]:
    """Compile ``scenarios/<slug>/spec.json`` into a throwaway dir; return the bundle."""
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    spec = json.loads((REPO_ROOT / "scenarios" / slug / "spec.json").read_text())
    return compile_spec_to_bundle(
        spec, domain=DOMAIN, scenario_dir=tempfile.mkdtemp(prefix="golden_"), slug=spec["slug"]
    )


def _assignment_profile(bundle: dict[str, Any]) -> dict[str, Any]:
    behaviors = bundle["agents"]["assignment"]
    assert len(behaviors) == 1, f"expected one assignment agent, got {sorted(behaviors)}"
    return copy.deepcopy(next(iter(behaviors.values()))["profile"])


# --- tick states --------------------------------------------------------------

def _order_doc(oid: str, beh: dict[str, Any]) -> dict[str, Any]:
    prof = beh["profile"]
    return {
        "_id": oid,
        "state": "unassigned",
        "haulier_id": beh["haulier_id"],
        "pickup_loc": beh["pickup_loc"],
        "dropoff_loc": beh["dropoff_loc"],
        "order_size": beh["order_size"],
        "profile": {
            "haulier_id": prof["haulier_id"],
            "pickup_facility_name": prof["pickup_facility_name"],
            "dropoff_facility_name": prof["dropoff_facility_name"],
            "pickup_loc": prof["pickup_loc"],
            "dropoff_loc": prof["dropoff_loc"],
            "order_size": beh["order_size"],
        },
    }


def _tick_state(bundle: dict[str, Any], n_orders: int, seed: int) -> dict[str, Any]:
    """One synthetic tick over a compiled world, drawn from ``random.Random(seed)``."""
    rng = random.Random(seed)
    agents = bundle["agents"]

    order_items = sorted(agents["order"].items(), key=lambda kv: (kv[1]["request_time_step"], kv[0]))
    start = rng.randrange(0, len(order_items) - n_orders)
    orders = [_order_doc(oid, beh) for oid, beh in order_items[start:start + n_orders]]

    facility_points = [
        beh["profile"]["location"]
        for _fid, beh in sorted(agents["facility"].items())
        if isinstance(beh.get("profile", {}).get("location"), dict)
    ]

    trucks: list[dict[str, Any]] = []
    for tid, beh in sorted(agents["truck"].items()):
        prof = beh["profile"]
        current_loc = beh.get("init_loc") if rng.random() < 0.7 else rng.choice(facility_points)
        tprof: dict[str, Any] = {
            "haulier_id": prof["haulier_id"],
            "truck_size": prof["truck_size"],
            "restricted_areas": list(prof.get("restricted_areas") or []),
            "home_facility_name": prof.get("home_facility_name"),
            "current_loc": copy.deepcopy(current_loc),
        }
        if rng.random() < 0.3:
            # The truck just dropped off at some order's pickup facility and is still
            # there: the keys constraints._truck_last_dropoff_{facility,loc} read, plus
            # current_loc at the same point, so assignment_cost clamps to exactly 0.0
            # and the solvers' tie-breaking is genuinely exercised.
            src = rng.choice(orders)
            tprof["last_dropoff_facility_name"] = src["profile"]["pickup_facility_name"]
            tprof["last_dropoff_loc"] = copy.deepcopy(src["profile"]["pickup_loc"])
            tprof["current_loc"] = copy.deepcopy(src["profile"]["pickup_loc"])
        trucks.append({
            "_id": tid,
            "state": "offline" if rng.random() < 0.1 else "online",
            "haulier_id": prof["haulier_id"],
            "profile": tprof,
        })

    busy_ids = sorted(rng.sample([t["_id"] for t in trucks], len(trucks) // 10))
    claimed = sorted(rng.sample([o["_id"] for o in orders], max(1, len(orders) * 2 // 100)))
    return {
        "trucks": trucks,
        "orders": orders,
        "busy_ids": busy_ids,
        "claimed_order_ids": claimed,
        "sim_clock": bundle["settings"]["REFERENCE_TIME"],
    }


# Copied (not imported) from tests/test_assignment_pooled_determinism.py::_tied_fleet
# and tests/test_assignment_cooperation.py::_truck/_order, so later edits to those
# tests cannot change the golden world.
_TIED_LON, _TIED_LAT = 103.85, 1.30


def _pt(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def _tied_truck(tid: str, haulier: str) -> dict[str, Any]:
    return {
        "_id": tid,
        "state": "online",
        "haulier_id": haulier,
        "profile": {
            "haulier_id": haulier,
            "current_loc": _pt(_TIED_LON, _TIED_LAT),
            "truck_size": "40ft",
            "last_dropoff_loc": _pt(_TIED_LON, _TIED_LAT),
        },
    }


def _tied_order(oid: str, haulier: str) -> dict[str, Any]:
    return {
        "_id": oid,
        "state": "unassigned",
        "haulier_id": haulier,
        "pickup_loc": _pt(_TIED_LON, _TIED_LAT),
        "dropoff_loc": _pt(_TIED_LON + 0.05, _TIED_LAT),
        "profile": {"haulier_id": haulier},
    }


def _tied_state(sim_clock: str) -> dict[str, Any]:
    trucks = [
        _tied_truck("t_acme1", "acme"), _tied_truck("t_acme2", "acme"),
        _tied_truck("t_borax1", "borax"), _tied_truck("t_borax2", "borax"),
    ]
    orders = [
        _tied_order("o_acme1", "acme"), _tied_order("o_acme2", "acme"),
        _tied_order("o_borax1", "borax"), _tied_order("o_borax2", "borax"),
    ]
    return {"trucks": trucks, "orders": orders, "busy_ids": [], "claimed_order_ids": [],
            "sim_clock": sim_clock}


def _assert_unique_ids(state_id: str, state: dict[str, Any]) -> None:
    for kind in ("trucks", "orders"):
        counts = Counter(d["_id"] for d in state[kind])
        dups = sorted(k for k, n in counts.items() if n > 1)
        assert not dups, f"tick state {state_id}: duplicate {kind} ids {dups[:5]}"


# --- case grid ----------------------------------------------------------------

def _case_profile(base: dict[str, Any], *, strategy: str, topology: str,
                  structure: str | None, spatial: bool, cap: int) -> dict[str, Any]:
    """The compiled profile with exactly the grid's axes overwritten."""
    prof = copy.deepcopy(base)
    prof["strategy"] = strategy
    prof["use_spatial_matching"] = spatial
    prof["max_trucks_per_haulier"] = cap
    planner = prof["planner"]
    planner["topology"] = topology
    # Mirror the strategy into the third legacy dial the way preprocess does, so
    # the stored profile is one a real compile could have produced.
    planner["deployment"] = {"type": strategy, "params": copy.deepcopy(prof.get("solver_params") or {})}
    if structure is not None:
        ids = {s["id"] for s in prof["cooperation"]["structures"]}
        assert structure in ids, f"structure {structure!r} not in compiled profile {sorted(ids)}"
        prof["cooperation"]["active"] = structure
    return prof


def _grid(state_id: str, structures: tuple[str | None, ...], *, full: bool):
    """Yield ``(strategy, topology, structure, spatial, cap)`` for one tick state.

    Random has no ``solve_pairs`` today, so ``use_spatial_matching`` is fixed True
    for it (the flag is inert there). ``full=False`` (tiny tied world) drops the
    spatial/cap axes, which cannot bind on 4 trucks.
    """
    for strategy in STRATEGIES:
        for topology in TOPOLOGIES:
            for structure in structures:
                spatials = SPATIAL if (full and strategy == "GreedyNearest") else (True,)
                caps = CAPS if full else (500,)
                for spatial in spatials:
                    for cap in caps:
                        yield strategy, topology, structure, spatial, cap


def _case_id(state_id, strategy, topology, structure, spatial, cap) -> str:
    strat = {"GreedyNearest": "greedy", "RandomAssignment": "random"}[strategy]
    parts = [state_id, strat, topology, structure or "nocoop-only",
             "spatial" if spatial else "sampled", f"cap{cap}"]
    return "-".join(parts)


def _canonical(obj: Any) -> Any:
    """``obj`` as the fixture will store and replay it (sorted keys, JSON types)."""
    return json.loads(json.dumps(obj, sort_keys=True))


def build_fixture() -> dict[str, Any]:
    """Compile worlds, build tick states and cases, run every case; return the fixture."""
    from tests.golden.harness import MODULE_SEED, RUN_ID, TIME_STEP, run_case

    bundles = {slug: _compile_world(slug) for slug in (COLLAB_SLUG, MULTI_SLUG)}
    base_profiles = {slug: _assignment_profile(b) for slug, b in bundles.items()}

    tick_states: dict[str, dict[str, Any]] = {}
    state_world: dict[str, str] = {}
    for state_id, slug, n_orders, seed in TICK_STATE_PLAN:
        tick_states[state_id] = _tick_state(bundles[slug], n_orders, seed)
        state_world[state_id] = slug
    tick_states["T"] = _tied_state(bundles[COLLAB_SLUG]["settings"]["REFERENCE_TIME"])
    state_world["T"] = COLLAB_SLUG
    for state_id, state in tick_states.items():
        _assert_unique_ids(state_id, state)
    # Canonical (sorted-key) round trip: the fixture is written with sort_keys, so
    # the replay feeds sorted-key dicts, and key order is visible in the published
    # json.dumps strings. Capture must feed the app exactly what replay will.
    tick_states = _canonical(tick_states)

    cases: list[dict[str, Any]] = []
    for state_id in ("A", "B", "C", "T"):
        slug = state_world[state_id]
        structures = COLLAB_STRUCTURES if slug == COLLAB_SLUG else (None,)
        for strategy, topology, structure, spatial, cap in _grid(
            state_id, structures, full=state_id != "T"
        ):
            case = {
                "case_id": _case_id(state_id, strategy, topology, structure, spatial, cap),
                "tick_state": state_id,
                "strategy": strategy,
                "axes": {"topology": topology, "structure": structure or "no-coop",
                         "use_spatial_matching": spatial, "max_trucks_per_haulier": cap},
                "profile": _case_profile(base_profiles[slug], strategy=strategy,
                                         topology=topology, structure=structure,
                                         spatial=spatial, cap=cap),
            }
            case = _canonical(case)
            case["expected"] = json.loads(json.dumps(run_case(case, tick_states)))
            cases.append(case)

    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"

    fixture = {
        "header": {
            "format_version": FORMAT_VERSION,
            "capture_commit": _git_sha(),
            "generator_command": GENERATOR_COMMAND,
            "worlds": {"A": COLLAB_SLUG, "B": COLLAB_SLUG, "C": MULTI_SLUG,
                       "T": "tied (copy of tests/test_assignment_pooled_determinism._tied_fleet)"},
            "constants": {"module_seed": MODULE_SEED, "time_step": TIME_STEP, "run_id": RUN_ID},
            "case_count": len(cases),
            "coverage_matrix": coverage_matrix(cases),
            "coverage_gaps_by_design": [
                "RandomAssignment x use_spatial_matching=false: Random has no solve_pairs "
                "at capture, so the flag is inert for it (always the sampling path)",
                "C x chain: the multi_haulier world compiles only the no-coop structure",
                "T x {sampled, cap40}: 4 trucks, the cap cannot bind",
            ],
        },
        "tick_states": tick_states,
        "cases": cases,
    }
    teeth = check_teeth(fixture)
    fixture["header"]["teeth"] = teeth
    return fixture


# --- coverage + teeth (shared with the replay test) ---------------------------

def coverage_matrix(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Count cases per pair of interacting axes, so a gap is visible in the header."""
    def axis_values(c):
        return {
            "tick_state": c["tick_state"],
            "strategy": c["strategy"],
            "topology": c["axes"]["topology"],
            "structure": c["axes"]["structure"],
            "use_spatial_matching": str(c["axes"]["use_spatial_matching"]).lower(),
            "max_trucks_per_haulier": str(c["axes"]["max_trucks_per_haulier"]),
        }

    names = ["tick_state", "strategy", "topology", "structure",
             "use_spatial_matching", "max_trucks_per_haulier"]
    matrix: dict[str, dict[str, int]] = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            cell: Counter = Counter()
            for c in cases:
                v = axis_values(c)
                cell[f"{v[a]}|{v[b]}"] += 1
            matrix[f"{a} x {b}"] = dict(sorted(cell.items()))
    return matrix


def _assignable_by_haulier(state: dict[str, Any]) -> Counter:
    busy = set(state["busy_ids"])
    return Counter(
        t["haulier_id"] for t in state["trucks"]
        if t["state"] == "online" and t["_id"] not in busy
    )


def check_teeth(fixture: dict[str, Any]) -> dict[str, Any]:
    """Assert the fixture can detect regressions (plan Step G0 item 5); return the counts.

    Raises ``AssertionError`` naming the failed property.
    """
    from apps.container_logistics.assignment import constraints

    cases = fixture["cases"]
    states = fixture["tick_states"]

    with_pairs = sum(1 for c in cases if c["expected"]["pairs"])
    assert with_pairs >= 0.9 * len(cases), f"only {with_pairs}/{len(cases)} cases have pairs"

    chain_with_tags = sum(
        1 for c in cases if c["axes"]["structure"] == "chain" and c["expected"]["share_tags"]
    )
    assert chain_with_tags > 0, "no chain case produced a cross-haulier share tag"
    nocoop_with_tags = sum(
        1 for c in cases if c["axes"]["structure"] == "no-coop" and c["expected"]["share_tags"]
    )
    assert nocoop_with_tags == 0, f"{nocoop_with_tags} no-coop cases produced share tags"

    # The cap can only bind on the sampling path (spatial off, or Random which has no
    # solve_pairs) and only when some haulier has more assignable trucks than the cap.
    sampling = [
        c for c in cases
        if c["axes"]["max_trucks_per_haulier"] == SAMPLE_CAP
        and (c["strategy"] == "RandomAssignment" or not c["axes"]["use_spatial_matching"])
        and max(_assignable_by_haulier(states[c["tick_state"]]).values()) > SAMPLE_CAP
    ]
    assert sampling, "no case actually samples (cap 40 never binds)"

    # ... and binding must change the outcome somewhere, or the axis is decorative.
    by_key = {(c["tick_state"], c["strategy"], c["axes"]["topology"], c["axes"]["structure"],
               c["axes"]["use_spatial_matching"], c["axes"]["max_trucks_per_haulier"]):
              c["expected"]["pairs"] for c in cases}
    cap_changes = sum(
        1 for c in sampling
        if by_key.get((c["tick_state"], c["strategy"], c["axes"]["topology"],
                       c["axes"]["structure"], c["axes"]["use_spatial_matching"], 500))
        != c["expected"]["pairs"]
    )
    assert cap_changes > 0, "cap 40 never changed the pairs versus cap 500"

    tied_costs: set[float] = set()
    tied_cases = [c for c in cases if c["tick_state"] == "T"]
    for c in tied_cases:
        trucks = {t["_id"]: t for t in states["T"]["trucks"]}
        orders = {o["_id"]: o for o in states["T"]["orders"]}
        params = c["profile"].get("solver_params") or {}
        for tid, oid in c["expected"]["pairs"]:
            tied_costs.add(constraints.assignment_cost(trucks[tid], orders[oid], params))
    assert tied_cases and tied_costs == {0.0}, f"tick T costs are not all 0.0: {sorted(tied_costs)}"

    zero_cost_pairs = 0
    for c in cases:
        if c["tick_state"] == "T":
            continue
        trucks = {t["_id"]: t for t in states[c["tick_state"]]["trucks"]}
        orders = {o["_id"]: o for o in states[c["tick_state"]]["orders"]}
        params = c["profile"].get("solver_params") or {}
        zero_cost_pairs += sum(
            1 for tid, oid in c["expected"]["pairs"]
            if constraints.assignment_cost(trucks[tid], orders[oid], params) == 0.0
        )
    assert zero_cost_pairs > 0, "no zero-cost (dual-cycle tie) pair in the compiled worlds"

    return {
        "cases": len(cases),
        "cases_with_pairs": with_pairs,
        "chain_cases_with_share_tags": chain_with_tags,
        "sampling_cases": len(sampling),
        "sampling_cases_changed_by_cap": cap_changes,
        "tied_cases": len(tied_cases),
        "tied_costs": sorted(tied_costs),
        "zero_cost_pairs_in_compiled_worlds": zero_cost_pairs,
    }


# --- output -------------------------------------------------------------------

def _git_sha() -> str:
    """Short HEAD sha; refuses a dirty tracked ``apps/`` tree (the fixture would lie)."""
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "apps"], cwd=REPO_ROOT)
    if dirty.returncode != 0:
        raise SystemExit("REFUSING TO CAPTURE: tracked files under apps/ differ from HEAD.")
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def encode_fixture(fixture: dict[str, Any]) -> bytes:
    """Canonical bytes: sorted keys, compact separators, gzip with mtime 0."""
    raw = json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import io

    buf = io.BytesIO()
    # filename="" keeps the output path out of the gzip header too.
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    """Write the fixture (or ``--out PATH``); print the teeth summary."""
    args = list(sys.argv[1:] if argv is None else argv)
    out = Path(args[args.index("--out") + 1]) if "--out" in args else FIXTURE_PATH
    fixture = build_fixture()
    out.write_bytes(encode_fixture(fixture))
    print(json.dumps(fixture["header"]["teeth"], sort_keys=True))
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(fixture['cases'])} cases)")
    return 0


if __name__ == "__main__":
    # Isolate compile_spec_to_bundle from the real scenarios tree; must be set before
    # any scenario module is imported (they read it lazily, but be safe).
    os.environ.setdefault("ORSIM_SCENARIOS_DIR", tempfile.mkdtemp(prefix="golden_scen_root_"))
    sys.path.insert(0, str(REPO_ROOT))
    raise SystemExit(main())
