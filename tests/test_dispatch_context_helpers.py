"""Planner tick helpers: verbatim moves, held to frozen copies of the old inline code.

What is locked: the moved helpers (``solver_lib/candidates.py``,
``solver_lib/objective.py``, the framework's ``Tick.permitted`` / ``Tick.serves``,
``pooled_market.CompanyRule``, ``views.py``, ``compat.legacy_context``) compute
exactly what the inline code in ``assignment/app.py`` and
``assignment/pooled_planner.py`` computed at ``ec085f5``, including how many draws
they take from which RNG.

Why: the dispatch refactor (docs/plans/policy_slots_p0_p3.md §4 P1; the generic
contract, plan_contract_final.md WS1) must be byte-identical. The golden test locks
the end-to-end result; these tests pin each moved piece separately against an ORACLE,
a frozen copy of the old inline code kept inside this file, so a drift is located to
one helper rather than to "the tick".
"""

from __future__ import annotations

import itertools
import math
import random
from typing import Any, Dict, List, Tuple

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import spatial
from apps.container_logistics.policies.compat.legacy_context import LegacyDecideContext
from apps.container_logistics.policies.dispatch.bind import bind_tree
from apps.container_logistics.policies.dispatch.library.pooled_market import CompanyRule
from apps.container_logistics.policies.dispatch.resolved import ResolvedDispatch
from apps.container_logistics.policies.dispatch.runner import DispatchRunner
from apps.container_logistics.policies.dispatch.tick import DispatchClock
from apps.container_logistics.policies.dispatch.views import OrderView, TruckView
from apps.container_logistics.policies.solver_lib import candidates as C
from apps.container_logistics.policies.solver_lib import objective as OBJ
from apps.container_logistics.policies.solver_lib.pair_solver import PairSolver
from apps.container_logistics.rebate import RebateBook  # noqa: F401  (leaf import check)
from tests.test_assignment_cooperation import _order, _pt, _truck

_CLOCK = DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=7)


class _Leaf(PairSolver):
    """An unregistered pair leaf: its top ``bind`` declares the profile's active arm (``compat.unregistered``)."""

    def solve(self, trucks, orders, *, pair_allowed, cost):  # pragma: no cover - ticks are not solved here
        return []


def _profile(adjacency=None, max_pickup=None, **knobs):
    """An assignment-profile slice: ``max_travel_time_pickup``, an arm with ``adjacency``, knobs."""
    prof: Dict[str, Any] = {"max_travel_time_pickup": max_pickup, **knobs}
    if adjacency:
        prof["cooperation"] = {"active": "s", "structures": [
            {"id": "s", "adjacency": {k: sorted(v) for k, v in adjacency.items()}}]}
    return prof


def _tick(trucks, orders, *, adjacency=None, max_pickup=None, **knobs):
    """The WHOLE tick of an unregistered leaf over ``trucks``/``orders`` under that world."""
    prof = _profile(adjacency, max_pickup, **knobs)
    node = ResolvedDispatch(name="_Leaf", version="unregistered", timing="online", params=None,
                            source="unregistered")
    runner = DispatchRunner(bind_tree(node, _Leaf(), prof), profile=prof)
    return runner.tick(candidates=list(trucks), orders=list(orders), profile=prof, clock=_CLOCK, seed=99)


def _sub(trucks, orders, *, scope="partition:haulier:acme", sample_rng=random, candidates=None, **world):
    """A sub-tick over ALL of the tick's documents (the old partition / company scope)."""
    tick = _tick(trucks, orders, **world)
    return tick.restrict(trucks=tick.raw_trucks, orders=tick.raw_orders, scope=scope,
                         sample_rng=sample_rng, candidates=candidates)


# --- oracles: frozen copies of the old inline code (ec085f5) ------------------


