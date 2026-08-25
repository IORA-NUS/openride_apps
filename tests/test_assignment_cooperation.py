"""Component clearing + benefit-driven job sharing in AssignmentApp (plan A3 gate).

Exercises the REAL ``AssignmentApp.assign``/``publish`` with a stub manager:
- no-coop parity: without a structure (or with a no-edge one), matching is
  byte-identical to the pre-cooperation behavior;
- benefit-driven sharing: with a structure edge, a partner truck wins an order
  ON COST while the owner's own truck sits free (never a roster-full fallback);
- fail-closed eligibility (no edge => never cross); benefit_km measurement;
- publish() carries the shared/owner/carrier/benefit fields.
"""

import json
import random
from typing import Any, Dict, List

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.assignment.solver import get_solver


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid, haulier, lon, lat):
    return {
        "_id": tid,
        "state": "online",
        "haulier_id": haulier,
        "profile": {"haulier_id": haulier, "current_loc": _pt(lon, lat), "truck_size": "40ft"},
    }


def _order(oid, haulier, lon, lat):
    return {
        "_id": oid,
        "state": "unassigned",
        "haulier_id": haulier,
        "pickup_loc": _pt(lon, lat),
        "dropoff_loc": _pt(lon + 0.05, lat),
        "profile": {"haulier_id": haulier},
    }


class _StubManager:
    def __init__(self, trucks, orders):
        self._trucks = trucks
        self._orders = orders

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


class _StubMessenger:
    def __init__(self):
        self.published: List[Dict[str, Any]] = []
        self.client = self

    def publish(self, topic, message):
        self.published.append({"topic": topic, "payload": json.loads(message)})


def _app(trucks, orders, *, cooperation=None, strategy="GreedyNearest"):
    """Build a real AssignmentApp without the ORSim lifecycle (stub manager/messenger)."""
    app = AssignmentApp.__new__(AssignmentApp)
    profile = {
        "strategy": strategy,
        "respect_truck_online_state": True,
        "reject_if_active_haul_trip": True,
        "use_spatial_matching": True,
    }
    if cooperation is not None:
        profile["cooperation"] = cooperation
    app.behavior = {"profile": profile}
    app.run_id = "run_test"
    app.manager = _StubManager(trucks, orders)
    app.messenger = _StubMessenger()
    app._solver = get_solver(strategy, None)
    app._solver_params = {}
    app._warned_haulier_block = False
    app._share_tags = {}
    return app


def _coop(edges, active="s1"):
    all_ids = sorted({h for e in edges for h in e} | {"acme", "borax", "cargo"})
    from apps.container_logistics.datagen import hauliers as H

    return H.normalize_cooperation(
        {"active": active, "structures": [{"id": "s1", "edges": edges}]}, all_ids
    )


# Geometry: order pickup at lon 103.85; Acme truck ~11 km away, Borax truck ~1.1 km away.
FAR, NEAR = 0.10, 0.01


