"""OrderBatchWriter: the three Mongo ops + the derived state metadata they depend on.

Pins the derivation (nothing is a hand-copied literal), the fold/idempotency semantics that
make batched writes safe, and the sweep guard that removes the ~6% cancel race.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from bson import ObjectId

from apps.container_logistics.order_lifecycle.batch_writer import (
    ADJACENT,
    EVENT_TO_STATE,
    FEASIBLE_TRANSITIONS,
    TERMINAL_STATES,
    ApplyStats,
    OrderBatchWriter,
    parse_event,
    plan_event_updates,
)
from apps.container_logistics.statemachine import OrderStateMachine
from apps.utils import time_to_str
from tests.fake_mongo import FakeCollection

RUN_ID = "run_test"
NOW = datetime(2026, 7, 29, 10, 0, 0)
LATER = datetime(2026, 7, 29, 11, 30, 0)
HEX40 = re.compile(r"^[0-9a-f]{40}$")


def oid(n: int) -> str:
    return f"{n:024x}"


def event_payload(event, order_id, truck_id=None, sim_clock=None):
    data = {"event": event, "order_id": order_id}
    if sim_clock is not None:
        data["sim_clock"] = sim_clock
    return {"action": "order_workflow_event", "truck_id": truck_id, "data": data}


# --------------------------------------------------------------------------- derivation


def test_event_to_state_is_derived_from_the_interaction_map():
    assert EVENT_TO_STATE == {
        "order_assigned_to_truck": "assigned",
        "order_pickup_started": "pickup_in_progress",
        "order_pickup_completed": "in_transit",
        "order_dropoff_started": "dropoff_in_progress",
        "order_delivered": "completed",
        "order_cancelled": "cancelled",
    }


def test_feasible_transitions_match_the_state_machine_and_terminals_are_empty():
    # Identical to the api's OrderController: [t.event for t in machine.current_state.transitions].
    assert FEASIBLE_TRANSITIONS["created"] == ["publish", "cancel"]
    assert FEASIBLE_TRANSITIONS["unassigned"] == ["assign", "cancel"]
    assert FEASIBLE_TRANSITIONS["assigned"] == ["pickup_started", "cancel"]
    assert FEASIBLE_TRANSITIONS["dropoff_in_progress"] == ["deliver", "cancel"]
    for terminal in TERMINAL_STATES:
        assert FEASIBLE_TRANSITIONS[terminal] == []
    assert set(FEASIBLE_TRANSITIONS) == {s.name for s in OrderStateMachine.states}


def test_terminal_states_and_adjacency_are_derived():
    assert TERMINAL_STATES == (OrderStateMachine.completed.name, OrderStateMachine.cancelled.name)
    assert ADJACENT["unassigned"] == {"assigned", "cancelled"}
    assert ADJACENT["completed"] == set()


# --------------------------------------------------------------------------- parse_event


def test_parse_event_prefers_the_payload_sim_clock():
    ev = parse_event(
        event_payload("order_delivered", oid(1), truck_id=oid(9), sim_clock=time_to_str(LATER)),
        NOW,
    )
    assert (ev.order_id, ev.event, ev.target_state, ev.truck_id) == (
        oid(1),
        "order_delivered",
        "completed",
        oid(9),
    )
    assert ev.sim_clock == LATER


@pytest.mark.parametrize("garbled", ["not a date", "", None])
def test_parse_event_falls_back_to_the_step_clock(garbled):
    ev = parse_event(event_payload("order_delivered", oid(1), sim_clock=garbled), NOW)
    assert ev.sim_clock == NOW
    assert isinstance(ev.sim_clock, datetime)


def test_parse_event_rejects_unknown_events_and_missing_order_ids():
    assert parse_event(event_payload("truck_arrived_pickup_queue", oid(1)), NOW) is None
    assert parse_event(event_payload("order_delivered", None), NOW) is None
    assert parse_event({"data": {}}, NOW) is None
    assert parse_event("garbage", NOW) is None


def test_parse_event_reads_a_top_level_truck_id():
    ev = parse_event(
        {"data": {"event": "order_assigned_to_truck", "order_id": oid(1)}, "truck_id": oid(7)},
        NOW,
    )
    assert ev.truck_id == oid(7)


# --------------------------------------------------------------------------- plan/apply


def test_assign_writes_state_truck_datetimes_feasible_and_etag():
    coll = FakeCollection([{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "unassigned"}])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9))], NOW
    )

    assert (stats.events_seen, stats.parsed, stats.unknown) == (1, 1, 0)
    assert (stats.orders_touched, stats.applied, stats.warned_jumps) == (1, 1, 0)
    doc = coll.docs[ObjectId(oid(1))]
    assert doc["state"] == "assigned"
    assert doc["truck"] == ObjectId(oid(9))
    assert doc["sim_clock"] == NOW and doc["_updated"] == NOW
    assert isinstance(doc["sim_clock"], datetime) and isinstance(doc["_updated"], datetime)
    assert doc["feasible_transitions"] == ["pickup_started", "cancel"]
    assert HEX40.match(doc["_etag"])
    # No `transition` field is ever written.
    assert "transition" not in doc


def test_in_batch_burst_folds_to_one_update_keeping_the_assigned_truck():
    coll = FakeCollection([{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "unassigned"}])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9)),
            event_payload("order_pickup_started", oid(1), truck_id=oid(9), sim_clock=time_to_str(LATER)),
        ],
        NOW,
    )

    assert stats.parsed == 2
    assert stats.orders_touched == 1
    assert stats.warned_jumps == 0
    assert len(coll.bulk_write_calls) == 1 and len(coll.bulk_write_calls[0]) == 1
    doc = coll.docs[ObjectId(oid(1))]
    assert doc["state"] == "pickup_in_progress"          # LAST event wins
    assert doc["truck"] == ObjectId(oid(9))              # assign's truck retained
    assert doc["sim_clock"] == LATER                     # LAST event's clock wins


def test_out_of_order_events_self_heal_and_warn_once():
    # A dropped ORDER_PICKUP_STARTED: assign then pickup_completed.
    coll = FakeCollection([{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "unassigned"}])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9)),
            event_payload("order_pickup_completed", oid(1)),
        ],
        NOW,
    )

    assert stats.warned_jumps == 1
    assert coll.docs[ObjectId(oid(1))]["state"] == "in_transit"  # applied anyway


def test_terminal_documents_are_idempotent_no_ops():
    coll = FakeCollection(
        [
            {"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "completed"},
            {"_id": ObjectId(oid(2)), "run_id": RUN_ID, "state": "cancelled"},
        ]
    )
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_cancelled", oid(1)),   # late cancel vs a completed order
            event_payload("order_cancelled", oid(2)),   # cancel feedback vs a cancelled order
        ],
        NOW,
    )

    assert stats.skipped_terminal == 2
    assert stats.applied == 0
    assert coll.docs[ObjectId(oid(1))]["state"] == "completed"
    assert coll.docs[ObjectId(oid(2))]["state"] == "cancelled"


def test_unknown_payloads_are_counted_not_applied():
    coll = FakeCollection([{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "unassigned"}])
    writer = OrderBatchWriter(RUN_ID, collection=coll)
    stats = writer.apply_events(
        [event_payload("truck_arrived_pickup_queue", oid(1)), {"nonsense": True}], NOW
    )
    assert (stats.events_seen, stats.parsed, stats.unknown, stats.applied) == (2, 0, 2, 0)
    assert coll.bulk_write_calls == []


def test_plan_event_updates_filters_are_run_scoped_and_terminal_guarded():
    from apps.container_logistics.order_lifecycle.batch_writer import ParsedEvent

    ops, stats = plan_event_updates(
        [ParsedEvent(oid(1), "order_delivered", "completed", None, NOW)], {}, RUN_ID
    )
    (flt, set_doc), = ops
    assert flt == {
        "_id": ObjectId(oid(1)),
        "run_id": RUN_ID,
        "$or": [
            {"state": {"$nin": ["completed", "cancelled"]}},
            # ...plus the one advisory exception (see the resurrection tests below).
            {"state": "cancelled", "meta.cancel_reason": "overdue_unassigned"},
        ],
    }
    assert set_doc["state"] == "completed"
    assert isinstance(stats, ApplyStats)
    # Unknown current state => no jump warning.
    assert stats.warned_jumps == 0


# --------------------------------------------------------------------------- publish_due


def _created(n, step, haulier="acme"):
    return {
        "_id": ObjectId(oid(n)),
        "run_id": RUN_ID,
        "state": "created",
        "profile": {"haulier_id": haulier},
        "meta": {"request_time_step": step},
    }


def test_publish_due_releases_only_orders_whose_step_has_arrived():
    coll = FakeCollection([_created(1, 5), _created(2, 10), _created(3, 20)])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    assert writer.publish_due(10, NOW) == 2

    assert coll.docs[ObjectId(oid(1))]["state"] == "unassigned"
    assert coll.docs[ObjectId(oid(2))]["state"] == "unassigned"
    assert coll.docs[ObjectId(oid(3))]["state"] == "created"
    published = coll.docs[ObjectId(oid(1))]
    assert published["feasible_transitions"] == ["assign", "cancel"]
    assert published["sim_clock"] == NOW and published["_updated"] == NOW
    assert HEX40.match(published["_etag"])
    assert published["meta"]["request_time_step"] == 5  # preserved


def test_publish_due_respects_the_haulier_filter():
    coll = FakeCollection([_created(1, 5, "acme"), _created(2, 5, "borax")])
    writer = OrderBatchWriter(RUN_ID, collection=coll, haulier_filter=["acme"])

    assert writer.publish_due(10, NOW) == 1
    assert coll.docs[ObjectId(oid(1))]["state"] == "unassigned"
    assert coll.docs[ObjectId(oid(2))]["state"] == "created"


# --------------------------------------------------------------------------- sweep


def _unassigned(n, step):
    doc = _created(n, step)
    doc["state"] = "unassigned"
    return doc


def test_sweep_is_strictly_overdue_open_haul_guarded_and_preserves_meta():
    # step=100, max_wait=30 => strictly older than step 70 is overdue.
    coll = FakeCollection([_unassigned(1, 60), _unassigned(2, 70), _unassigned(3, 80), _unassigned(4, 10)])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    swept = writer.cancel_overdue_unassigned(100, 30, {oid(4)}, LATER)

    assert swept == 1                                             # only #1
    cancelled = coll.docs[ObjectId(oid(1))]
    assert cancelled["state"] == "cancelled"
    assert cancelled["feasible_transitions"] == []
    assert cancelled["meta"]["cancel_reason"] == "overdue_unassigned"
    assert cancelled["meta"]["request_time_step"] == 60           # dot-path preserved it
    assert cancelled["sim_clock"] == LATER and isinstance(cancelled["_updated"], datetime)
    assert HEX40.match(cancelled["_etag"])
    assert coll.docs[ObjectId(oid(2))]["state"] == "unassigned"   # boundary: not strictly older
    assert coll.docs[ObjectId(oid(3))]["state"] == "unassigned"
    assert coll.docs[ObjectId(oid(4))]["state"] == "unassigned"   # open haul => never swept


def test_sweep_respects_the_haulier_filter():
    a, b = _unassigned(1, 10), _unassigned(2, 10)
    b["profile"]["haulier_id"] = "borax"
    coll = FakeCollection([a, b])
    writer = OrderBatchWriter(RUN_ID, collection=coll, haulier_filter=["acme"])
    assert writer.cancel_overdue_unassigned(100, 30, set(), NOW) == 1
    assert coll.docs[ObjectId(oid(2))]["state"] == "unassigned"


# --------------------------------------------------------------------------- in-drain terminal


def test_a_terminal_state_reached_mid_drain_swallows_later_events():
    """Repro: two haul trips referencing one order in one step (the re-haul class).

    Both a delivery and a cancellation arrive in the SAME drain. The $nin filter can only see
    the *stored* state (still dropoff_in_progress), so without an in-fold guard the merged
    $set would end at ``cancelled`` and the delivery would be silently lost.
    """
    coll = FakeCollection(
        [{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "dropoff_in_progress"}]
    )
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_delivered", oid(1)),
            event_payload("order_cancelled", oid(1)),
        ],
        NOW,
    )

    assert coll.docs[ObjectId(oid(1))]["state"] == "completed"
    assert coll.docs[ObjectId(oid(1))]["feasible_transitions"] == []
    assert stats.skipped_terminal == 1
    assert stats.orders_touched == 1


def test_a_cancel_mid_drain_is_not_undone_by_a_later_assign():
    coll = FakeCollection([{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "unassigned"}])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_cancelled", oid(1)),
            event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9)),
        ],
        NOW,
    )

    doc = coll.docs[ObjectId(oid(1))]
    assert doc["state"] == "cancelled"
    assert "truck" not in doc          # the swallowed assign left nothing behind
    assert stats.skipped_terminal == 1


def test_events_for_a_stored_terminal_doc_write_nothing_at_all():
    coll = FakeCollection([{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "completed"}])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events([event_payload("order_cancelled", oid(1))], NOW)

    assert stats.skipped_terminal == 1
    assert stats.orders_touched == 0
    assert coll.bulk_write_calls == []   # no pointless no-op UpdateOne is emitted


def test_a_terminal_order_does_not_suppress_other_orders_in_the_same_drain():
    coll = FakeCollection(
        [
            {"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "completed"},
            {"_id": ObjectId(oid(2)), "run_id": RUN_ID, "state": "unassigned"},
        ]
    )
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_cancelled", oid(1)),
            event_payload("order_assigned_to_truck", oid(2), truck_id=oid(9)),
        ],
        NOW,
    )

    assert stats.orders_touched == 1 and stats.applied == 1
    assert coll.docs[ObjectId(oid(2))]["state"] == "assigned"


# --------------------------------------------------------------------------- sweep resurrection


def _sweep_cancelled(n, request_step=10):
    """A doc exactly as ``cancel_overdue_unassigned`` leaves it."""
    return {
        "_id": ObjectId(oid(n)),
        "run_id": RUN_ID,
        "state": "cancelled",
        "feasible_transitions": [],
        "profile": {"haulier_id": "acme"},
        "meta": {"request_time_step": request_step, "cancel_reason": "overdue_unassigned"},
    }


def test_sweep_cancelled_order_is_resurrected_by_the_haul_it_actually_got():
    """The 0.58% residual race: assignment lands between the sweep's open-haul read and its
    update_many, so a hauled order gets cancelled. The haul's events must win."""
    coll = FakeCollection([_sweep_cancelled(1)])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    first = writer.apply_events(
        [event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9))], NOW
    )
    assert first.resurrected == 1
    assert first.applied == 1
    doc = coll.docs[ObjectId(oid(1))]
    assert doc["state"] == "assigned"
    assert doc["truck"] == ObjectId(oid(9))
    assert doc["meta"]["cancel_reason"] is None          # stale marker cleared
    assert doc["meta"]["request_time_step"] == 10        # dot-path preserved the rest of meta

    # ...and the rest of the lifecycle proceeds normally in later drains.
    for event in ("order_pickup_started", "order_pickup_completed", "order_dropoff_started"):
        writer.apply_events([event_payload(event, oid(1))], NOW)
    last = writer.apply_events(
        [event_payload("order_delivered", oid(1), sim_clock=time_to_str(LATER))], NOW
    )

    doc = coll.docs[ObjectId(oid(1))]
    assert doc["state"] == "completed"
    assert doc["meta"]["cancel_reason"] is None
    assert doc["sim_clock"] == LATER
    assert last.resurrected == 0                          # only the first drain resurrected