def _oracle_pair_allowed_factory(adjacency, max_pickup_travel):
    """app.py ``_pair_allowed`` closure, verbatim."""

    def _pair_allowed(t: Dict[str, Any], o: Dict[str, Any]) -> bool:
        if assign_constraints.haulier_matches(t, o):
            return assign_constraints.pair_allowed(
                t, o, max_travel_time_pickup=max_pickup_travel
            )
        if adjacency and assign_constraints.share_eligible(t, o, adjacency):
            return assign_constraints.pair_allowed_ignoring_haulier(
                t, o, max_travel_time_pickup=max_pickup_travel
            )
        return False

    return _pair_allowed


def _oracle_serves_factory(adjacency):
    """app.py ``_serves`` closure, verbatim."""

    def _serves(truck_haulier: str, order_haulier: str) -> bool:
        return truck_haulier == order_haulier or (
            order_haulier in adjacency.get(truck_haulier, ())
        )

    return _serves


def _oracle_spatial_pairs(bucket_trucks, bucket_orders, *, adjacency, max_pickup_travel,
                          cell_deg, per_order, max_rings):
    """The spatial branch of app.py ``_solve_bucket``, verbatim up to the solve."""
    _pair_allowed = _oracle_pair_allowed_factory(adjacency, max_pickup_travel)
    _serves = _oracle_serves_factory(adjacency)
    orders_by_haulier: Dict[str, List[Dict[str, Any]]] = {}
    for o in bucket_orders:
        oh = assign_constraints.haulier_of(o)
        if oh:
            orders_by_haulier.setdefault(oh, []).append(o)
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for oh, o_group in orders_by_haulier.items():
        eligible = [
            t for t in bucket_trucks
            if (assign_constraints.haulier_of(t) or "") and _serves(
                assign_constraints.haulier_of(t), oh
            )
        ]
        if not eligible:
            continue
        pairs.extend(
            spatial.iter_candidate_pairs(
                eligible,
                o_group,
                pair_allowed=_pair_allowed,
                cell_deg=cell_deg,
                per_order=per_order,
                max_rings=max_rings,
            )
        )
    return pairs


def _oracle_company_allowed_factory(carrier, max_pickup_travel):
    """pooled_planner ``_pair_allowed_for(carrier)`` with app.py's own/pooled closures inlined."""

    def _pair_allowed_own(t, o):
        return assign_constraints.pair_allowed(t, o, max_travel_time_pickup=max_pickup_travel)

    def _pair_allowed_pooled(t, o):
        return assign_constraints.pair_allowed_ignoring_haulier(
            t, o, max_travel_time_pickup=max_pickup_travel
        )

    def _p(t, o, _c=carrier):
        if assign_constraints.haulier_of(o) == _c:
            return _pair_allowed_own(t, o)
        return _pair_allowed_pooled(t, o)

    return _p


def _oracle_settings(prof):
    """app.py assign() parsing of the spatial and sampling knobs, verbatim."""
    max_trucks_per_haulier = max(1, int(prof.get("max_trucks_per_haulier", 500)))
    cell_deg = float(prof.get("spatial_cell_deg", spatial.DEFAULT_CELL_DEG))
    per_order = max(1, int(prof.get("spatial_candidates_per_order", spatial.DEFAULT_PER_ORDER_CANDIDATES)))
    max_rings = max(0, int(prof.get("spatial_max_rings", spatial.DEFAULT_MAX_RINGS)))
    return cell_deg, per_order, max_rings, max_trucks_per_haulier


def _oracle_assignment_cost(truck, order, params=None):
    """constraints.assignment_cost before the _is_dual_cycle_pair factor-out, verbatim."""
    c = assign_constraints
    params = params or {}
    pickup = c.order_pickup_loc(order)
    anchor = c.truck_anchor_loc(truck)
    if pickup is None or anchor is None:
        return float("inf")
    base_km = c._haversine_km(anchor, pickup)
    if base_km is None:
        return float("inf")
    bonus_km = float(params.get("dual_cycle_bonus_km", 5.0))
    radius_km = float(params.get("dual_cycle_radius_km", 0.5))
    is_dual_cycle = False
    last_fac = c._truck_last_dropoff_facility(truck)
    order_pickup_fac = (order.get("profile") or {}).get("pickup_facility_name")
    if last_fac is not None and order_pickup_fac is not None and last_fac == str(order_pickup_fac):
        is_dual_cycle = True
    else:
        last_loc = c._truck_last_dropoff_loc(truck)
        if last_loc is not None:
            d = c._haversine_km(last_loc, pickup)
            if d is not None and d <= radius_km:
                is_dual_cycle = True
    cost = base_km - (bonus_km if is_dual_cycle else 0.0)
    return max(0.0, cost)


