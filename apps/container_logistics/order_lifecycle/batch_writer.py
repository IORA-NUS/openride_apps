"""Direct-Mongo batch writer for the order collection (order-lifecycle service).

This module owns **all** order-document writes during a ``service``-mode run — three
bounded operations per step, whose cost depends on how many *events happened*, never on
how many orders exist:

1. ``apply_events``              — drain of ORDER_* workflow events -> one ``bulk_write``.
2. ``publish_due``               — demand curve: ``created`` -> ``unassigned``, one ``update_many``.
3. ``cancel_overdue_unassigned`` — the relocated ``UNASSIGNED_ORDER_MAX_WAIT_STEPS`` policy,
   open-haul guarded, one ``update_many``.

Design notes
------------
*Derived, never copied.* ``EVENT_TO_STATE``, ``FEASIBLE_TRANSITIONS``, ``ADJACENT`` and
``TERMINAL_STATES`` are all computed at import time from
``statemachine/haultrip_order_interactions.py`` and ``OrderStateMachine`` — the same
declarative sources the truck-side emitter uses. Emitter and applier therefore cannot drift,
and ``state`` (the only field anything downstream reads) is exactly what the api would have
stored.

*Two written-but-unread fields deliberately DIVERGE from the api's format.* ``OrderController``
validates against a machine rebuilt at runtime from the persisted statemachine *definition*
(``get_state_machine(statemachine_id, …)``), not from the ``OrderStateMachine`` class here.
That dynamic machine names its transitions differently and exposes more of them, so the api
writes a longer ``feasible_transitions`` list (entries shaped like
``assign__unassigned__assigned``) and additionally persists the ``transition`` field the
agent PATCHed. This writer instead emits the static class's short event names
(``["assign", "cancel"]``) and never writes ``transition`` at all. That is acceptable, not an
oversight: **nothing in either repo ever reads either field off an order document** (verified
by grep across apps/ and analytics/ — they are write-only bookkeeping), and after this change
nothing Eve-PATCHes an order, so no server-side validation ever re-derives them. If a reader
is ever added, this derivation must be revisited.

*``_etag`` is hygiene only.* After this change nothing Eve-PATCHes an order document, so no
``If-Match`` check is ever performed against these values; they exist purely so a doc read
back through Eve still carries a well-formed etag. They are deliberately cheap tokens, not
Eve-faithful ``sha1(bson.json_util.dumps(doc))`` hashes.

*Datetimes, never strings.* ``sim_clock``/``_updated`` are written as real ``datetime``
objects. The windowed KPI queries (``count_in_window``) use ``$gte``/``$lt`` on these fields
and silently return nothing when they are strings — do **not** copy ``bulk_cancel.py``'s
string ``_updated`` here.

*Idempotent by construction, on two levels.* (1) Once an order's folded state within a drain
is terminal, every later event for that order in the same drain is dropped — otherwise a
same-step ``[delivered, cancelled]`` burst (two haul trips referencing one order, the
documented re-haul class) would fold to ``cancelled`` and silently lose the delivery, because
the stored-state filter cannot see the folded state. (2) Every event write still carries a
terminal guard, so a late event racing a terminal write from an earlier drain matches zero
docs. Together they break the ``ORDER_CANCELLED`` feedback loop without any special-case code.

*One terminal state is deliberately NOT final: the sweep's own cancel.* The overdue sweep is
**advisory demand-shedding**, and a real assignment must trump it. Two operations race across
a step: the sweep reads ``order_ids_with_open_haul()``, then issues its ``update_many``; an
order matched inside that window is cancelled by the sweep while the truck hauls it to
completion, and its later ORDER_* events would then hit a terminal doc and be refused —
preserving a wrong ``cancelled`` (measured at 24/4152 = 0.58% on the full-scale gate). So a
doc in ``cancelled`` **with ``meta.cancel_reason == SWEEP_CANCEL_REASON``** is *resurrectable*:
workflow events apply to it (both the per-op filter and the in-drain fold accept it) and the
stale marker is cleared on the way through, so a resurrected-then-completed order carries no
misleading reason. ``completed`` stays hard-terminal, and ``cancelled`` **without** that marker
stays hard-terminal — that is the ORDER_CANCELLED feedback guard, and it must not regress.
A cancel event arriving for a resurrectable doc is a no-op (it stays cancelled, marker intact)
rather than a pointless resurrect-to-cancelled write.

The race is asymmetric and only needs fixing in this one direction: the sweep's ``update_many``
matches ``state == "unassigned"`` only, so it can never cancel a doc that has already been
moved to ``assigned`` — the reverse ordering is safe by construction.

The planning core (``parse_event`` / ``plan_event_updates``) is pure and unit-testable with
plain dicts — no Mongo, no orsim, no MQTT.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from bson import ObjectId
from pymongo import MongoClient, UpdateOne

from apps.config import kpi_sink_settings
from apps.container_logistics.statemachine import (
    OrderStateMachine,
    haultrip_order_interactions,
)
from apps.utils import str_to_time

logger = logging.getLogger(__name__)

# Eve resource "order" in the container-logistics domain -> this Mongo collection.
ORDER_COLLECTION = "container_logistics_order"

TERMINAL_STATES = (OrderStateMachine.completed.name, OrderStateMachine.cancelled.name)

CREATED_STATE = OrderStateMachine.created.name
UNASSIGNED_STATE = OrderStateMachine.unassigned.name
ASSIGNED_STATE = OrderStateMachine.assigned.name
CANCELLED_STATE = OrderStateMachine.cancelled.name

# Marker this writer stamps on its own demand-shedding sweep cancels (``meta.cancel_reason``).
# It is what makes such a cancel *advisory* rather than final — see ``_resurrectable``.
SWEEP_CANCEL_REASON = "overdue_unassigned"

# event name -> target ORDER state. Derived from the same declarative interaction map the
# truck's ``post_transition_hook`` uses to pick which event to emit.
EVENT_TO_STATE: Dict[str, str] = {
    rule["event"]: rule["target_new_state"]
    for rule in haultrip_order_interactions
    if rule.get("target_statemachine") == OrderStateMachine.__name__
}

# state name -> the events legal from it. Identical to the api's
# ``[t.event for t in machine.current_state.transitions]`` (a machine parked in state S
# exposes exactly ``S.transitions``), without instantiating a machine per state.
FEASIBLE_TRANSITIONS: Dict[str, List[str]] = {
    state.name: [t.event for t in state.transitions] for state in OrderStateMachine.states
}

# state name -> the states directly reachable from it. Used only to *log* non-sequential
# jumps (a dropped MQTT event); the write is applied regardless — see ``plan_event_updates``.
ADJACENT: Dict[str, Set[str]] = {
    state.name: {t.target.name for t in state.transitions} for state in OrderStateMachine.states
}


@dataclass
class ParsedEvent:
    order_id: str
    event: str
    target_state: str
    truck_id: Optional[str]
    sim_clock: datetime


@dataclass
class ApplyStats:
    events_seen: int = 0
    parsed: int = 0
    unknown: int = 0
    orders_touched: int = 0
    applied: int = 0
    skipped_terminal: int = 0
    warned_jumps: int = 0
    # Orders revived from an advisory sweep cancel because a haul actually happened. Expected
    # to be small but non-zero; a run trending upward means the sweep window is too wide.
    resurrected: int = 0


def _resurrectable(state: Optional[str], cancel_reason: Optional[str]) -> bool:
    """True for a doc the *sweep* cancelled — advisory, so a real haul overrides it."""
    return state == CANCELLED_STATE and cancel_reason == SWEEP_CANCEL_REASON


def _current_of(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Normalize a ``current_states`` entry to ``(state, cancel_reason)``.

    Accepts either the bare state name or a mapping carrying ``state`` (+ optional
    ``cancel_reason``), so callers can pass whichever they have.
    """
    if isinstance(value, dict):
        return value.get("state"), value.get("cancel_reason")
    return value, None


