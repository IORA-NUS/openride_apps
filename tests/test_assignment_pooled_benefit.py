"""``benefit_km`` accounting under the pooled topology (shared-pool plan Phase 5).

``benefit_km`` keeps its pre-pool SHADOW-PRICE definition (plan §D6) — the owner's
cheapest own truck still free after the final commit, minus the awarded cost — so
the metric stays comparable across offer/claim policies and against pre-pool runs.
The new fields make it auditable instead of a bare difference.

``tests/test_analytics_collaboration.py`` must keep passing UNMODIFIED: that is the
proof the analytics contract (shared / owner_haulier_id / benefit_km) held.
"""

import json

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.message_data_models import AssignedHaulTripPayload
from apps.container_logistics.statemachine import ContainerLogisticsActions

from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app, _ids


def test_benefit_km_uses_owner_trucks_free_after_final_commit():
    """F2/F3 semantics preserved: 41 partner trucks nearer than acme's own truck
    fill the spatial candidate cap, but the owner's free own truck must still be
    found by the post-commit FULL-FLEET scan — a real number, not a phantom
    'unserveable' None."""
    trucks = [
        _truck(f"t_borax_{i}", "borax", 103.85 + 0.001 * (i + 1), 1.30) for i in range(41)
    ]
    trucks.append(_truck("t_acme", "acme", 103.90, 1.30))  # own truck, ~5.6 km
    orders = [_order("o_acme", "acme", 103.85, 1.30)]

    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    assert len(matches) == 1
    (truck, _order_doc), = matches
    assert truck["_id"].startswith("t_borax")
    tag = app._share_tags[(truck["_id"], "o_acme")]
    assert tag["benefit_km"] is not None, "scan missed the free own truck outside the cap"
    assert tag["benefit_km"] > 0
    assert tag["owner_reserve_km"] == pytest.approx(5.6, abs=0.6)


def test_owner_reserve_is_the_cheapest_of_several_free_trucks():
    """``owner_reserve_km`` must be the MINIMUM over the owner's free feasible
    trucks — the review's F8 gap (plan §13.4 FIX-8).

    Mutating ``pooled_planner.owner_reserve_km``'s ``if best is None or c < best``
    to ``c > best`` SURVIVED the whole suite, because every other benefit fixture
    leaves the owner **at most one** free feasible truck, and with n <= 1 min == max.
    A ``max`` implementation reports 11.117 where the truth is 0.111 — a 100x error
    in ``benefit_km``, the headline collaboration metric, and it flips the sign of
    most reported gains.

    So: acme keeps TWO free feasible trucks two orders of magnitude apart, borax
    carries the single order, and the reserve must be the near one.
    """
    trucks = [
        _truck("t_acme_near", "acme", 103.851, 1.30),  # ~0.111 km  <- the reserve
        _truck("t_acme_far", "acme", 103.95, 1.30),    # ~11.117 km <- the max decoy
        _truck("t_borax", "borax", 103.8505, 1.30),    # ~0.056 km, cheapest overall
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]

    # Fixture guard: if the geometry ever stops producing TWO feasible owner trucks
    # at unmistakably different costs, this test silently loses its teeth.
    near, far = C.assignment_cost(trucks[0], orders[0], {}), C.assignment_cost(trucks[1], orders[0], {})
    assert C.pair_allowed(trucks[0], orders[0], max_travel_time_pickup=None)
    assert C.pair_allowed(trucks[1], orders[0], max_travel_time_pickup=None)
    assert far > 10 * near > 0, f"fixture lost its spread: near={near} far={far}"

    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    # The partner carries it, so BOTH acme trucks are still free after the final
    # commit — i.e. the reserve scan genuinely has two candidates to choose between.
    assert _ids(matches) == [("t_borax", "o_acme")]
    tag = app._share_tags[("t_borax", "o_acme")]

    assert tag["owner_reserve_km"] == pytest.approx(near, abs=1e-3), (
        "owner_reserve_km is not the CHEAPEST free feasible owner truck"
    )
    assert tag["owner_reserve_km"] != pytest.approx(far, abs=1e-3), (
        "owner_reserve_km took the most expensive owner truck (a max/min inversion)"
    )
    # And the metric that depends on it stays small, not 100x too big.
    # abs=2e-3: all three fields are independently round()ed to 3 dp, so the
    # identity can be off by up to ~1.5e-3 at these sub-km magnitudes.
    assert tag["benefit_km"] == pytest.approx(
        tag["owner_reserve_km"] - tag["awarded_cost_km"], abs=2e-3
    )
    assert tag["benefit_km"] < 1.0


