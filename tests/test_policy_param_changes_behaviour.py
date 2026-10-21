"""A policy param edited in the spec reaches the dispatch decision (the editor does something).

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2 tests): one order,
a truck 3 km away whose last drop-off is the order's pickup facility (a dual cycle),
and a truck 0.5 km away with no dual cycle. Through the REAL ``AssignmentApp``
(``__init__``'s resolution path, then ``assign``), a profile whose
``policies.dispatch.params.dual_cycle_bonus_km`` is 5.0 awards the dual-cycle truck,
and 0.0 awards the near truck. The profiles are produced by the per-run override
path, the same one the dashboard/CLI params take.

Why: a params form whose values never reach ``decide`` would look like it works.
"""

from __future__ import annotations

import random
from copy import deepcopy

from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.scenario.scenario_overrides import apply_policy_overrides


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid, lon, lat, **extra):
    return {"_id": tid, "state": "online", "haulier_id": "acme",
            "profile": {"haulier_id": "acme", "current_loc": _pt(lon, lat), "truck_size": "40ft", **extra}}


def _order(oid, lon, lat):
    return {"_id": oid, "state": "unassigned", "haulier_id": "acme",
            "pickup_loc": _pt(lon, lat), "dropoff_loc": _pt(lon + 0.05, lat),
            "profile": {"haulier_id": "acme", "pickup_facility_name": "CT_1"}}


class _StubManager:
    def __init__(self, trucks, orders):
        self._trucks, self._orders = trucks, orders

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

    def resolve_facility_resource_id(self, name):
        return None


# ~111 km per degree of latitude: 0.027 deg ~ 3 km, 0.0045 deg ~ 0.5 km.
TRUCKS = [
    _truck("t_dual", 103.85, 1.30 + 0.027, last_dropoff_facility_name="CT_1"),
    _truck("t_near", 103.85, 1.30 + 0.0045),
]
ORDERS = [_order("o1", 103.85, 1.30)]


def _awards(bonus_km, monkeypatch):
    from apps.container_logistics.assignment import app as app_module

    collection = {"a0": {"profile": {"strategy": "GreedyNearest",
                                     "respect_truck_online_state": True,
                                     "reject_if_active_haul_trip": True}}}
    # Round 2: the legacy profile is Partitioned/GreedyNearest; the dial is the inner's.
    apply_policy_overrides(collection, {"dispatch": {"params": {"inner": {"params": {"dual_cycle_bonus_km": bonus_km}}}}})
    behavior = deepcopy(collection["a0"])

    def _stub(self, **kwargs):
        self.behavior = kwargs["behavior"]

    monkeypatch.setattr(app_module.ORSimApp, "__init__", _stub)
    app = AssignmentApp(run_id="r", sim_clock="c", behavior=behavior, messenger=None)
    app.run_id = "r"
    app.manager = _StubManager(deepcopy(TRUCKS), deepcopy(ORDERS))
    app._warned_haulier_block = False
    app._share_tags = {}
    random.seed(3)
    return sorted((str(t["_id"]), str(o["_id"])) for t, o in app.assign("Wed, 01 Jan 2020 08:00:00 GMT", 1))


def test_dual_cycle_bonus_param_changes_awards_end_to_end(monkeypatch):
    """Verified to have teeth: making ``merge_policy_overrides`` drop the override
    params (so both profiles ran the 5.0 default) gave the dual-cycle truck both times."""
    with_bonus = _awards(5.0, monkeypatch)
    without = _awards(0.0, monkeypatch)
    assert with_bonus == [("t_dual", "o1")], with_bonus
    assert without == [("t_near", "o1")], without