def _fleet():
    trucks = [
        _truck("t_acme", "acme", 103.85 + FAR, 1.30),
        _truck("t_borax", "borax", 103.85 + NEAR, 1.30),
        _truck("t_cargo", "cargo", 103.85 + NEAR / 2, 1.30),  # solo control, even nearer
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    return trucks, orders


def test_no_structure_keeps_own_haulier_only():
    random.seed(7)
    trucks, orders = _fleet()
    app = _app(trucks, orders)  # no cooperation block at all (pre-feature profile)
    matches = app.assign("2020-01-01 08:00:00")
    assert [(t["_id"], o["_id"]) for t, o in matches] == [("t_acme", "o_acme")]
    assert app._share_tags == {}


def test_no_edge_structure_is_parity_with_today():
    random.seed(7)
    trucks, orders = _fleet()
    baseline = _app(trucks, orders).assign("2020-01-01 08:00:00")
    with_noop = _app(trucks, orders, cooperation=_coop([])).assign("2020-01-01 08:00:00")
    assert [(t["_id"], o["_id"]) for t, o in baseline] == [
        (t["_id"], o["_id"]) for t, o in with_noop
    ]


def test_partner_truck_wins_on_cost_while_owner_truck_free():
    random.seed(7)
    trucks, orders = _fleet()
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    # Borax's truck is ~10x closer -> it carries Acme's job EVEN THOUGH Acme has a
    # free feasible truck. Cargo is even nearer but has NO edge -> never eligible.
    assert [(t["_id"], o["_id"]) for t, o in matches] == [("t_borax", "o_acme")]
    tag = app._share_tags[("t_borax", "o_acme")]
    assert tag["shared"] is True
    assert tag["owner_haulier_id"] == "acme" and tag["carrier_haulier_id"] == "borax"
    # benefit = own_best (Acme truck ~11.1 km) - cross cost (~1.1 km) ≈ 10 km.
    assert tag["benefit_km"] == pytest.approx(10.0, abs=0.5)


def test_benefit_none_when_owner_has_no_feasible_truck():
    random.seed(7)
    trucks = [_truck("t_borax", "borax", 103.86, 1.30)]  # Acme has NO truck at all
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    assert [(t["_id"], o["_id"]) for t, o in matches] == [("t_borax", "o_acme")]
    assert app._share_tags[("t_borax", "o_acme")]["benefit_km"] is None


def test_benefit_never_negative_when_own_truck_consumed_by_earlier_match():
    # Acme has ONE truck and TWO orders; the truck takes the nearer order, the
    # other order goes to the partner. The naive pre-solve own_best would compare
    # against the already-consumed acme truck and go negative; the realizable-
    # alternative rule must yield None (no free own truck), never a negative.
    random.seed(7)
    trucks = [
        _truck("t_acme", "acme", 103.851, 1.30),
        _truck("t_borax", "borax", 103.99, 1.30),  # far partner truck
    ]
    orders = [
        _order("o_near", "acme", 103.85, 1.30),
        _order("o_far", "acme", 103.87, 1.30),
    ]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    assert len(matches) == 2
    cross = [tag for tag in app._share_tags.values()]
    assert len(cross) == 1
    b = cross[0]["benefit_km"]
    assert b is None or b >= 0.0, f"benefit_km must never be negative (got {b})"


def test_publish_carries_share_fields():
    random.seed(7)
    trucks, orders = _fleet()
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    app.publish(matches)
    msgs = app.messenger.published
    assert len(msgs) == 1 and msgs[0]["topic"] == "run_test/t_borax"
    p = msgs[0]["payload"]
    assert p["shared"] is True
    assert p["owner_haulier_id"] == "acme" and p["carrier_haulier_id"] == "borax"
    assert p["benefit_km"] is not None


def test_own_pairs_still_win_when_own_truck_is_cheapest():
    random.seed(7)
    trucks = [
        _truck("t_acme", "acme", 103.851, 1.30),   # own truck nearest
        _truck("t_borax", "borax", 103.95, 1.30),  # partner far away
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    assert [(t["_id"], o["_id"]) for t, o in matches] == [("t_acme", "o_acme")]
    assert app._share_tags == {}  # own-fleet match is not "shared"


def test_share_eligibility_is_edge_direct_not_transitive():
    # acme-borax and borax-cargo, but NO acme-cargo edge: cargo's (nearest) truck
    # must never take acme's order, even though they share a component.
    random.seed(7)
    trucks = [
        _truck("t_acme", "acme", 103.85 + FAR, 1.30),
        _truck("t_cargo", "cargo", 103.85 + NEAR / 2, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(
        trucks, orders, cooperation=_coop([["acme", "borax"], ["borax", "cargo"]])
    )
    matches = app.assign("2020-01-01 08:00:00")
    assert [(t["_id"], o["_id"]) for t, o in matches] == [("t_acme", "o_acme")]
    assert app._share_tags == {}


def test_constraint_helpers_fail_closed():
    adj = C.share_adjacency({"adjacency": {"acme": ["borax"], "borax": ["acme"]}})
    t_borax = _truck("t", "borax", 103.85, 1.3)
    t_none = {"_id": "x", "state": "online", "profile": {}}
    o_acme = _order("o", "acme", 103.85, 1.3)
    assert C.share_eligible(t_borax, o_acme, adj) is True
    assert C.share_eligible(t_none, o_acme, adj) is False        # missing haulier
    assert C.share_eligible(t_borax, o_acme, {}) is False        # empty adjacency
    t_acme = _truck("t2", "acme", 103.85, 1.3)
    assert C.share_eligible(t_acme, o_acme, adj) is False        # same haulier != "share"


def test_chain_component_ineligible_trucks_cannot_starve_orders():
    # F1 regression (HIGH): structure A-B, B-C (chain; NO A-C edge). 45 cargo
    # trucks parked AT acme's pickup could consume the whole spatial candidate
    # cap (default 40) by proximity despite being share-INELIGIBLE for acme —
    # starving the order while acme's own truck sits free 3 km away. Candidates
    # must be generated over eligible trucks only.
    random.seed(7)
    trucks = [_truck(f"t_cargo_{i}", "cargo", 103.8501, 1.3001) for i in range(45)]
    trucks.append(_truck("t_acme", "acme", 103.88, 1.30))  # ~3.3 km away, eligible
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(
        trucks, orders,
        cooperation=_coop([["acme", "borax"], ["borax", "cargo"]]),
    )
    matches = app.assign("2020-01-01 08:00:00")
    assert [(t["_id"], o["_id"]) for t, o in matches] == [("t_acme", "o_acme")], \
        "order starved: ineligible trucks consumed the candidate cap"
    assert app._share_tags == {}


def test_benefit_scan_sees_own_truck_outside_candidate_cap():
    # F2 regression: 41 borax (partner) trucks nearer than acme's own truck fill
    # the cap; the owner's free own truck must still be found by the post-solve
    # scan — benefit is a real number, NOT None ("unserveable") — and SIGNED (F3):
    # here the nearest borax truck is genuinely cheaper, so benefit > 0.
    random.seed(7)
    trucks = [_truck(f"t_borax_{i}", "borax", 103.85 + 0.001 * (i + 1), 1.30) for i in range(41)]
    trucks.append(_truck("t_acme", "acme", 103.90, 1.30))  # own truck, ~5.6 km
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    assert len(matches) == 1
    (truck, order), = matches
    assert truck["_id"].startswith("t_borax")
    tag = app._share_tags[(truck["_id"], "o_acme")]
    assert tag["benefit_km"] is not None, "phantom 'unserveable' — scan missed the free own truck"
    assert tag["benefit_km"] > 0