def test_benefit_none_when_owner_has_no_free_feasible_truck():
    """None = the owner truly had no free feasible truck this tick."""
    trucks = [_truck("t_borax", "borax", 103.86, 1.30)]  # acme has NO truck at all
    orders = [_order("o_acme", "acme", 103.85, 1.30)]

    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    assert _ids(matches) == [("t_borax", "o_acme")]
    tag = app._share_tags[("t_borax", "o_acme")]
    assert tag["benefit_km"] is None
    assert tag["owner_reserve_km"] is None
    assert tag["awarded_cost_km"] is not None  # the awarded side is still recorded


def test_benefit_owner_truck_consumed_by_an_earlier_award_is_not_counted_free():
    """An own truck already committed this tick is NOT a realizable alternative,
    so it must never inflate the reserve."""
    trucks = [
        _truck("t_acme", "acme", 103.851, 1.30),
        _truck("t_borax", "borax", 103.99, 1.30),
    ]
    orders = [
        _order("o_near", "acme", 103.85, 1.30),
        _order("o_far", "acme", 103.87, 1.30),
    ]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    assert len(matches) == 2
    cross = list(app._share_tags.values())
    assert len(cross) == 1
    tag = cross[0]
    # t_acme is committed to the other order, so acme has no free truck left.
    assert tag["owner_reserve_km"] is None
    assert tag["benefit_km"] is None


def test_benefit_equals_reserve_minus_awarded_cost():
    """The arithmetic identity that makes the number checkable directly in Mongo
    (plan §10.3 check 5)."""
    trucks = [
        _truck("t_acme", "acme", 103.85 + 0.10, 1.30),
        _truck("t_borax", "borax", 103.85 + 0.01, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    app.assign("2020-01-01 08:00:00")

    tag = app._share_tags[("t_borax", "o_acme")]
    assert tag["owner_reserve_km"] is not None and tag["awarded_cost_km"] is not None
    assert tag["benefit_km"] == pytest.approx(
        tag["owner_reserve_km"] - tag["awarded_cost_km"], abs=1e-3
    )


def test_self_claim_emits_no_share_tag():
    """Owner wins its own contributed order => shared=False, no tag, ledger
    identical to today."""
    trucks = [
        _truck("t_acme", "acme", 103.851, 1.30),
        _truck("t_borax", "borax", 103.95, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")

    assert _ids(matches) == [("t_acme", "o_acme")]
    assert app._share_tags == {}

    app.publish(matches)
    payload = app.messenger.published[0]["payload"]
    assert payload["shared"] is False
    assert payload["pool_id"] is None
    assert payload["market_round"] is None


def test_publish_carries_the_new_optional_fields():
    trucks = [
        _truck("t_acme", "acme", 103.85 + 0.10, 1.30),
        _truck("t_borax", "borax", 103.85 + 0.01, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    matches = app.assign("2020-01-01 08:00:00")
    app.publish(matches)

    payload = app.messenger.published[0]["payload"]
    assert payload["shared"] is True
    assert payload["pool_id"] == "p:acme+borax"
    assert payload["market_round"] == 1
    assert payload["awarded_cost_km"] is not None
    assert payload["owner_reserve_km"] is not None
    assert payload["benefit_km"] == pytest.approx(
        payload["owner_reserve_km"] - payload["awarded_cost_km"], abs=1e-3
    )


def test_payload_round_trips_new_optional_fields_and_old_payloads_still_parse():
    """Wire compatibility: four OPTIONAL additions. Old payloads (and therefore old
    consumers / historical runs) keep working."""
    new = AssignedHaulTripPayload(
        action=ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
        order={"_id": "o1"},
        truck_id="t1",
        shared=True,
        owner_haulier_id="acme",
        carrier_haulier_id="borax",
        benefit_km=10.0,
        pool_id="p:acme+borax",
        awarded_cost_km=1.1,
        owner_reserve_km=11.1,
        market_round=2,
    )
    round_tripped = AssignedHaulTripPayload.parse(json.loads(json.dumps(new.__dict__)))
    assert round_tripped == new

    # An OLD payload with none of the new keys must still parse, defaulting to None.
    old = {
        "action": ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
        "order": {"_id": "o1"},
        "truck_id": "t1",
        "shared": True,
        "owner_haulier_id": "acme",
        "carrier_haulier_id": "borax",
        "benefit_km": 10.0,
    }
    parsed = AssignedHaulTripPayload.parse(old)
    assert parsed is not None
    assert parsed.benefit_km == 10.0
    assert parsed.pool_id is None
    assert parsed.awarded_cost_km is None
    assert parsed.owner_reserve_km is None
    assert parsed.market_round is None