def _oracle_is_dual_cycle(truck, order, radius_km):
    """The dual-cycle branch of the oracle above, as a predicate (pins ``objective.is_dual_cycle``)."""
    c = assign_constraints
    pickup = c.order_pickup_loc(order)
    last_fac = c._truck_last_dropoff_facility(truck)
    order_pickup_fac = (order.get("profile") or {}).get("pickup_facility_name")
    if last_fac is not None and order_pickup_fac is not None and last_fac == str(order_pickup_fac):
        return True
    last_loc = c._truck_last_dropoff_loc(truck)
    if last_loc is not None:
        d = c._haversine_km(last_loc, pickup)
        if d is not None and d <= radius_km:
            return True
    return False


# --- generated grids -----------------------------------------------------------


def _grid_trucks(n_per=6, hauliers=("acme", "borax", "cargo")):
    trucks = []
    rnd = random.Random(11)
    for h in hauliers:
        for i in range(n_per):
            t = _truck(f"t_{h}_{i}", h, 103.80 + rnd.random() * 0.2, 1.25 + rnd.random() * 0.1)
            if i % 3 == 0:
                t["profile"]["truck_size"] = "20ft"
            if i % 4 == 1:
                t["profile"]["restricted_areas"] = ["fac_x"]
            if i % 2 == 0:
                t["profile"]["last_dropoff_loc"] = _pt(103.85 + rnd.random() * 0.02, 1.30)
            if i % 5 == 2:
                t["profile"]["last_dropoff_facility_name"] = "fac_x"
            trucks.append(t)
    trucks.append({"_id": "t_none", "state": "online", "profile": {"current_loc": _pt(103.85, 1.30)}})
    trucks.append({"_id": "t_nogeo", "haulier_id": "acme", "profile": {"haulier_id": "acme"}})
    return trucks


def _grid_orders(n_per=5, hauliers=("acme", "borax", "cargo")):
    orders = []
    rnd = random.Random(23)
    for h in hauliers:
        for i in range(n_per):
            o = _order(f"o_{h}_{i}", h, 103.80 + rnd.random() * 0.2, 1.25 + rnd.random() * 0.1)
            if i % 2 == 1:
                o["profile"]["order_size"] = "2x20"
            if i % 3 == 0:
                o["profile"]["pickup_facility_name"] = "fac_x"
            orders.append(o)
    orders.append({"_id": "o_nogeo", "haulier_id": "borax", "profile": {"haulier_id": "borax"}})
    return orders


_ADJACENCIES = [
    {},
    {"acme": {"borax"}, "borax": {"acme"}},
    {"acme": {"borax", "cargo"}, "borax": {"acme"}, "cargo": {"acme"}},
    # Asymmetric: acme trucks may take borax orders, never the reverse.
    {"acme": {"borax"}},
]
_MAX_PICKUP = [None, 0, 900, "bad"]


def _ids(pairs):
    return [(id(t), id(o)) for t, o in pairs]


# --- tests ---------------------------------------------------------------------