def test_a_late_cancel_leaves_a_sweep_cancelled_order_exactly_as_it_was():
    coll = FakeCollection([_sweep_cancelled(1)])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events([event_payload("order_cancelled", oid(1))], NOW)

    assert stats.skipped_terminal == 1
    assert stats.resurrected == 0
    assert stats.orders_touched == 0
    assert coll.bulk_write_calls == []                     # no resurrect-to-cancelled write
    doc = coll.docs[ObjectId(oid(1))]
    assert doc["state"] == "cancelled"
    assert doc["meta"]["cancel_reason"] == "overdue_unassigned"   # marker intact


def test_a_plain_cancel_is_still_hard_terminal_feedback_guard_intact():
    """ORDER_CANCELLED feedback (haul cancel -> order cancel -> haul cancel) must not resurrect."""
    doc = _sweep_cancelled(1)
    doc["meta"].pop("cancel_reason")                       # cancelled, but not BY the sweep
    coll = FakeCollection([doc])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9))], NOW
    )

    assert stats.skipped_terminal == 1 and stats.resurrected == 0
    assert coll.bulk_write_calls == []
    assert coll.docs[ObjectId(oid(1))]["state"] == "cancelled"


def test_a_completed_order_is_still_hard_terminal():
    coll = FakeCollection(
        [{"_id": ObjectId(oid(1)), "run_id": RUN_ID, "state": "completed", "meta": {}}]
    )
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    for event in ("order_assigned_to_truck", "order_cancelled", "order_delivered"):
        stats = writer.apply_events([event_payload(event, oid(1), truck_id=oid(9))], NOW)
        assert stats.skipped_terminal == 1 and stats.resurrected == 0

    assert coll.bulk_write_calls == []
    assert coll.docs[ObjectId(oid(1))]["state"] == "completed"


