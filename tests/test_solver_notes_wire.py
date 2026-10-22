"""The generic per-pair wire channel ``solver_notes`` (plan_contract_final §7.6, WS3).

What is locked:
1. a tick whose notes carry ONLY the legacy keys (``pool_id``/``market_round``, the
   market's provenance) publishes byte-identical MQTT strings to the pre-channel
   construction, rebuilt here as an independent oracle (the 11 fields in their old
   order, read with the old ``tag.get`` defaults);
2. any other note key lands in ``tag["solver_notes"]`` (audited AND v1) and in the
   published payload, as its LAST key, with zero wire edits;
3. an absent channel is omitted: no ``solver_notes`` key on an unshared pair, on a
   share without extra notes, or in ``to_wire_dict()`` of a default payload; an old
   payload parses to ``None``;
4. the truck agent forwards the notes into ``trip.meta.collaboration`` only when
   present.

Why: a solver must be able to attach any JSON note to a shared pair without anyone
editing the wire, and no shipped run may change a byte while it is possible.
"""

from __future__ import annotations

import json

from apps.container_logistics.assignment import accounting
from apps.container_logistics.message_data_models import AssignedHaulTripPayload
from apps.container_logistics.policies.compat import wire
from apps.container_logistics.statemachine import ContainerLogisticsActions
from apps.container_logistics.truck.app import TruckApp

from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app

_OLD_FIELD_ORDER = ("action", "order", "truck_id", "shared", "owner_haulier_id", "carrier_haulier_id",
                    "benefit_km", "pool_id", "awarded_cost_km", "owner_reserve_km", "market_round")