@pytest.mark.parametrize("adjacency", _ADJACENCIES)
@pytest.mark.parametrize("per_order", [1, 3, 40])
def test_spatial_partition_pairs_match_the_old_inline_grouping(adjacency, per_order):
    """Same pairs, same order, same objects as the old inline loop.

    Both the list-level ``spatial_pairs_for_partition`` (fed the oracle's own
    ``serves``/``allowed``) and the tick-level ``spatial_pairs`` (fed the tick's
    ``serves``/``permitted``, i.e. the top's declared pairs) are pinned.

    Verified to have teeth: swapping the arguments to ``serves`` in
    ``spatial_pairs_for_partition`` failed the asymmetric-adjacency cases (it
    survived while every adjacency here was symmetric, hence the fourth one).
    """
    trucks, orders = _grid_trucks(), _grid_orders()
    settings = C.CandidateSettings(cell_deg=0.02, per_order=per_order, max_rings=8, max_trucks_per_scope=500)
    for max_pickup in _MAX_PICKUP:
        want = _oracle_spatial_pairs(
            trucks, orders, adjacency=adjacency, max_pickup_travel=max_pickup,
            cell_deg=0.02, per_order=per_order, max_rings=8,
        )
        got = C.spatial_pairs_for_partition(
            trucks, orders, serves=_oracle_serves_factory(adjacency),
            allowed=_oracle_pair_allowed_factory(adjacency, max_pickup), settings=settings)
        assert _ids(got) == _ids(want), f"spatial pairs drifted (adjacency={adjacency}, max_pickup={max_pickup})"
        tick = _tick(trucks, orders, adjacency=adjacency, max_pickup=max_pickup)
        assert _ids(C.spatial_pairs(tick, settings)) == _ids(want), (adjacency, max_pickup)
    assert got, "the grid must produce candidate pairs or this test is vacuous"