def _as_id(value: Any) -> Any:
    """ObjectId when the value is a valid 24-hex id, else the value unchanged."""
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return value


def _etag(*parts: Any) -> str:
    return hashlib.sha1(":".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def parse_event(payload: Any, fallback_sim_clock: datetime) -> Optional[ParsedEvent]:
    """Turn one raw ORDER_* MQTT payload into a :class:`ParsedEvent` (or ``None``).

    Payload shape (emitted by ``TruckTripManager.message_template``)::

        {"action": "order_workflow_event",
         "truck_id": <id>,
         "data": {"event": <event>, "order_id": <id>, "sim_clock": "<RFC 1123>", ...}}

    Returns ``None`` for an unknown event or a payload with no order id — the caller
    counts those as ``unknown`` rather than failing the batch.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    event = data.get("event") or payload.get("event")
    target_state = EVENT_TO_STATE.get(event) if event else None
    if not target_state:
        return None

    order_id = data.get("order_id") or payload.get("order_id")
    if not order_id:
        return None

    truck_id = payload.get("truck_id") or data.get("truck_id")

    # F10: bucket completions by the *event's* sim time, not the applier's step clock.
    raw_clock = data.get("sim_clock")
    sim_clock = fallback_sim_clock
    if isinstance(raw_clock, datetime):
        sim_clock = raw_clock
    elif raw_clock:
        try:
            sim_clock = str_to_time(raw_clock)
        except (TypeError, ValueError):
            sim_clock = fallback_sim_clock

    return ParsedEvent(
        order_id=str(order_id),
        event=str(event),
        target_state=target_state,
        truck_id=None if truck_id is None else str(truck_id),
        sim_clock=sim_clock,
    )


def plan_event_updates(
    events: Sequence[ParsedEvent],
    current_states: Dict[str, str],
    run_id: str,
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], ApplyStats]:
    """Fold a drained event batch into **one** ``$set`` per order id.

    A burst that carries several events for one order in a single drain (e.g. assign +
    pickup_started) collapses to a single ``UpdateOne``: the last event's state/sim_clock
    win, while the assign's ``truck`` is retained.

    Returns ``(ops, stats)`` where each op is a ``(filter, set_doc)`` pair. Nothing here
    touches Mongo.
    """
    stats = ApplyStats(parsed=len(events))
    merged: Dict[str, Dict[str, Any]] = {}
    order_seq: List[str] = []
    resurrected: Set[str] = set()
    # Running per-order (state, cancel_reason) so an in-batch sequence is validated against
    # what the *previous* event in this same batch left behind, not the stale DB read.
    effective: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    for ev in events:
        oid = ev.order_id
        if oid not in effective:
            effective[oid] = _current_of(current_states.get(oid))

        known, known_reason = effective[oid]
        reviving = _resurrectable(known, known_reason)

        if reviving and ev.target_state == CANCELLED_STATE:
            # Already cancelled and staying cancelled: leave the doc (and its sweep marker)
            # exactly as-is rather than writing a pointless resurrect-to-cancelled update.
            stats.skipped_terminal += 1
            continue

        if not reviving and known in TERMINAL_STATES:
            # Hard terminal — either already terminal in Mongo, or made terminal by an EARLIER
            # event in this same drain. Drop the event entirely: folding it would overwrite the
            # terminal state in the merged $set, and the stored-state filter could not catch
            # that. Nothing is written for a hard-terminal doc.
            stats.skipped_terminal += 1
            continue

        if reviving:
            if oid not in resurrected:
                resurrected.add(oid)
                stats.resurrected += 1
                logger.info(
                    "OrderBatchWriter: resurrecting order=%s from an advisory sweep cancel "
                    "(event %s -> %s); the haul really happened.",
                    oid,
                    ev.event,
                    ev.target_state,
                )
            # A resurrection is a legitimate jump out of `cancelled`, not a dropped event.
        elif known is not None and ev.target_state != known and ev.target_state not in ADJACENT.get(known, ()):
            # Self-healing (§3.8): a dropped intermediate event shows up as a jump. We log it
            # so drop rates stay visible, then apply the target state anyway.
            logger.warning(
                "OrderBatchWriter: non-sequential order transition order=%s %s -> %s (event %s); "
                "applying anyway (likely a dropped intermediate event).",
                oid,
                known,
                ev.target_state,
                ev.event,
            )
            stats.warned_jumps += 1

        if oid not in merged:
            merged[oid] = {}
            order_seq.append(oid)
        slot = merged[oid]
        slot["state"] = ev.target_state
        slot["sim_clock"] = ev.sim_clock
        slot["_updated"] = ev.sim_clock
        slot["feasible_transitions"] = list(FEASIBLE_TRANSITIONS.get(ev.target_state, []))
        slot["_etag"] = _etag(oid, ev.target_state, ev.sim_clock.isoformat())
        if ev.target_state == ASSIGNED_STATE and ev.truck_id:
            slot["truck"] = _as_id(ev.truck_id)
        if oid in resurrected:
            # Clear the stale advisory marker (dot-path, so meta.request_time_step survives),
            # otherwise a resurrected-then-completed order reads "cancelled: overdue_unassigned".
            slot["meta.cancel_reason"] = None

        # Marker cleared above, so from here on the doc is an ordinary non-terminal order.
        effective[oid] = (ev.target_state, None)

    ops: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for oid in order_seq:
        ops.append(
            (
                {
                    "_id": _as_id(oid),
                    "run_id": run_id,
                    # Hard-terminal guard, with the one advisory exception: a doc the sweep
                    # cancelled is still writable, because a real haul outranks demand-shedding.
                    "$or": [
                        {"state": {"$nin": list(TERMINAL_STATES)}},
                        {
                            "state": CANCELLED_STATE,
                            "meta.cancel_reason": SWEEP_CANCEL_REASON,
                        },
                    ],
                },
                merged[oid],
            )
        )
    stats.orders_touched = len(ops)
    return ops, stats


def _mongo_client() -> MongoClient:
    """Same construction as ``order/bulk_cancel.py``, plus ``connect=False``.

    ``connect=False`` defers socket creation to first use, which is what keeps the client
    safe to build inside an eventlet-driven celery worker.
    """
    uri = kpi_sink_settings.get("mongo_uri")
    if uri:
        return MongoClient(uri, connect=False)
    return MongoClient(
        kpi_sink_settings["mongo_host"],
        int(kpi_sink_settings["mongo_port"]),
        connect=False,
    )


class OrderBatchWriter:
    """Thin impure shell around the pure planners above."""

    def __init__(self, run_id: str, collection=None, haulier_filter: Optional[Iterable[str]] = None):
        self.run_id = run_id
        self.haulier_filter = list(haulier_filter) if haulier_filter else None
        self._injected_collection = collection
        self._client: Optional[MongoClient] = None
        self._collection = None

    # -- plumbing ---------------------------------------------------------------

    @property
    def collection(self):
        if self._injected_collection is not None:
            return self._injected_collection
        if self._collection is None:
            self._client = _mongo_client()
            self._collection = self._client[kpi_sink_settings["mongo_db"]][ORDER_COLLECTION]
            self.ensure_indexes()
        return self._collection

    def ensure_indexes(self) -> None:
        """Declare the indexes this writer's own queries depend on.

        These were previously undeclared — created by hand against a live database, which the
        `/gate-run` skill still documents as a manual step for a fresh DB. Declaring them here
        makes them reproducible and keeps them next to the queries that need them.

        `PUBLISH_DUE_INDEX` is the one that was missing. `publish_due()` filters on
        ``{run_id, state, meta.request_time_step}`` but the closest existing index stopped at
        ``{run_id, state}``, so Mongo fetched EVERY order in the run still sitting in `created`
        and filtered the request step in memory — once per step, for every step of the run.
        Measured 2026-08-16: 3,273 docs examined to return 1,961 on a 5k-order scenario, and
        30,000 docs examined per step on the 30k-order consortium scenario.

        Never fatal: an index failure must not stop a run. `create_index` is idempotent, so
        calling this on every writer construction is cheap once the index exists.
        """
        try:
            self._collection.create_index(
                [("run_id", 1), ("state", 1), ("meta.request_time_step", 1)],
                name="run_id_state_request_step",
                background=True,
            )
        except Exception:
            logger.warning(
                "OrderBatchWriter: could not ensure run_id_state_request_step index; "
                "publish_due will fall back to an in-memory filter",
                exc_info=True,
            )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
                self._collection = None

    def _scope(self, base: Dict[str, Any]) -> Dict[str, Any]:
        """Add the optional per-haulier partition clause (§3.1 extension seam)."""
        if self.haulier_filter:
            base = dict(base)
            base["profile.haulier_id"] = {"$in": list(self.haulier_filter)}
        return base

    # -- op 1: workflow events --------------------------------------------------

    def apply_events(self, raw_payloads: Sequence[Any], fallback_sim_clock: datetime) -> ApplyStats:
        events: List[ParsedEvent] = []
        for payload in raw_payloads:
            parsed = parse_event(payload, fallback_sim_clock)
            if parsed is not None:
                events.append(parsed)

        if not events:
            stats = ApplyStats(events_seen=len(raw_payloads))
            stats.unknown = len(raw_payloads)
            return stats

        ids = [_as_id(ev.order_id) for ev in events]
        current_states: Dict[str, Dict[str, Any]] = {}
        try:
            # cancel_reason is projected too: it is what distinguishes an advisory sweep
            # cancel (resurrectable) from a real terminal cancel.
            for doc in self.collection.find(
                {"_id": {"$in": ids}}, {"state": 1, "meta.cancel_reason": 1}
            ):
                current_states[str(doc.get("_id"))] = {
                    "state": doc.get("state"),
                    "cancel_reason": ((doc.get("meta") or {}).get("cancel_reason")),
                }
        except Exception:
            logger.exception("OrderBatchWriter: current-state read failed; applying without state context")

        ops, stats = plan_event_updates(events, current_states, self.run_id)
        stats.events_seen = len(raw_payloads)
        stats.unknown = stats.events_seen - stats.parsed

        if ops:
            result = self.collection.bulk_write(
                [UpdateOne(f, {"$set": s}) for f, s in ops], ordered=False
            )
            stats.applied = int(getattr(result, "modified_count", 0) or 0)
        return stats

    # -- op 2: demand curve -----------------------------------------------------

    def publish_due(self, step: int, sim_clock: datetime) -> int:
        """``created`` -> ``unassigned`` for every order whose request step has arrived."""
        result = self.collection.update_many(
            self._scope(
                {
                    "run_id": self.run_id,
                    "state": CREATED_STATE,
                    "meta.request_time_step": {"$lte": step},
                }
            ),
            {
                "$set": {
                    "state": UNASSIGNED_STATE,
                    "feasible_transitions": list(FEASIBLE_TRANSITIONS.get(UNASSIGNED_STATE, [])),
                    "sim_clock": sim_clock,
                    "_updated": sim_clock,
                    "_etag": _etag(self.run_id, "publish", step),
                }
            },
        )
        return int(getattr(result, "modified_count", 0) or 0)

    # -- op 3: overdue sweep ----------------------------------------------------

    def cancel_overdue_unassigned(
        self,
        step: int,
        max_wait_steps: int,
        open_haul_order_ids: Iterable[Any],
        sim_clock: datetime,
    ) -> int:
        """Cancel overdue ``unassigned`` demand, excluding anything with an open haul.

        The open-haul exclusion (F8) is the structural fix for the ~6% "order cancelled but
        the truck still completed the haul" race: an order matched right at its deadline is
        already referenced by a non-terminal haul trip, so it can never be swept.
        """
        excluded = [_as_id(x) for x in open_haul_order_ids if x]
        result = self.collection.update_many(
            self._scope(
                {
                    "run_id": self.run_id,
                    "state": UNASSIGNED_STATE,
                    "meta.request_time_step": {"$lt": step - max_wait_steps},
                    "_id": {"$nin": excluded},
                }
            ),
            {
                "$set": {
                    "state": CANCELLED_STATE,
                    "feasible_transitions": [],
                    "sim_clock": sim_clock,
                    "_updated": sim_clock,
                    # Dot-path so meta.request_time_step survives (a whole-`meta` $set would
                    # drop it and break every later publish/sweep query for this doc).
                    # Marks this cancel as ADVISORY: if a haul for the order turns out to have
                    # been in flight, ``apply_events`` resurrects the doc on the marker.
                    "meta.cancel_reason": SWEEP_CANCEL_REASON,
                    "_etag": _etag(self.run_id, "sweep", step),
                }
            },
        )
        return int(getattr(result, "modified_count", 0) or 0)
