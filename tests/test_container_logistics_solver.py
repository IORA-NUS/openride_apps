"""Pluggable assignment solvers: registry, greedy nearest, and dual-cycle cost.

Locks in the plug-and-play contract (select a solver by name, inject feasibility
+ cost) and the GreedyNearest behaviour: pick the globally nearest truck->pickup
pair first, with a dual-cycle discount for chaining a haul off the truck's last
drop-off. The cost function degrades to ``inf`` (random ordering) when geometry
is missing rather than crashing.
"""

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.assignment.solver import (
    DEFAULT_SOLVER,
    SOLVER_REGISTRY,
    GreedyNearestSolver,
    RandomAssignmentSolver,
    get_solver,
)


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid, lon, lat, *, last_dropoff=None, last_dropoff_facility=None):
    prof = {"current_loc": _pt(lon, lat)}
    if last_dropoff is not None:
        prof["last_dropoff_loc"] = last_dropoff
    if last_dropoff_facility is not None:
        prof["last_dropoff_facility_name"] = last_dropoff_facility
    return {"_id": tid, "profile": prof}


def _order(oid, lon, lat, *, pickup_facility=None):
    prof = {}
    if pickup_facility is not None:
        prof["pickup_facility_name"] = pickup_facility
    return {"_id": oid, "pickup_loc": _pt(lon, lat), "profile": prof}


_ALLOW = lambda t, o: True  # noqa: E731
_COST = lambda t, o: C.assignment_cost(t, o, {})  # noqa: E731


# --- registry / plug-and-play ----------------------------------------------

def test_registry_contains_known_solvers():
    assert set(SOLVER_REGISTRY) == {"RandomAssignment", "GreedyNearest"}


def test_get_solver_by_name():
    assert isinstance(get_solver("GreedyNearest", {}), GreedyNearestSolver)
    assert isinstance(get_solver("RandomAssignment", None), RandomAssignmentSolver)


def test_get_solver_unknown_falls_back_to_default():
    solver = get_solver("DoesNotExist", None)
    assert type(solver) is SOLVER_REGISTRY[DEFAULT_SOLVER]


def test_solver_params_are_passed_through():
    solver = get_solver("GreedyNearest", {"dual_cycle_bonus_km": 9.0})
    assert solver.params["dual_cycle_bonus_km"] == 9.0


# --- greedy nearest ---------------------------------------------------------

def test_greedy_picks_nearest_truck():
    near = _truck("near", 103.851, 1.291)
    far = _truck("far", 103.99, 1.40)
    order = _order("o1", 103.85, 1.29)
    out = GreedyNearestSolver().solve([far, near], [order], pair_allowed=_ALLOW, cost=_COST)
    assert [(t["_id"], o["_id"]) for t, o in out] == [("near", "o1")]


def test_greedy_global_optimum_over_order_priority():
    # Two orders, two trucks. A per-order greedy taking o_far first could grab the
    # truck o_near needs; global nearest-pair assigns the smallest distance first.
    t1 = _truck("t1", 103.850, 1.290)
    t2 = _truck("t2", 103.860, 1.290)
    o_near = _order("o_near", 103.851, 1.290)  # right next to t1
    o_far = _order("o_far", 103.900, 1.290)
    out = GreedyNearestSolver().solve([t1, t2], [o_far, o_near], pair_allowed=_ALLOW, cost=_COST)
    pairs = {o["_id"]: t["_id"] for t, o in out}
    assert pairs["o_near"] == "t1"  # nearest pair locked first
    assert pairs["o_far"] == "t2"
    assert len(out) == 2


def test_greedy_never_double_assigns():
    t = _truck("t", 103.85, 1.29)
    orders = [_order("o1", 103.851, 1.291), _order("o2", 103.852, 1.292)]
    out = GreedyNearestSolver().solve([t], orders, pair_allowed=_ALLOW, cost=_COST)
    assert len(out) == 1  # only one truck -> one assignment


def test_greedy_respects_pair_allowed():
    t = _truck("t", 103.85, 1.29)
    o = _order("o1", 103.851, 1.291)
    out = GreedyNearestSolver().solve([t], [o], pair_allowed=lambda t, o: False, cost=_COST)
    assert out == []


def test_greedy_handles_missing_geometry():
    # No coordinates anywhere -> cost is inf, but the solver still assigns (random).
    t = {"_id": "t", "profile": {}}
    o = {"_id": "o", "profile": {}}
    out = GreedyNearestSolver().solve([t], [o], pair_allowed=_ALLOW, cost=_COST)
    assert [(a["_id"], b["_id"]) for a, b in out] == [("t", "o")]


# --- dual-cycle cost --------------------------------------------------------

def test_dual_cycle_discount_flips_choice():
    near = _truck("near", 103.851, 1.291)  # base ~0 km from pickup
    # `mid` is ~3 km away but just dropped off AT the pickup -> dual-cycle bonus.
    pickup = _pt(103.85, 1.29)
    mid = _truck("mid", 103.88, 1.29, last_dropoff=pickup)
    order = {"_id": "o1", "pickup_loc": pickup, "profile": {}}
    out = GreedyNearestSolver().solve([near, mid], [order], pair_allowed=_ALLOW, cost=_COST)
    # 3 km base minus a 5 km bonus clamps to 0, beating `near`.
    assert out[0][0]["_id"] == "mid"


def test_dual_cycle_via_facility_name_match():
    t = _truck("t", 103.88, 1.29, last_dropoff_facility="portX")
    o = _order("o", 103.85, 1.29, pickup_facility="portX")
    assert C.assignment_cost(t, o, {}) == 0.0


def test_cost_missing_geometry_is_inf():
    import math

    assert math.isinf(C.assignment_cost({"profile": {}}, {"profile": {}}, {}))


def test_cost_is_plain_distance_without_dual_cycle():
    t = _truck("t", 103.85, 1.29)
    o = _order("o", 103.95, 1.29)
    cost = C.assignment_cost(t, o, {})
    assert cost > 0 and not __import__("math").isinf(cost)
