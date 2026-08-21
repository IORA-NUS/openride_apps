"""The solver seam — facility-rebate plan §3.2, §7 (Phase 5).

This phase ships the MECHANISM only: a third opt-in injected callable on
``BaseAssignmentSolver`` (mirroring ``set_rng``/``set_tiebreak``) and the
``AssignmentApp`` wiring that constructs a :class:`RebateBook` and injects it —
but ONLY when the compiled profile carries ``planner.rebate_aware: true``, which
defaults ``false`` and is set by no shipped scenario. No solver reads the
injected book.

Covers:
- the injection contract itself (opt-in, ``None`` until called);
- the book is never constructed (no facility read, no ``set_rebate_book`` call)
  when the flag is false;
- no shipped scenario enables the flag;
- **R-I1b** — adding a real, non-trivial rebate schedule and actually injecting
  it into a rebate-BLIND solver changes no allocation, over several ticks, on a
  fixture engineered to contain genuine cost ties;
- **R-I1c** — the same rebate block changes neither FIFO gate-assignment order
  nor the resolved facility service time.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List

import pytest

from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.assignment.solver import get_solver
from apps.container_logistics.assignment.solver.base import BaseAssignmentSolver
from apps.container_logistics.facility.service_time import resolve_service_time
from apps.container_logistics.rebate import RebateBook
from apps.container_logistics.statemachine import (
    FacilityQueueController,
    FacilityVisitType,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- fixtures


def _pt(lon: float, lat: float) -> Dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def _tied_truck(tid: str, haulier: str, lon: float, lat: float) -> Dict[str, Any]:
    """A truck whose anchor AND last drop-off are both at (lon, lat) — cost 0 to
    any order whose pickup is also there (haversine 0 trivially, no dual-cycle
    approximation needed)."""
    return {
        "_id": tid,
        "state": "online",
        "haulier_id": haulier,
        "profile": {
            "haulier_id": haulier,
            "current_loc": _pt(lon, lat),
            "last_dropoff_loc": _pt(lon, lat),
            "truck_size": "40ft",
        },
    }


def _order(oid: str, haulier: str, lon: float, lat: float,
           pickup_fac: str = None, dropoff_fac: str = None) -> Dict[str, Any]:
    """An order candidate — carrying the facility ids a rebate-aware solver would price.

    R2-8 / review F3: without these the book's facilities are referenced by NOTHING, so
    a facility-keyed rebate resolves ``None`` on every candidate pair and the inertness
    test passes for a reason that has nothing to do with the invariant. The reviewer's
    guarded mutation A walked straight through the old fixture.
    """
    return {
        "_id": oid,
        "state": "unassigned",
        "haulier_id": haulier,
        "pickup_loc": _pt(lon, lat),
        "dropoff_loc": _pt(lon + 0.05, lat),
        "pickup_facility_resource_id": pickup_fac or _FAC_ACME,
        "dropoff_facility_resource_id": dropoff_fac or _FAC_BORAX,
        "profile": {
            "haulier_id": haulier,
            "pickup_facility_resource_id": pickup_fac or _FAC_ACME,
            "dropoff_facility_resource_id": dropoff_fac or _FAC_BORAX,
        },
    }


# One shared point per haulier: 2 trucks x 2 orders = 4 pairs per haulier, and
# EVERY one of those 8 candidate pairs (no cross-haulier edges configured) costs
# exactly 0.0 km — a genuine, exact tie, not a near-tie.
#: Facility ids the book is keyed on AND the orders reference. The linkage between
#: these two is exactly what review F3 found missing.
_FAC_ACME = "fac_acme"
_FAC_BORAX = "fac_borax"

_ACME_PT = (103.85, 1.30)
_BORAX_PT = (103.95, 1.31)


def _tied_fleet():
    trucks = [
        _tied_truck("t_acme1", "acme", *_ACME_PT),
        _tied_truck("t_acme2", "acme", *_ACME_PT),
        _tied_truck("t_borax1", "borax", *_BORAX_PT),
        _tied_truck("t_borax2", "borax", *_BORAX_PT),
    ]
    # Within each haulier the two orders point at DIFFERENT facilities, so a
    # facility-keyed price differs BETWEEN tied candidates. With both orders on the
    # same facility a rebate would shift every candidate by the same constant and
    # could not reorder anything — the test would be insensitive by arithmetic even
    # with the ids wired (R2-8).
    orders = [
        _order("o_acme1", "acme", *_ACME_PT, pickup_fac=_FAC_ACME, dropoff_fac=_FAC_BORAX),
        _order("o_acme2", "acme", *_ACME_PT, pickup_fac=_FAC_BORAX, dropoff_fac=_FAC_ACME),
        _order("o_borax1", "borax", *_BORAX_PT, pickup_fac=_FAC_ACME, dropoff_fac=_FAC_BORAX),
        _order("o_borax2", "borax", *_BORAX_PT, pickup_fac=_FAC_BORAX, dropoff_fac=_FAC_ACME),
    ]
    return trucks, orders


class _StubManager:
    """Mirrors ``tests/test_assignment_pooled.py``'s ``_StubManager``, plus the
    facility-read surface ``_inject_rebate_book`` uses. ``facility_calls`` lets a
    test PROVE the facility endpoint was (or was not) hit."""

    def __init__(self, trucks, orders, *, facility_docs=None, run_id="run_test"):
        self._trucks = trucks
        self._orders = orders
        self._facility_docs = list(facility_docs or [])
        self.run_id = run_id
        self.facility_calls = 0

    def list_trucks(self, projection=None, online_only=True):
        return list(self._trucks)

    def list_unassigned_orders(self, projection=None):
        return list(self._orders)

    def order_ids_with_open_haul(self):
        return set()

    def active_haul_truck_ids(self):
        return []

    def online_state_name(self):
        return "online"

    def _facility_url(self):
        return "http://stub/facility"

    def _paged_where(self, base_url, where_clause, projection=None, *, max_results=None):
        self.facility_calls += 1
        return list(self._facility_docs)


class _StubMessenger:
    def __init__(self):
        self.published: List[Dict[str, Any]] = []
        self.client = self

    def publish(self, topic, message):
        self.published.append({"topic": topic, "payload": json.loads(message)})


def _app(
    trucks,
    orders,
    *,
    rebate_aware: bool = False,
    facility_docs=None,
    run_id: str = "run_test",
    strategy: str = "GreedyNearest",
    topology: str = "pooled",
) -> AssignmentApp:
    """A real ``AssignmentApp`` (no ORSim lifecycle), same pattern as
    ``tests/test_assignment_pooled.py::_app``. ``topology="pooled"`` by default —
    it is the ONLY path that seeds ``set_rng``/``set_tiebreak`` per tick
    (``_assign_pooled``), which is what makes equal-cost ties deterministic
    (plan §7: "the predictable repair is to weaken it into vacuity" otherwise).
    """
    app = AssignmentApp.__new__(AssignmentApp)
    planner: Dict[str, Any] = {"topology": topology, "timing": {"mode": "online"}}
    if rebate_aware:
        planner["rebate_aware"] = True
    profile = {
        "strategy": strategy,
        "respect_truck_online_state": True,
        "reject_if_active_haul_trip": True,
        "use_spatial_matching": True,
        "planner": planner,
    }
    app.behavior = {"profile": profile}
    app.run_id = run_id
    app.manager = _StubManager(trucks, orders, facility_docs=facility_docs, run_id=run_id)
    app.messenger = _StubMessenger()
    app._solver = get_solver(strategy, None)
    app._solver_params = {}
    app._warned_haulier_block = False
    app._share_tags = {}
    return app


def _ordered_ids(matches):
    """Ordered (truck_id, order_id) pairs — NOT sorted, so the test also catches
    an award-ORDER change, not merely an award-SET change (plan §7 wants the
    "ORDERED award list" compared)."""
    return [(t["_id"], o["_id"]) for t, o in matches]


# A non-trivial rebate schedule: signed, every hour listed (dense, no warnings).
_RICH_SCHEDULE = {
    "points": [
        {"hour": h, "amount": (40.0 if h % 3 == 0 else -12.5)} for h in range(24)
    ]
}


#: A SECOND, deliberately different schedule. Two facilities on the same curve would
#: apply a uniform shift to every candidate, which no cost comparison can see (R2-8).
_OTHER_SCHEDULE = {
    "points": [
        {"hour": h, "amount": (-99.0 if h % 2 == 0 else 250.0)} for h in range(24)
    ]
}


def _facility_docs_with_rebate():
    return [
        {"_id": _FAC_ACME, "profile": {"name": "acme_yard", "rebate": _RICH_SCHEDULE}},
        {"_id": _FAC_BORAX, "profile": {"name": "borax_yard", "rebate": _OTHER_SCHEDULE}},
    ]


# --------------------------------------------------------------------------- (a) the injection contract


def test_set_rebate_book_is_opt_in_and_defaults_none():
    """Mirrors ``set_rng``/``set_tiebreak``: a fresh solver's ``rebate_book`` is
    ``None`` until injected, and injecting ``None`` explicitly restores that."""
    solver = get_solver("GreedyNearest", None)
    assert isinstance(solver, BaseAssignmentSolver)
    assert solver.rebate_book is None

    book = RebateBook.from_facility_docs(_facility_docs_with_rebate())
    assert len(book) == 2  # sanity: the fixture book is not accidentally empty
    solver.set_rebate_book(book)
    assert solver.rebate_book is book

    solver.set_rebate_book(None)
    assert solver.rebate_book is None


# --------------------------------------------------------------------------- (b) construction gated on the flag


def test_book_is_not_constructed_when_rebate_aware_is_false():
    """``planner.rebate_aware`` absent (the shipped default) => no facility read,
    no book, ``set_rebate_book`` never called — checked both via the private
    helper directly and via a full ``assign()`` tick."""
    trucks, orders = _tied_fleet()
    app = _app(trucks, orders, rebate_aware=False, facility_docs=_facility_docs_with_rebate())

    prof = app._assignment_profile()
    assert app._rebate_aware(prof) is False

    app._inject_rebate_book(prof)
    assert app._solver.rebate_book is None
    assert app.manager.facility_calls == 0  # the facility endpoint was never hit

    # And through the real call path, not just the helper in isolation.
    app.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=0)
    assert app._solver.rebate_book is None
    assert app.manager.facility_calls == 0

    # Explicit false reads the same as absent.
    trucks2, orders2 = _tied_fleet()
    app2 = _app(trucks2, orders2, rebate_aware=False, facility_docs=_facility_docs_with_rebate())
    app2.behavior["profile"]["planner"]["rebate_aware"] = False
    app2.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=0)
    assert app2._solver.rebate_book is None
    assert app2.manager.facility_calls == 0

    # R2-7 / review F1 — the part this test was MISSING, and the reason it was
    # unfalsifiable: it built `app.behavior` by hand, so it asserted a negative about a
    # profile the compiler never produced. `rebate_aware` was in fact stripped by
    # `_normalize_planner`, meaning the flag could not be set from a scenario file AT
    # ALL, and this test passed anyway — it would have passed identically against a
    # build where the seam was permanently dead. The negative must therefore be
    # asserted against a COMPILED profile.
    compiled_off = _compiled_assignment_profile(rebate_aware=None)
    assert AssignmentApp._rebate_aware(compiled_off) is False
    assert compiled_off["planner"]["rebate_aware"] is False, (
        "the compiled profile does not even carry the key — a false-by-absence read "
        "here would be indistinguishable from the key being silently dropped"
    )


def test_rebate_aware_flag_defaults_false_in_every_shipped_scenario():
    """Guards against an unrelated editor save flipping the flag (plan §7)."""
    paths = sorted(glob.glob(os.path.join(_REPO_ROOT, "scenarios", "*", "scenario.json")))
    assert len(paths) >= 1, "expected at least one shipped scenario to scan"

    def _walk_for_enabled_rebate_aware(node, where):
        found = []
        if isinstance(node, dict):
            for k, v in node.items():
                path = f"{where}.{k}"
                if k == "rebate_aware" and bool(v):
                    found.append(path)
                found.extend(_walk_for_enabled_rebate_aware(v, path))
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                found.extend(_walk_for_enabled_rebate_aware(item, f"{where}[{idx}]"))
        return found

    offenders = []
    for path in paths:
        with open(path) as f:
            doc = json.load(f)
        offenders.extend(
            f"{os.path.relpath(path, _REPO_ROOT)}:{loc}"
            for loc in _walk_for_enabled_rebate_aware(doc, "$")
        )
    assert offenders == [], f"rebate_aware enabled in shipped scenario(s): {offenders}"


def _compiled_assignment_profile(rebate_aware=None, topology="pooled"):
    """Compile a real spec and return the assignment agent's EMITTED profile.

    The whole point of R2-7: assertions about this flag must ride the compiler, because
    the compiler is where it was being lost.
    """
    import copy as _copy

    from apps.container_logistics.datagen.preprocess import Preprocessor

    planner = {"topology": topology}
    if rebate_aware is not None:
        planner["rebate_aware"] = rebate_aware
    spec = {
        "name": "seam", "slug": "seam", "domain": "container_logistics",
        "simulationDays": 1, "seed": 7,
        "referenceTime": "2020-01-01 00:00:00",
        "agents": {"truck": {"count": 4}, "order": {"count": 10},
                   "facility": {"count": 8}},
        "planner": planner,
    }
    compiled = Preprocessor.compile(_copy.deepcopy(spec), domain="container_logistics",
                                    reference_time="2020-01-01 00:00:00")
    return compiled.spec.assignment_settings["profile"]


def test_compiled_profile_carries_rebate_aware_true():
    """R2-7 / review F1 — HIGH-1. The flag must SURVIVE the compiler.

    `_normalize_planner` returns a closed dict literal, and `rebate_aware` was not in
    it, so an author who set the flag in `spec.json` got a compiled profile without it
    and the opt-in seam was unreachable from a scenario file. This is the G23
    silent-drop mechanism one level below the place the plan checked for it.
    """
    prof = _compiled_assignment_profile(rebate_aware=True)
    assert prof["planner"]["rebate_aware"] is True
    assert AssignmentApp._rebate_aware(prof) is True


def test_rebate_aware_survives_all_the_way_into_the_generated_agent():
    """Compile is only half the path — the AGENT profile is what the app actually reads."""
    import copy as _copy

    from apps.container_logistics.datagen import ScenarioGenerator
    from apps.container_logistics.datagen.preprocess import Preprocessor

    spec = {
        "name": "seam", "slug": "seam", "domain": "container_logistics",
        "simulationDays": 1, "seed": 7,
        "referenceTime": "2020-01-01 00:00:00",
        "agents": {"truck": {"count": 4}, "order": {"count": 10},
                   "facility": {"count": 8}},
        "planner": {"topology": "pooled", "rebate_aware": True},
    }
    compiled = Preprocessor.compile(_copy.deepcopy(spec), domain="container_logistics",
                                    reference_time="2020-01-01 00:00:00")
    result = ScenarioGenerator(compiled.spec, catalog=Preprocessor.catalog()).generate()
    agent = next(iter(result.assignment.values()))
    assert AssignmentApp._rebate_aware(agent["profile"]) is True


def test_compiled_profile_defaults_rebate_aware_false_and_carries_the_key():
    """Default off, but PRESENT — absence and false must be distinguishable."""
    prof = _compiled_assignment_profile(rebate_aware=None)
    assert prof["planner"]["rebate_aware"] is False
    assert AssignmentApp._rebate_aware(prof) is False


# --------------------------------------------------------------------------- (R-I1b) allocation inertness


def test_rebate_does_not_change_allocations():
    """R-I1b: driving the SAME tied fixture through the pooled planner over
    several ticks, with a REAL, non-trivial rebate book actually constructed and
    injected into the (rebate-blind) solver, produces a byte-identical ORDERED
    award list to the run with no rebate data at all.

    The fixture is engineered so every one of its 8 candidate (truck, order)
    pairs (2 trucks x 2 orders, per haulier, no cross-haulier edges) has EXACTLY
    the same cost — 0.0 km, verified directly below — so the award is decided
    entirely by the seeded tie-break, which is where a non-determinism bug would
    show up as flakiness rather than as a real rebate effect.
    """
    from apps.container_logistics.assignment import constraints as assign_constraints

    trucks, orders = _tied_fleet()
    tie_costs = set()
    pair_count = 0
    for t in trucks:
        for o in orders:
            if assign_constraints.haulier_matches(t, o):
                pair_count += 1
                tie_costs.add(round(assign_constraints.assignment_cost(t, o, {}), 9))
    assert pair_count == 8, f"expected 8 same-haulier candidate pairs, got {pair_count}"
    assert tie_costs == {0.0}, f"fixture is not a genuine tie: costs = {tie_costs}"

    n_ticks = 5
    run_id = "run_ri1b"

    trucks_w, orders_w = _tied_fleet()
    app_w = _app(trucks_w, orders_w, rebate_aware=False, run_id=run_id)

    trucks_ws, orders_ws = _tied_fleet()
    app_ws = _app(
        trucks_ws,
        orders_ws,
        rebate_aware=True,
        facility_docs=_facility_docs_with_rebate(),
        run_id=run_id,
    )

    awards_w: List[List[tuple]] = []
    awards_ws: List[List[tuple]] = []
    for step in range(n_ticks):
        m_w = app_w.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=step)
        m_ws = app_ws.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=step)
        awards_w.append(_ordered_ids(m_w))
        awards_ws.append(_ordered_ids(m_ws))

    # Prove the book was REALLY constructed and REALLY injected in the W+S arm —
    # otherwise this test would pass vacuously (plan §7 M4/M5 anti-vacuity spirit).
    assert app_ws.manager.facility_calls >= 1
    assert app_ws._solver.rebate_book is not None
    assert len(app_ws._solver.rebate_book) == 2
    # And confirm the blind arm never touched a facility document or a book.
    assert app_w.manager.facility_calls == 0
    assert app_w._solver.rebate_book is None

    assert awards_w == awards_ws, (
        f"allocations diverged with a rebate schedule present: {awards_w!r} != {awards_ws!r}"
    )
    # Every tick actually assigned something, so equality isn't vacuous-by-emptiness.
    assert all(len(a) == 4 for a in awards_w)


# --------------------------------------------------------------------------- (R-I1c) queue-behaviour inertness


def test_rebate_does_not_change_queue_behaviour():
    """R-I1c: a facility profile carrying a rebate block changes neither the FIFO
    gate-assignment order nor the resolved service time — driven through the
    REAL ``FacilityQueueController`` (see tests/test_facility_queue_controller.py)
    and the REAL ``resolve_service_time``."""
    base_profile = {"gate_count": 2, "service_time": 600}
    rebate_profile = {
        "gate_count": 2,
        "service_time": 600,
        "rebate": _RICH_SCHEDULE,
    }

    # Sanity: the rebate block is real and non-trivial (not an empty/no-op block).
    book = RebateBook.from_facility_docs([{"_id": "fac1", "profile": rebate_profile}])
    assert len(book) == 1
    assert book.price_at("fac1", "Wed, 01 Jan 2020 09:00:00 GMT") == 40.0

    truck_ids = [f"truck_{i}" for i in range(6)]
    visit_types = [
        FacilityVisitType.PICKUP,
        FacilityVisitType.DROPOFF,
        FacilityVisitType.PICKUP,
        FacilityVisitType.PICKUP,
        FacilityVisitType.DROPOFF,
        FacilityVisitType.DROPOFF,
    ]

    def _drive(profile):
        service_time = resolve_service_time(profile, profile)
        controller = FacilityQueueController(gate_count=int(profile["gate_count"]))
        controller.open_facility()
        for tid, vt in zip(truck_ids, visit_types):
            controller.enqueue_truck(tid, visit_type=vt)

        served_order = []
        completion_times = []
        clock = 0
        while controller.queue or any(v is not None for v in controller.gate_assignments.values()):
            assignments = controller.assign_available_gates()
            clock += service_time
            for gate_idx in sorted(assignments):
                entry = assignments[gate_idx]
                served_order.append((entry.truck_id, entry.visit_type))
                completion_times.append(clock)
            for gate_idx in sorted(assignments):
                controller.release_gate(gate_idx)
        return served_order, completion_times, service_time

    served_base, times_base, st_base = _drive(base_profile)
    served_rebate, times_rebate, st_rebate = _drive(rebate_profile)

    assert len(served_base) == 6
    assert st_base == 600
    assert st_base == st_rebate, f"service time changed: {st_base} != {st_rebate}"
    assert served_base == served_rebate, (
        f"gate-assignment order changed: {served_base!r} != {served_rebate!r}"
    )
    assert times_base == times_rebate, (
        f"gate completion times changed: {times_base!r} != {times_rebate!r}"
    )


def test_the_opt_in_book_is_fetched_once_not_once_per_tick():
    """The opt-in seam must not reintroduce a per-tick HTTP round trip.

    ``profile.rebate`` is COMPILED data, stamped at generation and never mutated
    during a run, so the book is built once per app. Without that cache, opting in
    pages the whole facility collection on every assignment tick — 300 documents
    times ~2500 ticks — which is exactly the per-lookup HTTP cost the plan cited when
    it rejected a truck-side lookup (§3.1 option (b)). An opt-in flag is not a licence
    to smuggle that cost back in, so this is pinned rather than left to good manners.
    """
    trucks, orders = _tied_fleet()
    app = _app(
        trucks, orders,
        rebate_aware=True,
        facility_docs=_facility_docs_with_rebate(),
        topology="pooled",
    )
    for step in range(6):
        app.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=step)

    assert app.manager.facility_calls == 1, (
        f"the facility collection was paged {app.manager.facility_calls} times across "
        "6 ticks; the rebate book must be built once per app, not once per tick"
    )
    # ...and the book is genuinely populated, so this is not passing by doing nothing.
    assert app._solver.rebate_book is not None
    assert len(app._solver.rebate_book) == len(_facility_docs_with_rebate())


def test_read_failure_clears_the_injected_book():
    """R2-9 / review F10 — a failed read must CLEAR the book, not keep a stale one.

    `_inject_rebate_book` used to `return` on a read failure, leaving whatever book a
    previous tick injected attached to the solver. A solver would then go on pricing
    from data the system can no longer read, and nothing would say so. `None` is the
    seam's own word for "I do not know".
    """
    trucks, orders = _tied_fleet()
    app = _app(trucks, orders, rebate_aware=True,
               facility_docs=_facility_docs_with_rebate(), topology="pooled")

    app.assign("Wed, 01 Jan 2020 08:00:00 GMT", time_step=0)
    assert app._solver.rebate_book is not None, "fixture never injected a book"
    assert len(app._solver.rebate_book) == 2

    # The read now fails, and the previously successful cache is gone (as it would be
    # on a fresh app whose very first read fails).
    app._rebate_book_cache = None

    def _boom(*a, **k):
        raise RuntimeError("facility endpoint down")

    app.manager._paged_where = _boom
    app.assign("Wed, 01 Jan 2020 09:00:00 GMT", time_step=1)

    assert app._solver.rebate_book is None, (
        "a stale rebate book survived a read failure — the solver would keep pricing "
        "from data the system can no longer read"
    )


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
