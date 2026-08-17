"""Haulier model: distribution, generation tagging, and strict assignment matching.

A truck may only be assigned orders issued by its own haulier (matched on the stable
``haulier_id``). These tests lock that rule in at the three layers it spans:
config distribution, behavior generation, and the assignment solver.
"""

from collections import Counter

from apps.container_logistics.scenario import scenario_config as sc
from apps.container_logistics.scenario.generate_behavior import GenerateBehavior
from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.assignment.app import AssignmentApp


# --- normalize / distribute -------------------------------------------------

def test_normalize_defaults_to_single_haulier():
    assert sc.normalize_hauliers(None) == [
        {"id": "haulier", "name": "Haulier", "fleet_share": 1.0, "order_share": 1.0}
    ]


def test_normalize_slugifies_and_dedupes_ids():
    out = sc.normalize_hauliers(
        [{"name": "Haulier A"}, {"name": "Haulier B"}, {"name": "Haulier A"}]
    )
    ids = [h["id"] for h in out]
    assert ids == ["haulier_a", "haulier_b", "haulier_a_1"]
    # missing shares backfill to an equal split.
    assert all(abs(h["fleet_share"] - 1 / 3) < 1e-9 for h in out)


def test_distribute_by_share_matches_counts_exactly():
    hauliers = [
        {"id": "a", "name": "A", "fleet_share": 2, "order_share": 1},
        {"id": "b", "name": "B", "fleet_share": 1, "order_share": 1},
    ]
    dist = sc.distribute_by_share(9, hauliers, "fleet_share")
    counts = Counter(h["id"] for h in dist)
    assert len(dist) == 9
    assert counts["a"] == 6 and counts["b"] == 3  # 2:1 split


def test_distribute_single_haulier_assigns_all():
    dist = sc.distribute_by_share(5, sc.normalize_hauliers(None), "order_share")
    assert {h["id"] for h in dist} == {"haulier"}
    assert len(dist) == 5


# --- generation tagging -----------------------------------------------------

def test_generated_truck_and_order_carry_haulier():
    h = {"id": "acme", "name": "Acme", "fleet_share": 1, "order_share": 1}
    truck = GenerateBehavior.container_truck("truck_000000", haulier=h)
    order = GenerateBehavior.container_order("order_000000", haulier=h)
    assert truck["profile"]["haulier_id"] == "acme"
    assert truck["profile"]["haulier_name"] == "Acme"
    assert order["profile"]["haulier_id"] == "acme"


def test_generation_without_haulier_falls_back_to_default():
    truck = GenerateBehavior.container_truck("truck_000000")
    assert truck["profile"]["haulier_id"] == "haulier"


# --- matching ---------------------------------------------------------------

def _truck(hid):
    return {"_id": f"t-{hid}", "state": "online", "profile": {"haulier_id": hid, "truck_size": "20ft"}}


def _order(hid):
    return {"_id": f"o-{hid}", "profile": {"haulier_id": hid, "order_size": "1x20"}}


def test_haulier_matches_only_same_id():
    assert C.haulier_matches(_truck("a"), _order("a")) is True
    assert C.haulier_matches(_truck("a"), _order("b")) is False


def test_haulier_match_is_fail_closed_when_missing():
    assert C.haulier_matches(_truck("a"), {"profile": {}}) is False
    assert C.haulier_matches({"profile": {}}, _order("a")) is False


def test_pair_allowed_rejects_cross_haulier():
    assert C.pair_allowed(_truck("a"), _order("a"), max_travel_time_pickup=None) is True
    assert C.pair_allowed(_truck("a"), _order("b"), max_travel_time_pickup=None) is False


# --- solver bucketing (end to end on AssignmentApp.assign) -------------------

class _FakeManager:
    """Stand-in for AssignmentManager returning canned trucks/orders."""

    def __init__(self, trucks, orders):
        self._trucks = trucks
        self._orders = orders

    def list_trucks(self, projection=None, online_only=False):
        return self._trucks

    def list_unassigned_orders(self, projection=None):
        return self._orders

    def order_ids_with_open_haul(self):
        return set()

    def active_haul_truck_ids(self):
        return []

    @staticmethod
    def online_state_name():
        return "online"


def _make_app(trucks, orders):
    app = AssignmentApp.__new__(AssignmentApp)
    # Minimal wiring: solver + behavior + fake manager.
    from apps.container_logistics.assignment.solver import RandomAssignmentSolver

    app._solver = RandomAssignmentSolver()
    app._solver_params = {}
    app._warned_haulier_block = False
    app.behavior = {"profile": {"respect_truck_online_state": True, "reject_if_active_haul_trip": True}}
    app.manager = _FakeManager(trucks, orders)
    return app


def test_assign_never_crosses_haulier_boundary():
    trucks = [_truck("a"), _truck("b")]
    orders = [_order("a"), _order("b")]
    app = _make_app(trucks, orders)
    result = app.assign("2020-01-01 00:00:00 GMT")
    # Every (truck, order) pair must share a haulier id.
    assert result
    for truck, order in result:
        assert truck["profile"]["haulier_id"] == order["profile"]["haulier_id"]


def test_assign_starves_orders_with_no_matching_truck():
    # Two orders for haulier "b", but the only truck belongs to "a".
    trucks = [_truck("a")]
    orders = [_order("b"), _order("b")]
    app = _make_app(trucks, orders)
    result = app.assign("2020-01-01 00:00:00 GMT")
    assert result == []