def test_spatial_pairs_call_iter_candidate_pairs_through_the_module(monkeypatch):
    """Plan §2.8 / §7 risk 3: tests wrap ``spatial.iter_candidate_pairs``; the helper must see it."""
    calls = []
    real = spatial.iter_candidate_pairs

    def _wrapped(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(spatial, "iter_candidate_pairs", _wrapped)
    tick = _tick(_grid_trucks(), _grid_orders())
    C.spatial_pairs(tick, C.CandidateSettings.from_world(tick.world))
    assert len(calls) == 3, "one iter_candidate_pairs call per order haulier, through the module"


def test_sampled_trucks_cap_and_rng_source():
    """Module RNG on a partition sub-tick, the tick Random on a company sub-tick; at most one draw.

    Verified to have teeth: drawing from ``tick.rng`` instead of ``tick.sample_rng``
    on a sub-tick in ``candidates.tick_sampled_trucks`` failed this test; so did
    replacing its ``sampled is None`` memo test with ``True`` (a second draw).
    """
    trucks = [_truck(f"t{i}", "acme", 103.8 + i * 0.001, 1.3) for i in range(10)]
    orders = [_order("o1", "acme", 103.85, 1.3)]

    # Under the cap: the same object back, no draw at all.
    rng = random.Random(5)
    before = rng.getstate()
    assert C.sampled_trucks(trucks, cap=10, rng=rng) is trucks
    assert rng.getstate() == before

    # Over the cap: exactly rng.sample(trucks, cap).
    a, b = random.Random(5), random.Random(5)
    assert C.sampled_trucks(trucks, cap=4, rng=a) == b.sample(trucks, 4)
    assert a.getstate() == b.getstate()

    settings = C.CandidateSettings(cell_deg=0.02, per_order=40, max_rings=8, max_trucks_per_scope=4)

    # Partition sub-tick: draws from the random MODULE, once, only when asked.
    random.seed(99)
    seeded = random.getstate()
    expected = random.sample(list(trucks), 4)
    after_one_draw = random.getstate()

    random.seed(99)
    sub = _sub(trucks, orders, sample_rng=random, max_trucks_per_haulier=4)
    assert random.getstate() == seeded, "building a sub-tick must not draw"
    ctx = LegacyDecideContext(sub)
    first = C.tick_sampled_trucks(sub, settings)
    C.tick_sampled_trucks(sub, settings)
    ctx.sampled_trucks()
    ctx.candidate_pairs("sampled")
    assert [t["_id"] for t in first] == [t["_id"] for t in expected]
    assert random.getstate() == after_one_draw, "sampled trucks must be drawn exactly once per tick"

    # Company sub-tick: draws from the tick Random it was handed; the module is untouched.
    random.seed(99)
    module_before = random.getstate()
    tick_rng, oracle = random.Random(1234), random.Random(1234)
    csub = _sub(trucks, orders, scope="company:acme", sample_rng=tick_rng)
    got = C.tick_sampled_trucks(csub, settings)
    assert [t["_id"] for t in got] == [t["_id"] for t in oracle.sample(list(trucks), 4)]
    assert tick_rng.getstate() == oracle.getstate()
    assert random.getstate() == module_before, "the company sub-tick must not touch the module RNG"


def test_dual_cycle_cost_equals_assignment_cost_on_a_grid():
    """Bit-identical floats: the helper, today's assignment_cost, and the pre-refactor oracle.

    Verified to have teeth: changing ``d <= radius_km`` to ``d < radius_km`` in
    ``constraints._is_dual_cycle_pair`` failed this test (oracle mismatch). Since
    gate 6 ``assignment_cost`` inlines its own copy, so each copy is compared with
    the oracle on its own (the cost assert and the ``is_dual_cycle`` assert).
    """
    trucks, orders = _grid_trucks(), _grid_orders()
    # Put a last drop-off exactly on a pickup and one exactly at the radius boundary.
    trucks[1]["profile"]["last_dropoff_loc"] = orders[0]["pickup_loc"]
    knob_grid = [(5.0, 0.5), (0.0, 0.5), (2.5, 0.0), (5.0, 3.0), (100.0, 50.0)]
    checked = dual = 0
    for t, o in itertools.product(trucks, orders):
        for bonus, radius in knob_grid:
            params = {"dual_cycle_bonus_km": bonus, "dual_cycle_radius_km": radius}
            got = OBJ.dual_cycle_cost(t, o, bonus_km=bonus, radius_km=radius)
            want = _oracle_assignment_cost(t, o, params)
            now = assign_constraints.assignment_cost(t, o, params)
            assert repr(got) == repr(want) == repr(now), (t["_id"], o["_id"], bonus, radius)
            is_dual = OBJ.is_dual_cycle(t, o, radius_km=radius)
            # assignment_cost inlines its own copy of the predicate (gate 6), so the
            # helper's copy is pinned separately against the oracle.
            assert is_dual == _oracle_is_dual_cycle(t, o, radius), (t["_id"], o["_id"], radius)
            dual += is_dual
            checked += 1
        assert repr(assign_constraints.assignment_cost(t, o, {})) == repr(_oracle_assignment_cost(t, o, {}))
        dk = OBJ.deadhead_km(t, o)
        base = _oracle_assignment_cost(t, o, {"dual_cycle_bonus_km": 0.0})
        assert (math.isinf(dk) and math.isinf(base)) or dk == base
    assert checked > 100 and dual > 0, "grid must include dual-cycle pairs or the predicate is untested"


@pytest.mark.parametrize(
    "prof",
    [
        {},
        {"spatial_candidates_per_order": 0, "spatial_max_rings": -3, "max_trucks_per_haulier": 0},
        {"spatial_candidates_per_order": "7", "spatial_max_rings": "2", "spatial_cell_deg": "0.05"},
        {"spatial_candidates_per_order": 12.9, "max_trucks_per_haulier": 40, "spatial_cell_deg": 1},
        {"max_trucks_per_haulier": -5, "spatial_max_rings": 0},
    ],
)
def test_candidate_settings_from_profile_matches_app_parsing(prof):
    """Including the clamps: per_order 0 -> 1, max_rings -3 -> 0, max_trucks 0 -> 1.

    ``from_world`` parses a run world identically.

    Verified to have teeth: dropping ``max(1, ...)`` around ``per_order`` in
    ``CandidateSettings.from_profile`` failed the second case.
    """
    s = C.CandidateSettings.from_profile(prof)
    cell_deg, per_order, max_rings, max_trucks = _oracle_settings(prof)
    assert (s.cell_deg, s.per_order, s.max_rings, s.max_trucks_per_scope) == (
        cell_deg, per_order, max_rings, max_trucks
    )
    assert type(s.cell_deg) is float and type(s.per_order) is int
    assert s.spatial_params() == {"cell_deg": cell_deg, "per_order": per_order, "max_rings": max_rings}
    assert C.CandidateSettings.from_world(_tick([], [], **prof).world) == s


def test_candidate_settings_from_profile_raises_like_app_on_garbage():
    with pytest.raises(ValueError):
        C.CandidateSettings.from_profile({"spatial_candidates_per_order": "many"})


def test_views_expose_typed_fields_and_raw():
    t = _truck("t1", "acme", 103.9, 1.31)
    t["profile"]["last_dropoff_loc"] = _pt(103.8, 1.29)
    t["profile"]["last_dropoff_facility_name"] = "fac_x"
    o = _order("o1", "acme", 103.85, 1.30)
    o["profile"].update({"order_size": "2x20", "pickup_facility_name": "fac_a", "dropoff_facility_name": ""})
    bare_t = {"_id": 7, "profile": {}}
    bare_o = {"_id": 8, "order_size": "40ft"}

    tick = _sub([t, bare_t], [o, bare_o])
    tv, btv = tick.trucks
    ov, bov = tick.orders
    assert tick.trucks is tick.trucks, "views are cached per tick"
    assert isinstance(tv, TruckView) and isinstance(ov, OrderView)
    assert (tv.id, tv.haulier_id, tv.size, tv.position) == ("t1", "acme", "40ft", (103.9, 1.31))
    assert tv.last_dropoff_position == (103.8, 1.29) and tv.last_dropoff_facility == "fac_x"
    assert tv.raw is t
    assert (btv.id, btv.haulier_id, btv.size, btv.position) == ("7", None, "20ft", None)
    assert (ov.id, ov.haulier_id, ov.size, ov.pickup, ov.dropoff) == ("o1", "acme", "2x20", (103.85, 1.30), (103.85 + 0.05, 1.30))
    assert (ov.pickup_facility_name, ov.dropoff_facility_name) == ("fac_a", None)
    assert ov.raw is o
    assert (bov.size, bov.pickup, bov.haulier_id) == ("40ft", None, None)
    # Views and raw documents are interchangeable at the tick's and the legacy context's helpers.
    oracle = _oracle_pair_allowed_factory({}, None)
    ctx = LegacyDecideContext(tick)
    assert tick.permitted(tv, ov) == tick.permitted(t, o) == ctx.allowed(tv, ov) == oracle(t, o)
    assert tick.measure.deadhead_km(tv, ov) == ctx.deadhead_km(tv, ov) == OBJ.deadhead_km(t, o)
    assert ctx.dual_cycle_cost(tv, ov, bonus_km=5.0, radius_km=0.5) == assign_constraints.assignment_cost(t, o, {})
    assert tick.measure.cost(t, o) == assign_constraints.assignment_cost(t, o, {})
    with pytest.raises(Exception):
        tv.id = "x"  # frozen


@pytest.mark.parametrize("adjacency", _ADJACENCIES)
def test_tick_permitted_and_serves_match_the_legacy_closure_on_a_pair_grid(adjacency):
    """The top's ``Tick.permitted``/``.serves`` == app.py's ``_pair_allowed``/``_serves``.

    The top's declared pairs come from the arm (the unregistered leaf's bind,
    ``compat.unregistered`` -> ``solver_lib.pair_leaf``); a sub-tick inherits them.

    Verified to have teeth: replacing ``pair_allowed_ignoring_haulier`` with
    ``pair_allowed`` in ``dispatch.tick._top_permitted`` failed the
    non-empty-adjacency cases.
    """
    trucks, orders = _grid_trucks(), _grid_orders()
    allowed_seen = 0
    for max_pickup in _MAX_PICKUP:
        tick = _tick(trucks, orders, adjacency=adjacency, max_pickup=max_pickup)
        sub = tick.restrict(trucks=tick.raw_trucks, orders=tick.raw_orders, scope="s", sample_rng=random)
        oracle = _oracle_pair_allowed_factory(adjacency, max_pickup)
        for t, o in itertools.product(trucks, orders):
            got = tick.permitted(t, o)
            assert got == oracle(t, o) == sub.permitted(t, o), (t["_id"], o["_id"], max_pickup)
            allowed_seen += got
    serves = _oracle_serves_factory(adjacency)
    tick = _tick(trucks, orders, adjacency=adjacency)
    for a, b in itertools.product(["acme", "borax", "cargo", "zeta"], repeat=2):
        assert tick.serves(a, b) == serves(a, b), (a, b)
    assert allowed_seen > 0


def test_company_rule_matches_legacy_closure_on_a_pair_grid():
    """``CompanyRule.allowed`` == pooled ``_pair_allowed_for(carrier)`` over app.py's closures.

    Verified to have teeth: swapping the own/pooled branches in
    ``CompanyRule.allowed`` failed this test.
    """
    trucks, orders = _grid_trucks(), _grid_orders()
    for carrier in ["acme", "borax", "cargo", "zeta"]:
        for max_pickup in _MAX_PICKUP:
            rule = CompanyRule(haulier_id=carrier, max_travel_time_pickup=max_pickup)
            oracle = _oracle_company_allowed_factory(carrier, max_pickup)
            for t, o in itertools.product(trucks, orders):
                assert rule.allowed(t, o) == oracle(t, o), (carrier, t["_id"], o["_id"], max_pickup)
    assert CompanyRule("acme", 60).to_dict() == {"kind": "company", "haulier_id": "acme", "max_travel_time_pickup": 60}


def test_candidate_pairs_are_cached_per_kind_and_full_is_the_allowed_cross_product(monkeypatch):
    """The legacy context's ``candidate_pairs``: cached per kind; ``full`` = every permitted pair;
    a caller's ``candidates`` are served as a copy.

    Verified to have teeth: dropping the ``_pairs_cache`` store in
    ``LegacyDecideContext.candidate_pairs`` failed the one-call assertion.
    """
    trucks, orders = _grid_trucks(n_per=2), _grid_orders(n_per=2)
    calls = []
    real = C.spatial_pairs
    monkeypatch.setattr(C, "spatial_pairs", lambda *a: calls.append(1) or real(*a))

    sub = _sub(trucks, orders, scope="partition:component:3")
    ctx = LegacyDecideContext(sub)
    assert ctx.default_candidate_kind == "sampled", "no candidates param: the legacy default kind"
    assert ctx.scope.kind == "partition" and ctx.scope.key == "partition:component:3" and ctx.scope.round is None
    assert _ids(ctx.candidate_pairs("spatial")) == _ids(ctx.candidate_pairs("spatial"))
    assert len(calls) == 1
    oracle = _oracle_pair_allowed_factory({}, None)
    full = ctx.candidate_pairs("full")
    assert _ids(full) == _ids([(t, o) for t in trucks for o in orders if oracle(t, o)])
    with pytest.raises(ValueError):
        ctx.candidate_pairs("nearest")

    pairs = [(sub.raw_trucks[0], sub.raw_orders[0])]
    cctx = LegacyDecideContext(_sub(trucks, orders, scope="company:acme", sample_rng=random.Random(0),
                                    candidates=pairs), default_candidate_kind="spatial")
    # The pooled round is no longer part of the scope (plan_contract_final.md §7.7: round=None).
    assert cctx.scope.key == "company:acme" and cctx.scope.kind == "company" and cctx.scope.round is None
    got = cctx.candidate_pairs()
    assert _ids(got) == _ids(pairs) and got is not pairs
    assert len(calls) == 1, "a caller's candidates are served; nothing is rebuilt"