def test_resurrection_and_completion_fold_into_one_update_in_a_single_drain():
    coll = FakeCollection([_sweep_cancelled(1)])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9)),
            event_payload("order_delivered", oid(1)),
        ],
        NOW,
    )

    assert len(coll.bulk_write_calls) == 1 and len(coll.bulk_write_calls[0]) == 1
    assert stats.resurrected == 1 and stats.orders_touched == 1
    doc = coll.docs[ObjectId(oid(1))]
    assert doc["state"] == "completed"
    assert doc["truck"] == ObjectId(oid(9))               # assign's truck retained
    assert doc["meta"]["cancel_reason"] is None
    assert doc["meta"]["request_time_step"] == 10


def test_a_resurrected_order_cannot_be_re_cancelled_later_in_the_same_drain():
    """Once revived and delivered, the ORDER_CANCELLED feedback guard applies again."""
    coll = FakeCollection([_sweep_cancelled(1)])
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    stats = writer.apply_events(
        [
            event_payload("order_assigned_to_truck", oid(1), truck_id=oid(9)),
            event_payload("order_delivered", oid(1)),
            event_payload("order_cancelled", oid(1)),
        ],
        NOW,
    )

    assert coll.docs[ObjectId(oid(1))]["state"] == "completed"
    assert stats.skipped_terminal == 1


def test_sweep_never_cancels_an_already_assigned_order():
    """The other direction of the race is safe by construction: the sweep matches
    state == 'unassigned' only."""
    coll = FakeCollection(
        [
            {
                "_id": ObjectId(oid(1)),
                "run_id": RUN_ID,
                "state": "assigned",
                "profile": {"haulier_id": "acme"},
                "meta": {"request_time_step": 1},
            }
        ]
    )
    writer = OrderBatchWriter(RUN_ID, collection=coll)

    assert writer.cancel_overdue_unassigned(1000, 30, set(), NOW) == 0
    assert coll.docs[ObjectId(oid(1))]["state"] == "assigned"