class _RawMessenger:
    """Records the raw ``(topic, message)`` strings, as the golden harness does."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.client = self

    def publish(self, topic, message):
        self.published.append((topic, message))


def _shared_app():
    """A pooled tick with one cross-company award (t_borax serves o_acme) and one self-serve."""
    trucks = [
        _truck("t_acme", "acme", 103.95 + 0.001, 1.30),
        _truck("t_borax", "borax", 103.85 + 0.01, 1.30),
        _truck("t_far", "acme", 103.85 + 0.10, 1.30),
    ]
    orders = [_order("o_acme", "acme", 103.85, 1.30), _order("o_acme2", "acme", 103.95, 1.30)]
    app = _app(trucks, orders, cooperation=_coop([["acme", "borax"]]))
    app.messenger = _RawMessenger()
    return app


def _old_bytes(app, truck, order, tag):
    """The pre-channel ``publish()`` string for one pair (independent oracle)."""
    values = {
        "action": ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
        "order": app._order_publish_payload(order),
        "truck_id": truck["_id"],
        "shared": bool(tag.get("shared", False)),
        "owner_haulier_id": tag.get("owner_haulier_id"),
        "carrier_haulier_id": tag.get("carrier_haulier_id"),
        "benefit_km": tag.get("benefit_km"),
        "pool_id": tag.get("pool_id"),
        "awarded_cost_km": tag.get("awarded_cost_km"),
        "owner_reserve_km": tag.get("owner_reserve_km"),
        "market_round": tag.get("market_round"),
    }
    return json.dumps({k: values[k] for k in _OLD_FIELD_ORDER}, default=str)


def test_legacy_only_notes_publish_the_old_bytes():
    """Verified to have teeth: emitting ``solver_notes: null`` (``json.dumps(payload.__dict__)``
    instead of ``to_wire_dict()``) and routing ``market_round`` into ``solver_notes``
    (dropping it from ``LEGACY_NOTE_FIELDS``) each failed this test."""
    app = _shared_app()
    matches = app.assign("2020-01-01 08:00:00")
    assert len(matches) == 2
    shares = [k for k, t in app._share_tags.items() if t.get("shared")]
    assert shares == [("t_borax", "o_acme")], "probe: exactly one cross-company award"
    tag = app._share_tags[("t_borax", "o_acme")]
    assert tag["pool_id"] is not None and tag["market_round"] == 1 and "solver_notes" not in tag

    app.publish(matches)
    assert len(app.messenger.published) == 2
    for (topic, message), (truck, order) in zip(app.messenger.published, matches):
        assert topic == f"run_test/{truck['_id']}"
        tag = app._share_tags.get((truck["_id"], order["_id"])) or {}
        assert message == _old_bytes(app, truck, order, tag)


def test_an_extra_note_key_reaches_the_tag_and_the_payload_last():
    """Verified to have teeth: dropping the ``solver_notes`` branch of ``wire_fields``
    failed this test (the tag carried the note, the payload did not)."""
    app = _shared_app()
    matches = app.assign("2020-01-01 08:00:00")
    (acc,) = [a for a in accounting.account_shares(
        matches, candidates=[t for t, _o in matches], cost=lambda t, o: 1.0,
        own_allowed=lambda t, o: False)]
    notes = {(acc.truck_id, acc.order_id): {"pool_id": "p:x", "market_round": 3,
                                            "price": {"bid_km": 1.5}, "rank": 2}}
    app._share_tags = accounting.render_share_tags([acc], fmt="audited", solver_notes=notes)
    tag = app._share_tags[(acc.truck_id, acc.order_id)]
    assert (tag["pool_id"], tag["market_round"]) == ("p:x", 3)
    assert tag["solver_notes"] == {"price": {"bid_km": 1.5}, "rank": 2}
    assert list(tag)[-1] == "solver_notes"

    app.publish(matches)
    by_truck = {json.loads(m)["truck_id"]: json.loads(m) for _t, m in app.messenger.published}
    shared = by_truck[acc.truck_id]
    assert list(shared) == list(_OLD_FIELD_ORDER) + ["solver_notes"]
    assert shared["solver_notes"] == {"price": {"bid_km": 1.5}, "rank": 2}
    assert (shared["pool_id"], shared["market_round"]) == ("p:x", 3)

    # v1 (the default top's format) carries the channel too.
    v1 = accounting.render_share_tags([acc], fmt="v1", solver_notes=notes)[(acc.truck_id, acc.order_id)]
    assert v1["solver_notes"] == {"price": {"bid_km": 1.5}, "rank": 2}
    assert wire.wire_fields(v1)["solver_notes"] == {"price": {"bid_km": 1.5}, "rank": 2}


def test_an_absent_channel_is_omitted_everywhere():
    """Verified to have teeth: ``to_wire_dict`` returning ``dict(self.__dict__)``
    unconditionally failed this test."""
    app = _shared_app()
    matches = app.assign("2020-01-01 08:00:00")
    app.publish(matches)
    for _topic, message in app.messenger.published:
        assert "solver_notes" not in json.loads(message)
    assert "solver_notes" not in wire.wire_fields({})
    assert wire.wire_fields({}) == {"shared": False, **{k: None for k in _OLD_FIELD_ORDER[4:]}}

    default = AssignedHaulTripPayload(action=ContainerLogisticsActions.ASSIGNED_HAUL_TRIP, order={"_id": "o"})
    assert list(default.to_wire_dict()) == list(_OLD_FIELD_ORDER)
    old = {"action": ContainerLogisticsActions.ASSIGNED_HAUL_TRIP, "order": {"_id": "o"}, "truck_id": "t"}
    assert AssignedHaulTripPayload.parse(old).solver_notes is None
    noted = AssignedHaulTripPayload.parse({**old, "shared": True, "solver_notes": {"k": 1}})
    assert noted.solver_notes == {"k": 1}
    assert AssignedHaulTripPayload.parse(json.loads(json.dumps(noted.to_wire_dict()))) == noted


class _TruckManager:
    def get_id(self):
        return "t1"


def _truck_app():
    app = TruckApp.__new__(TruckApp)
    app.manager = _TruckManager()
    app.latest_sim_clock = "2020-01-01 08:00:00"
    app.latest_loc = {"type": "Point", "coordinates": [103.85, 1.30]}
    app.calls = []
    app.handle_assignment = lambda clock, loc, order, collaboration=None: app.calls.append(collaboration)
    return app


def _assigned(**extra):
    return {"action": ContainerLogisticsActions.ASSIGNED_HAUL_TRIP, "order": {"_id": "o1"},
            "truck_id": "t1", "shared": True, "owner_haulier_id": "acme",
            "carrier_haulier_id": "borax", "benefit_km": 1.0, **extra}


def test_truck_forwards_solver_notes_only_when_present():
    """Verified to have teeth: forwarding ``parsed.solver_notes`` unconditionally failed the
    without-notes half (the trip doc gained ``solver_notes: None``)."""
    app = _truck_app()
    app.handle_app_topic_messages(_assigned())
    app.handle_app_topic_messages(_assigned(solver_notes={"rank": 2}))
    app.handle_app_topic_messages({**_assigned(solver_notes={"rank": 2}), "shared": False})
    without, with_notes, unshared = app.calls
    assert "solver_notes" not in without
    assert list(without) == ["shared", "owner_haulier_id", "carrier_haulier_id", "benefit_km", "pool_id",
                             "awarded_cost_km", "owner_reserve_km", "market_round"]
    assert with_notes["solver_notes"] == {"rank": 2}
    assert list(with_notes)[-1] == "solver_notes"
    assert unshared is None, "notes surface only on shared pairs"
