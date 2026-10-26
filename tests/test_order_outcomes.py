"""Order outcomes as SIM KPIs (analytics/order_outcomes.py).

Definitions, classification precedence, the day-end boundary, owner attribution, the
str-vs-ObjectId join, the live refresh cadence, and the bulk-cancel marker's interaction
with the order-lifecycle writer's resurrection rule. No Mongo: the reader is driven with a
tiny in-memory fake that implements exactly the queries it issues.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from apps.container_logistics.analytics import order_outcomes as oo
from apps.container_logistics.analytics.manager import AnalyticsManager
from apps.container_logistics.order.bulk_cancel import HORIZON_CANCEL_REASON
from apps.container_logistics.order_lifecycle import batch_writer as bw

REF = datetime(2020, 1, 1, 8, 0, 0)
STEP = 240
H = 2520 * STEP  # 7 whole days
DAY = 86400


def _doc(state, *, rts=0, done_at=None, reason=None, truck=None, hid="h1", hn="H One", _id=None,
         clock=None):
    meta = {"request_time_step": rts}
    if reason is not None:
        meta["cancel_reason"] = None if reason == "__none__" else reason
    d = {
        "_id": _id or ObjectId(),
        "profile": {"haulier_id": hid, "haulier_name": hn},
        "meta": meta,
        "state": state,
    }
    if truck is not None:
        d["truck"] = truck
    if done_at is not None:
        d["sim_clock"] = REF + timedelta(seconds=done_at)
    if clock is not None:
        d["sim_clock"] = clock
    return d


def _fold(docs, *, final=True, closed=None, hauls=(), horizon_s=H):
    ev = oo._haul_evidence(hauls, reference_time=REF, horizon_s=horizon_s)
    recs = [
        oo.record_from_doc(d, haul=ev.get(str(d["_id"])), reference_time=REF,
                           step_interval=STEP, horizon_s=horizon_s)
        for d in docs
    ]
    return oo.fold_outcomes(recs, horizon_s=horizon_s, step_interval=STEP, reference_time=REF,
                            final=final, closed=closed)


# ------------------------------------------------------------------ classify


def test_classify_precedence_and_every_reason():
    c = oo.classify
    assert c(state="completed") is None
    assert c(state="assigned", has_truck=True) is None
    # haul/truck evidence outranks every marker
    assert c(state="cancelled", cancel_reason=bw.SWEEP_CANCEL_REASON, has_truck=True) == "cancelled_in_progress"
    assert c(state="cancelled", cancel_reason=HORIZON_CANCEL_REASON, has_haul=True) == "cancelled_in_progress"
    assert c(state="cancelled", cancel_reason=bw.SWEEP_CANCEL_REASON) == "overdue_unassigned"
    assert c(state="cancelled", cancel_reason=HORIZON_CANCEL_REASON) == "unserved_at_horizon"
    assert c(state="cancelled") == "other"
    # a resurrected doc carries cancel_reason None: never "overdue"
    assert c(state="cancelled", cancel_reason=None) == "other"
    assert oo.FAILURE_REASONS == (
        "cancelled_in_progress", "overdue_unassigned", "unserved_at_horizon", "other")


def test_sub_counts_started_and_at_horizon():
    late = REF + timedelta(seconds=H + 6 * 3600)
    a, b, c = ObjectId(), ObjectId(), ObjectId()
    docs = [
        # bulk-cancelled while assigned: marker + truck -> in_progress, at_horizon, not started
        _doc("cancelled", rts=2000, truck=ObjectId(), reason=HORIZON_CANCEL_REASON, _id=a),
        # haul cancelled at truck close, order has no truck field: haul evidence
        _doc("cancelled", rts=2000, _id=b, clock=REF + timedelta(seconds=10)),
        # cancelled mid-run on a started haul (not at horizon)
        _doc("cancelled", rts=10, truck=ObjectId(), _id=c, clock=REF + timedelta(seconds=5000)),
    ]
    hauls = [
        {"order": str(b), "state": "cancelled", "sim_clock": late, "stats": {"loaded_started_at": 1}},
        {"order": str(c), "state": "cancelled", "sim_clock": REF + timedelta(seconds=5000),
         "stats": {"reposition_started_at": "x"}},
    ]
    out = _fold(docs, hauls=hauls)
    t = out["totals"]
    assert t["failed"]["cancelled_in_progress"] == 3
    assert t["cancelled_in_progress_started"] == 2
    assert t["cancelled_in_progress_at_horizon"] == 2  # a (marker) + b (haul clock)
    assert out["invariants_ok"], out["invariant_errors"]


def test_completed_haul_is_not_in_progress_evidence():
    oid = ObjectId()
    out = _fold([_doc("cancelled", _id=oid, reason=bw.SWEEP_CANCEL_REASON)],
                hauls=[{"order": str(oid), "state": "completed", "stats": {}}])
    assert out["totals"]["failed"]["overdue_unassigned"] == 1


# ------------------------------------------------------------------ fold / boundaries


def test_fold_invariants_and_overlap_rule():
    docs = [
        _doc("completed", rts=0, done_at=100),                   # d0 same day
        _doc("completed", rts=0, done_at=DAY + 5),                # d0 later
        _doc("cancelled", rts=0, reason=bw.SWEEP_CANCEL_REASON),  # d0 failed
        _doc("assigned", rts=10, truck=ObjectId()),               # d0 open
        _doc("created", rts=400),                                 # not released
    ]
    out = _fold(docs, final=False, closed=1)
    d0 = out["days"]["d0"]
    assert d0 == {"released": 4, "done_same_day": 1, "done_later": 1, "undone_at_day_end": 3,
                  "open": 1, "failed": {"total": 1, "cancelled_in_progress": 0,
                                        "overdue_unassigned": 1, "unserved_at_horizon": 0, "other": 0}}
    # failed is a SUBSET of undone: undone = later + failed + open
    assert d0["undone_at_day_end"] == d0["done_later"] + d0["failed"]["total"] + d0["open"]
    assert out["invariants_ok"]
    assert out["final"] is False and out["closed_days"] == 1


def test_strict_less_than_at_exact_day_end():
    """A completion at exactly (d+1)*86400 is LATER (a `<=` mutant is killed here)."""
    on = _fold([_doc("completed", rts=0, done_at=DAY)])["totals"]
    just_before = _fold([_doc("completed", rts=0, done_at=DAY - 1)])["totals"]
    assert (on["done_same_day"], on["done_later"]) == (0, 1)
    assert (just_before["done_same_day"], just_before["done_later"]) == (1, 0)


def test_partial_last_day_deadline_is_the_horizon():
    h = 6.5 * DAY
    rts = int(6.2 * DAY / STEP)  # released on day 6, which ends at H (not 7*86400)
    before_h = _fold([_doc("completed", rts=rts, done_at=h - 60)], horizon_s=h)
    after_h = _fold([_doc("completed", rts=rts, done_at=h + 60)], horizon_s=h)
    assert before_h["totals"]["done_same_day"] == 1
    assert after_h["totals"]["done_later"] == 1  # post-horizon drain
    assert before_h["days_count"] == 7 and before_h["day_seconds_last"] == int(0.5 * DAY)


def test_post_horizon_completion_counts_as_done_later():
    rts = 2500  # day 6 of 7
    out = _fold([_doc("completed", rts=rts, done_at=H + 3600)])
    assert out["totals"]["done_later"] == 1 and out["totals"]["done_same_day"] == 0


def test_non_date_sim_clock_is_untimed_and_fails_invariant():
    d = _doc("completed", rts=0)
    d["sim_clock"] = "Wed, 01 Jan 2020 09:00:00 GMT"  # a string, never a BSON date
    out = _fold([d])
    assert out["totals"]["completed_untimed"] == 1
    assert out["invariants_ok"] is False


def test_early_completion_fails_invariant():
    out = _fold([_doc("completed", rts=100, done_at=10)])
    assert out["totals"]["early_completions"] == 1 and not out["invariants_ok"]


def test_resurrected_then_completed_counts_as_done():
    out = _fold([_doc("completed", rts=0, done_at=500, reason="__none__", truck=ObjectId())])
    assert out["totals"]["done_same_day"] == 1 and out["totals"]["failed"]["total"] == 0


def test_final_with_open_orders_is_flagged():
    out = _fold([_doc("assigned", rts=0)], final=True)
    assert not out["invariants_ok"] and "still open" in out["invariant_errors"][-1]


def test_owner_attribution_and_haulier_row_fields():
    docs = [
        _doc("completed", rts=0, done_at=60, hid="a", hn="A"),
        _doc("cancelled", rts=0, reason=bw.SWEEP_CANCEL_REASON, hid="b", hn="B"),
        _doc("completed", rts=0, done_at=60, hid="b", hn="B", truck="truck-of-a"),
    ]
    out = _fold(docs)
    assert out["attribution"] == "owner"
    assert set(out["by_haulier"]) == {"a", "b"}
    assert out["by_haulier"]["b"]["released"] == 2
    rows = oo.haulier_row_fields(out)
    assert rows["b"]["orders_failed_overdue_unassigned"] == 1
    assert rows["b"]["orders_done_same_day"] == 1
    assert rows["b"]["same_day_completion_rate"] == 0.5
    assert rows["a"]["order_outcomes_final"] is True
    assert not any(k == "num_orders_completed" for k in rows["a"])  # never the carrier key


def test_live_fold_counts_only_closed_days():
    docs = [_doc("completed", rts=0, done_at=60), _doc("completed", rts=400, done_at=DAY + 3600 * 3)]
    out = _fold(docs, final=False, closed=1)
    assert out["totals"]["released"] == 1 and list(out["days"]) == ["d0"]
    s = oo.scalars_from_outcomes(out)
    assert s == {"num_orders_done_same_day_total": 1, "num_orders_undone_at_day_end_total": 0,
                 "same_day_completion_rate": 1.0}
    assert oo.scalars_from_outcomes(None)["same_day_completion_rate"] == 0.0


def test_closed_days():
    assert oo.closed_days(0, H) == 0
    assert oo.closed_days(DAY - 1, H) == 0
    assert oo.closed_days(DAY, H) == 1
    assert oo.closed_days(H + 6 * 3600, H) == 7
    assert oo.closed_days(6.6 * DAY, 6.5 * DAY) == 7  # partial last day closes at H


# ------------------------------------------------------------------ reference time is UTC


def test_reference_time_parsed_as_utc_under_a_non_utc_tz(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    monkeypatch.setenv("TZ", "Asia/Singapore")
    time.tzset()
    try:
        ref = oo.parse_reference_time("2020-01-01 08:00:00")
        assert ref == datetime(2020, 1, 1, 8, 0, 0) and ref.tzinfo is None
        aware = datetime(2020, 1, 1, 16, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        assert oo.parse_reference_time(aware) == datetime(2020, 1, 1, 8, 0, 0)
        assert oo.parse_reference_time("Wed, 01 Jan 2020 08:00:00 GMT") == ref
        m = _manager()
        m.set_order_outcome_context(STEP, "2020-01-01 08:00:00", 2520)
        assert m._order_outcome_elapsed_s(datetime(2020, 1, 2, 8, 0, 0)) == DAY
        assert m._order_outcome_elapsed_s("Thu, 02 Jan 2020 08:00:00 GMT") == DAY
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


# ------------------------------------------------------------------ the reader (fake Mongo)


def _matches(doc, flt):
    for k, v in flt.items():
        if k == "$nor":
            if any(_matches(doc, f) for f in v):
                return False
            continue
        cur = doc
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if isinstance(v, dict):
            if "$ne" in v and cur == v["$ne"]:
                return False
            if "$in" in v and cur not in v["$in"]:
                return False
        elif cur != v:
            return False
    return True


class _Coll:
    def __init__(self, docs, agg_rows=None):
        self.docs = docs
        self.agg_rows = agg_rows or []
        self.pipelines = []
        self.query_kw = []  # (op, kwargs) of every query: the time bounds are asserted

    def find(self, flt, projection=None, **kw):
        self.query_kw.append(("find", kw))
        return [d for d in self.docs if _matches(d, flt)]

    def aggregate(self, pipeline, **kw):
        self.query_kw.append(("aggregate", kw))
        self.pipelines.append(pipeline)
        return list(self.agg_rows)

    def count_documents(self, flt, **kw):
        self.query_kw.append(("count_documents", kw))
        return sum(1 for d in self.docs if _matches(d, flt))


class _Client:
    def __init__(self, orders, hauls):
        self.db = {"container_logistics_order": orders, "container_logistics_haul_trip": hauls}
        self.closed = False

    def __getitem__(self, name):
        return self.db

    def close(self):
        self.closed = True


def _with_run(docs, run_id="r1"):
    for d in docs:
        d["run_id"] = run_id
    return docs


def test_reader_joins_string_haul_order_to_objectid_id():
    """haul.order is a STRING, order._id an ObjectId. Dropping str() loses the match."""
    oid = ObjectId()
    orders = _Coll(_with_run([_doc("cancelled", rts=2000, _id=oid)]))
    hauls = _Coll(_with_run([{"order": str(oid), "state": "cancelled", "stats": {"loaded_started_at": 1}}]))
    client = _Client(orders, hauls)
    reader = oo.OrderOutcomeReader("r1", client_factory=lambda: client, db_name="x")
    recs = reader.read(reference_time=REF, step_interval=STEP, horizon_s=H, per_doc=True)
    out = oo.fold_outcomes(recs, horizon_s=H, step_interval=STEP, reference_time=REF, final=True)
    assert out["totals"]["failed"]["cancelled_in_progress"] == 1
    assert out["totals"]["cancelled_in_progress_started"] == 1
    assert client.closed is True


def test_reader_aggregate_path_excludes_haul_referenced_cancels_from_the_group():
    oid = ObjectId()
    orders = _Coll(
        _with_run([_doc("cancelled", rts=2000, _id=oid, hid="a", hn="A")]),
        agg_rows=[{"_id": {"h": "a", "hn": "A", "rd": 0.0, "s": "completed", "r": None, "t": True,
                           "dd": 0.0, "ph": False, "e": False, "ah": False}, "n": 3}],
    )
    hauls = _Coll(_with_run([{"order": str(oid), "state": "cancelled", "stats": {}}]))
    client = _Client(orders, hauls)
    reader = oo.OrderOutcomeReader("r1", client_factory=lambda: client, db_name="x")
    recs = reader.read(reference_time=REF, step_interval=STEP, horizon_s=H)
    pipe = orders.pipelines[0]
    assert list(pipe[0]) == ["$match"] and pipe[0]["$match"]["run_id"] == "r1"  # index prefix first
    assert pipe[0]["$match"]["$nor"] == [{"state": "cancelled", "_id": {"$in": [oid]}}]
    out = oo.fold_outcomes(recs, horizon_s=H, step_interval=STEP, reference_time=REF, final=True)
    assert out["totals"]["released"] == 4
    assert out["totals"]["done_same_day"] == 3
    assert out["totals"]["failed"]["cancelled_in_progress"] == 1
    assert client.closed is True


def test_reader_closes_its_client_on_failure():
    class Boom(_Client):
        def __getitem__(self, name):
            raise RuntimeError("mongo down")

    client = Boom(None, None)
    with pytest.raises(RuntimeError):
        oo.OrderOutcomeReader("r1", client_factory=lambda: client, db_name="x").read(
            reference_time=REF, step_interval=STEP, horizon_s=H)
    assert client.closed is True


# ------------------------------------------------------------------ live refresh (manager)


def _manager():
    from unittest.mock import MagicMock

    return AnalyticsManager("r1", "20200101080000", MagicMock(), None)


class _ScriptedReader:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def read(self, **kw):
        self.calls += 1
        out = self.batches[min(self.calls - 1, len(self.batches) - 1)]
        if isinstance(out, Exception):
            raise out
        return [oo.record_from_doc(d, reference_time=REF, step_interval=STEP, horizon_s=H) for d in out]


def test_live_refresh_fires_only_on_a_newly_closed_day():
    m = _manager()
    reader = _ScriptedReader([[_doc("completed", rts=0, done_at=60)]])
    m._order_outcome_reader_factory = lambda run_id: reader
    m.set_order_outcome_context(STEP, "2020-01-01 08:00:00", 2520)
    assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(hours=3)) is False
    assert reader.calls == 0
    assert m.order_outcome_scalars()["num_orders_done_same_day_total"] == 0
    assert m.live_order_outcomes() is None
    assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=DAY + 100)) is True
    assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=DAY + 11000)) is False
    assert reader.calls == 1
    assert m.order_outcome_scalars()["num_orders_done_same_day_total"] == 1
    for k in range(2, 9):
        m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=k * DAY + 1))
    assert reader.calls == 7  # days 1..7 closed: at most `days` refreshes per run (closed = 1..D)
    assert m.live_order_outcomes()["final"] is False


def test_live_refresh_failure_keeps_cache_and_warns_once(caplog):
    m = _manager()
    reader = _ScriptedReader([[_doc("completed", rts=0, done_at=60)], RuntimeError("down"), RuntimeError("down")])
    m._order_outcome_reader_factory = lambda run_id: reader
    m.set_order_outcome_context(STEP, "2020-01-01 08:00:00", 2520)
    m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=DAY + 1))
    with caplog.at_level("WARNING"):
        assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=2 * DAY + 1)) is False
        assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=3 * DAY + 1)) is False
    assert sum("order-outcome recount failed" in r.message for r in caplog.records) == 1
    assert m.order_outcome_scalars()["num_orders_done_same_day_total"] == 1


def test_bad_context_disables_without_raising():
    m = _manager()
    m.set_order_outcome_context(None, "not a time", None)
    assert m._oo_context is None
    assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(days=3)) is False


def test_live_failed_count_may_decrease_on_resurrection():
    """num_orders_failed_total is count_by_state(cancelled): PROVISIONAL, may fall."""
    from unittest.mock import MagicMock

    from apps.container_logistics.analytics.app import AnalyticsApp

    app = AnalyticsApp.__new__(AnalyticsApp)
    app.kpi_collection = {"num_orders_completed_total": 10, "active_orders": 5}
    app.manager = MagicMock()
    app.manager.order_outcome_scalars.return_value = {}
    app.manager.count_orders_by_state.return_value = 7
    app._compute_order_outcome_kpis(REF)
    assert app.kpi_collection["num_orders_failed_total"] == 7
    assert app.kpi_collection["num_orders_released_total"] == 22
    app.manager.count_orders_by_state.return_value = 6  # one sweep cancel resurrected
    app._compute_order_outcome_kpis(REF)
    assert app.kpi_collection["num_orders_failed_total"] == 6


# ------------------------------------------------------------------ marker vs resurrection


def test_horizon_marker_is_hard_terminal_for_the_batch_writer():
    oid = str(ObjectId())
    ev = bw.ParsedEvent(order_id=oid, event="assign", target_state=bw.ASSIGNED_STATE,
                        truck_id=str(ObjectId()), sim_clock=REF)
    ev2 = bw.ParsedEvent(order_id=oid, event="x", target_state="completed", truck_id=None, sim_clock=REF)
    ops, stats = bw.plan_event_updates(
        [ev, ev2], {oid: {"state": "cancelled", "cancel_reason": HORIZON_CANCEL_REASON}}, "r1")
    assert ops == [] and stats.skipped_terminal == 2 and stats.resurrected == 0
    # ...while the sweep's own marker IS resurrectable (unchanged behaviour)
    ops, stats = bw.plan_event_updates(
        [ev], {oid: {"state": "cancelled", "cancel_reason": bw.SWEEP_CANCEL_REASON}}, "r1")
    assert len(ops) == 1 and stats.resurrected == 1


def test_bulk_cancelled_assigned_order_is_in_progress_at_horizon():
    out = _fold([_doc("cancelled", rts=2400, truck=ObjectId(), reason=HORIZON_CANCEL_REASON)])
    t = out["totals"]
    assert t["failed"]["cancelled_in_progress"] == 1 and t["failed"]["unserved_at_horizon"] == 0
    assert t["cancelled_in_progress_at_horizon"] == 1


def test_simulated_stamp_moves_unmarked_horizon_cancels_out_of_other():
    """Pre-marker runs leave bulk cancels unstamped -> `other`; with the stamp -> unserved."""
    old = _fold([_doc("cancelled", rts=2400)])["totals"]["failed"]
    new = _fold([_doc("cancelled", rts=2400, reason=HORIZON_CANCEL_REASON)])["totals"]["failed"]
    assert (old["other"], old["unserved_at_horizon"]) == (1, 0)
    assert (new["other"], new["unserved_at_horizon"]) == (0, 1)


def test_completion_at_exactly_the_horizon_on_a_partial_last_day_is_later():
    h = 6.5 * DAY
    rts = int(6.2 * DAY / STEP)
    out = _fold([_doc("completed", rts=rts, done_at=h)], horizon_s=h)
    assert (out["totals"]["done_same_day"], out["totals"]["done_later"]) == (0, 1)


# ------------------------------------------------------------------ fix round FX1 (review m3/m5/m7/m8)


def test_scalar_undone_is_published_from_undone_not_done_later():
    """review m7: undone (2) != done_later (1) here, so a mis-mapped scalar is caught."""
    docs = [
        _doc("completed", rts=0, done_at=60),
        _doc("completed", rts=0, done_at=DAY + 60),
        _doc("cancelled", rts=0, reason=bw.SWEEP_CANCEL_REASON),
    ]
    s = oo.scalars_from_outcomes(_fold(docs, final=False, closed=1))
    assert s["num_orders_undone_at_day_end_total"] == 2
    assert s["num_orders_done_same_day_total"] == 1
    assert s["same_day_completion_rate"] == round(1 / 3, 4)


def test_haulier_row_rate_is_done_same_day_over_released():
    """review m7: 1 same-day of 3 released (undone 2), so any other numerator is caught."""
    docs = [
        _doc("completed", rts=0, done_at=60, hid="a"),
        _doc("completed", rts=0, done_at=DAY + 60, hid="a"),
        _doc("cancelled", rts=0, reason=bw.SWEEP_CANCEL_REASON, hid="a"),
    ]
    row = oo.haulier_row_fields(_fold(docs))["a"]
    assert (row["orders_released"], row["orders_done_same_day"], row["orders_undone_at_day_end"]) == (3, 1, 2)
    assert row["same_day_completion_rate"] == round(1 / 3, 4)


@pytest.mark.parametrize("uri", ["mongodb://example:27017", None])
def test_mongo_client_is_bounded_on_selection_and_socket(monkeypatch, uri):
    """review m5/m7: both construction branches carry the selection AND socket bounds."""
    import pymongo

    from apps import config

    seen = {}

    def fake_client(*args, **kw):
        seen["args"], seen["kw"] = args, kw
        return object()

    monkeypatch.setattr(pymongo, "MongoClient", fake_client)
    monkeypatch.setattr(config, "kpi_sink_settings",
                        {"mongo_uri": uri, "mongo_host": "h", "mongo_port": "27017", "mongo_db": "x"})
    oo._mongo_client()
    assert seen["kw"]["connect"] is False
    assert seen["kw"]["serverSelectionTimeoutMS"] == 3000
    assert seen["kw"]["socketTimeoutMS"] == oo.LIVE_MAX_TIME_MS + oo._SOCKET_SLACK_MS
    oo._mongo_client(server_selection_timeout_ms=1234, socket_timeout_ms=999)
    assert seen["kw"]["serverSelectionTimeoutMS"] == 1234 and seen["kw"]["socketTimeoutMS"] == 999


def test_default_reader_socket_bound_follows_its_query_bound(monkeypatch):
    seen = {}
    monkeypatch.setattr(oo, "_mongo_client", lambda **kw: seen.update(kw) or _Client(_Coll([]), _Coll([])))
    oo.OrderOutcomeReader("r1", db_name="x", max_time_ms=7000).read(
        reference_time=REF, step_interval=STEP, horizon_s=H)
    assert seen == {"socket_timeout_ms": 7000 + oo._SOCKET_SLACK_MS}


@pytest.mark.parametrize("per_doc", [False, True])
def test_every_reader_query_carries_max_time_ms(per_doc):
    """review m5: a slow-but-up Mongo must fail the recount, not hold the analytics step."""
    oid = ObjectId()
    orders = _Coll(_with_run([_doc("cancelled", rts=2000, _id=oid), _doc("created", rts=5)]))
    hauls = _Coll(_with_run([{"order": str(oid), "state": "cancelled", "stats": {}}]))
    client = _Client(orders, hauls)
    oo.OrderOutcomeReader("r1", client_factory=lambda: client, db_name="x", max_time_ms=4321).read(
        reference_time=REF, step_interval=STEP, horizon_s=H, per_doc=per_doc, count_unreleased=True)
    calls = orders.query_kw + hauls.query_kw
    ops = sorted(op for op, _ in calls)
    assert ops == (["count_documents", "find", "find"] if per_doc
                   else ["aggregate", "count_documents", "find", "find"])
    for op, kw in calls:
        key = "max_time_ms" if op == "find" else "maxTimeMS"
        assert kw.get(key) == 4321, (op, kw)


def test_compute_uses_live_bound_live_and_final_bound_final(monkeypatch):
    built = []

    class Rec(oo.OrderOutcomeReader):
        def __init__(self, run_id, **kw):
            built.append(kw.get("max_time_ms"))
            super().__init__(run_id, **kw)

        def read(self, **kw):
            built.append(kw.get("count_unreleased", False))
            return []

    monkeypatch.setattr(oo, "OrderOutcomeReader", Rec)
    oo.compute_order_outcomes("r1", step_interval=STEP, reference_time=REF, horizon_s=H, final=False, closed=1)
    oo.compute_order_outcomes("r1", step_interval=STEP, reference_time=REF, horizon_s=H, final=True)
    assert built == [oo.LIVE_MAX_TIME_MS, False, oo.FINAL_MAX_TIME_MS, True]
    assert oo.LIVE_MAX_TIME_MS <= 5000


def test_reader_timeout_is_contained_by_the_live_refresh():
    """A maxTimeMS expiry raises inside read(); the manager keeps the previous values."""
    from pymongo.errors import ExecutionTimeout

    m = _manager()
    reader = _ScriptedReader([[_doc("completed", rts=0, done_at=60)], ExecutionTimeout("slow")])
    m._order_outcome_reader_factory = lambda run_id: reader
    m.set_order_outcome_context(STEP, "2020-01-01 08:00:00", 2520)
    assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=DAY + 1)) is True
    assert m.refresh_order_outcomes_if_day_closed(REF + timedelta(seconds=2 * DAY + 1)) is False
    assert m.order_outcome_scalars()["num_orders_done_same_day_total"] == 1


def test_release_at_or_after_horizon_is_an_invariant_error_not_a_failure():
    """review m8: an order released at/after H was never published inside the run."""
    rts_h = 2520  # t_rel == H exactly
    docs = [
        _doc("completed", rts=0, done_at=60),
        _doc("cancelled", rts=rts_h, reason=HORIZON_CANCEL_REASON),
        _doc("cancelled", rts=rts_h + 400, reason=HORIZON_CANCEL_REASON),
    ]
    out = _fold(docs)
    assert out["release_beyond_horizon"] == 2
    assert out["totals"]["released"] == 1 and out["totals"]["failed"]["total"] == 0
    assert out["days_count"] == 7 and set(out["days"]) == {"d0"}
    assert not out["invariants_ok"]
    assert any("at/after the horizon" in e for e in out["invariant_errors"])
    # just inside the horizon still counts as released and failed
    inside = _fold([_doc("cancelled", rts=rts_h - 1, reason=HORIZON_CANCEL_REASON)])
    assert inside["release_beyond_horizon"] == 0 and inside["invariants_ok"]
    assert inside["totals"]["failed"]["unserved_at_horizon"] == 1


def test_release_beyond_a_partial_last_day_horizon_is_flagged():
    """The bound is H itself, not days_count: release day 6 < 7 days, but after H = 6.5 d."""
    h = 6.5 * DAY
    out = _fold([_doc("cancelled", rts=int(6.7 * DAY / STEP), reason=HORIZON_CANCEL_REASON)], horizon_s=h)
    assert out["release_beyond_horizon"] == 1 and not out["invariants_ok"]


def test_pipeline_groups_on_release_beyond_horizon():
    pipe = oo.OrderOutcomeReader("r1", db_name="x").pipeline(
        reference_time=REF, step_interval=STEP, horizon_s=H, exclude_ids=[])
    assert pipe[1]["$group"]["_id"]["bh"] == {
        "$gte": [{"$multiply": ["$meta.request_time_step", STEP * 1000.0]}, H * 1000.0]}
    # and the aggregate row's bh reaches the fold
    orders = _Coll([], agg_rows=[{"_id": {"h": "a", "hn": "A", "rd": 7.0, "bh": True, "s": "cancelled",
                                          "r": HORIZON_CANCEL_REASON, "t": False, "dd": None, "ph": None,
                                          "e": None, "ah": True}, "n": 4}])
    client = _Client(orders, _Coll([]))
    recs = oo.OrderOutcomeReader("r1", client_factory=lambda: client, db_name="x").read(
        reference_time=REF, step_interval=STEP, horizon_s=H)
    out = oo.fold_outcomes(recs, horizon_s=H, step_interval=STEP, reference_time=REF, final=True)
    assert out["release_beyond_horizon"] == 4 and out["totals"]["released"] == 0


def test_created_at_final_is_an_invariant_error():
    """review m8: the final read counts orders still `created` (bulk cancel missed them)."""
    orders = _Coll(_with_run([_doc("completed", rts=0, done_at=60), _doc("created", rts=10),
                              _doc("created", rts=20)]))
    client = _Client(orders, _Coll([]))
    reader = oo.OrderOutcomeReader("r1", client_factory=lambda: client, db_name="x")
    for per_doc in (False, True):
        out = oo.compute_order_outcomes("r1", step_interval=STEP, reference_time=REF, horizon_s=H,
                                        final=True, reader=reader, per_doc=per_doc)
        assert out["unreleased_at_final"] == 2
        assert out["totals"]["released"] == (0 if not per_doc else 1)  # fake agg rows are empty
        assert any("still `created`" in e for e in out["invariant_errors"])
    live = oo.compute_order_outcomes("r1", step_interval=STEP, reference_time=REF, horizon_s=H,
                                     final=False, closed=1, reader=reader, per_doc=True)
    assert "unreleased_at_final" not in live and live["invariants_ok"]
    assert not any(op == "count_documents" for op, _ in orders.query_kw[-2:])


def test_released_total_never_dips():
    """review m3: separate reads can double-count then correct; the counter must not dip."""
    from unittest.mock import MagicMock

    from apps.container_logistics.analytics.app import AnalyticsApp

    app = AnalyticsApp.__new__(AnalyticsApp)
    app.manager = MagicMock()
    app.manager.order_outcome_scalars.return_value = {}
    seq = []
    for completed, cancelled, active in [(10, 2, 5), (10, 3, 5), (11, 3, 2), (12, 3, 3)]:
        app.kpi_collection = {"num_orders_completed_total": completed, "active_orders": active}
        app.manager.count_orders_by_state.return_value = cancelled
        app._compute_order_outcome_kpis(REF)
        seq.append(app.kpi_collection["num_orders_released_total"])
    assert seq == [17, 18, 18, 18]


def test_agent_forwards_live_outcomes_to_the_breakdown_flush():
    """review m7: the agent must hand manager.live_order_outcomes() to save_breakdowns."""
    from unittest.mock import MagicMock

    from apps.container_logistics.analytics.agent import AnalyticsAgentIndie

    agent = AnalyticsAgentIndie.__new__(AnalyticsAgentIndie)
    sentinel = object()
    agent.app = MagicMock()
    agent.app.manager.live_order_outcomes.return_value = sentinel
    agent.current_time = REF
    agent.get_current_time_str = lambda: "Wed, 01 Jan 2020 08:00:00 GMT"
    agent._maybe_flush_breakdowns({})
    agent.app.manager.save_breakdowns.assert_called_once_with(
        "Wed, 01 Jan 2020 08:00:00 GMT", order_outcomes=sentinel)
