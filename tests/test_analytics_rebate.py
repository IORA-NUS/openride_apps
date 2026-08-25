"""Facility-rebate settlement + ledger in AnalyticsManager (plan §3.1/§3.3/§10, Phase 4).

Settlement lives in analytics, downstream of every decision, so it prices the two
arrivals a completed trip already recorded (``stats.<leg>_queue_arrival_time`` +
``meta.<leg>_facility_resource_id``) against the facility's published schedule.

**Nothing under test is stubbed.** The real :class:`RebateBook` is built by the real
``from_facility_docs`` from real facility documents, and the real ``price_at`` parses the
real RFC-1123 strings the simulation writes (``'Wed, 01 Jan 2020 16:00:00 GMT'`` — the
exact shape verified on 785/785 completed trips of ``run_20260817_164156``). Only the
HTTP/DB boundary (``_paged_where``) and the persistence sink are stubbed: a stubbed-fetch
test that green-lit a panel showing "—" for a feature's whole life is a scar this project
already carries (CLAUDE.md §6.15).
"""

from datetime import datetime, timezone

import pytest

from apps.container_logistics.analytics.manager import AnalyticsManager

# --------------------------------------------------------------------------- fixtures

PORT = "6a82c961e30b9937b9db6f5e"      # pickup facility, shape copied from a real run
DEPOT = "5f13e0a72c9b41d8ae77c001"     # dropoff facility
BARE = "5f13e0a72c9b41d8ae77c999"      # a facility that publishes no schedule

#: 16:00 pays 20, 18:00 pays 5 — two different hours so the two arrivals cannot be
#: confused with each other, and the hour-of-day rule is actually exercised.
PORT_ARRIVAL = "Wed, 01 Jan 2020 16:00:00 GMT"
DEPOT_ARRIVAL = "Wed, 01 Jan 2020 18:00:00 GMT"
END = "Wed, 01 Jan 2020 23:00:00 GMT"


def _schedule(points, currency="credit"):
    return {"currency": currency, "resolution": "hour", "points": points}


def _facility_docs(port_points=None, depot_points=None, currency="credit"):
    """Real facility documents, exactly the ``_id`` + ``profile`` shape analytics reads."""
    docs = [{"_id": BARE, "profile": {"gate_count": 4, "service_time": 900}}]
    if port_points is not None:
        docs.append({"_id": PORT, "profile": {"rebate": _schedule(port_points, currency)}})
    else:
        docs.append({"_id": PORT, "profile": {"gate_count": 12}})
    if depot_points is not None:
        docs.append({"_id": DEPOT, "profile": {"rebate": _schedule(depot_points, currency)}})
    else:
        docs.append({"_id": DEPOT, "profile": {"gate_count": 2}})
    return docs


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _trip(
    truck_id,
    carrier,
    *,
    pickup_fac=PORT,
    dropoff_fac=DEPOT,
    pickup_at=PORT_ARRIVAL,
    dropoff_at=DEPOT_ARRIVAL,
    collab=None,
    sim_clock="Wed, 01 Jan 2020 20:00:00 GMT",
):
    meta = {
        "truck_profile": {"haulier_id": carrier, "haulier_name": carrier.title()},
        "order_profile": {},
        "pickup_facility_resource_id": pickup_fac,
        "dropoff_facility_resource_id": dropoff_fac,
        "reposition_origin_loc": _pt(103.80, 1.30),
    }
    if collab:
        meta["collaboration"] = collab
    stats = {}
    if pickup_at is not None:
        stats["pickup_queue_arrival_time"] = pickup_at
    if dropoff_at is not None:
        stats["dropoff_queue_arrival_time"] = dropoff_at
    return {
        "truck": truck_id,
        "sim_clock": sim_clock,
        "state": "completed",
        "pickup_loc": _pt(103.85, 1.30),
        "dropoff_loc": _pt(103.90, 1.30),
        "meta": meta,
        "stats": stats,
        "routes": {"planned": {}, "actual": {}},
    }


def _mgr(facility_docs=None, trips=None, incomplete_trips=None):
    """An AnalyticsManager with only the HTTP boundary replaced.

    ``_paged_where`` is dispatched by URL so the facility read (the rebate book) and the
    haul-trip read (the finalize recompute) return their own documents, and every other
    collection reads empty — which is what a manager with no REST server sees anyway.
    """
    m = AnalyticsManager.__new__(AnalyticsManager)
    m.run_id = "run_test_rebate"
    m.user = "tester"
    m.simulation_domain = "container-logistics-sim"
    m._truck_acc = {}
    m._haulier_acc = {}
    m._lane_acc = {}
    m._idle_seconds_sum = 0.0
    m._idle_count = 0
    m._run_start_time = "Wed, 01 Jan 2020 08:00:00 GMT"
    m._metric_window_start = m._run_start_time
    m._metric_window_end = None
    m._shared_trip_count = 0
    m._total_benefit_km = 0.0
    m._coop_structure_id = None
    m._coop_components = []

    facility_url = m._facility_url()
    haul_url = m._haul_trip_url()

    def _fake_paged_where(base_url, where_clause, projection=None, **kwargs):
        if base_url == facility_url:
            return list(facility_docs or [])
        if base_url == haul_url:
            # The forfeiture scan (R2-4) asks for state != completed; the finalize
            # recompute asks for state == completed. Dispatch on the clause so the two
            # populations stay distinct, exactly as they are against a real server.
            clauses = (where_clause or {}).get("$and") or []
            wants_incomplete = any(
                isinstance(c.get("state"), dict) and "$ne" in c["state"] for c in clauses
                if isinstance(c, dict)
            )
            if wants_incomplete:
                return list(incomplete_trips or [])
            return list(trips or [])
        return []

    m._paged_where = _fake_paged_where
    return m


def _ledger(m, haulier_id):
    acc = m._haulier_acc[haulier_id]
    return (
        acc["rebate_credited"],
        acc["rebate_arrivals_priced"],
        acc["rebate_arrivals_unpriced"],
    )


# ------------------------------------------------------------------------------ tests


def test_carrier_is_credited_not_the_owner():
    """A facility pays whoever SHOWS UP. The owner of a shared job stays at zero.

    Fifteen lines from the settlement call site, ``benefit_km_received`` is credited to
    ``collaboration.owner_haulier_id``; copying that shape here pays the wrong company
    (plan §15.4). This test exists precisely for that.
    """
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    m.accumulate_completed_trips(
        [
            _trip(
                "t1", "borax",
                collab={"shared": True, "owner_haulier_id": "acme",
                        "carrier_haulier_id": "borax", "benefit_km": 7.5},
            )
        ],
        END,
    )

    # The carrier (borax ran the truck) gets the money.
    assert _ledger(m, "borax") == (20.0, 1, 1)  # dropoff facility has no schedule
    # The owner (acme) is credited the collaboration benefit and NOTHING of the rebate.
    acme = m._haulier_acc["acme"]
    assert acme["jobs_shared_out"] == 1, "the shared-job path must really have run"
    assert acme["benefit_km_received"] == 7.5
    assert acme["rebate_credited"] == 0.0
    assert acme["rebate_arrivals_priced"] == 0
    assert acme["rebate_arrivals_unpriced"] == 0, (
        "the owner did not arrive anywhere; it must not even appear in the denominator"
    )


def test_both_arrivals_settle_independently():
    """Two arrivals, two facilities, two different hours — one symmetric loop, no roles."""
    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}, {"hour": 18, "amount": 1.0}],
        depot_points=[{"hour": 18, "amount": 5.0}, {"hour": 16, "amount": 99.0}],
    ))
    m.accumulate_completed_trips([_trip("t1", "acme")], END)
    # 20 (port @16) + 5 (depot @18). Picking the wrong facility, or pricing both arrivals
    # at one hour, gives 40/10/119/104 — every confusion is a different number.
    assert _ledger(m, "acme") == (25.0, 2, 0)

    # Same trip, arrivals swapped in time: now 1 (port @18) + 99 (depot @16).
    m2 = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}, {"hour": 18, "amount": 1.0}],
        depot_points=[{"hour": 18, "amount": 5.0}, {"hour": 16, "amount": 99.0}],
    ))
    m2.accumulate_completed_trips(
        [_trip("t1", "acme", pickup_at=DEPOT_ARRIVAL, dropoff_at=PORT_ARRIVAL)], END
    )
    assert _ledger(m2, "acme") == (100.0, 2, 0)


def test_missing_arrival_stamp_counts_unpriced_and_pays_nothing():
    """No stamp ⇒ no rebate (plan §10): the price is for an arrival that happened.

    Also covers the malformed-stamp branch, which must degrade into the same published
    denominator rather than killing the analytics tick.
    """
    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}],
        depot_points=[{"hour": 18, "amount": 5.0}],
    ))
    m.accumulate_completed_trips(
        [
            _trip("t1", "acme", dropoff_at=None),            # dropoff never stamped
            _trip("t2", "acme", pickup_fac=None),            # arrival with no facility id
            _trip("t3", "acme", pickup_at="not a timestamp"),  # unparseable stamp
        ],
        END,
    )
    credited, priced, unpriced = _ledger(m, "acme")
    # Only the three well-stamped arrivals pay: t1 pickup 20, t2 dropoff 5, t3 dropoff 5.
    assert credited == 30.0
    assert priced == 3
    assert unpriced == 3
    assert priced + unpriced == 6, "every arrival must land in exactly one bucket"


def test_facility_without_schedule_counts_unpriced():
    """A facility that publishes nothing is unpriceable, never an imputed zero."""
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    m.accumulate_completed_trips([_trip("t1", "acme", dropoff_fac=BARE)], END)
    assert _ledger(m, "acme") == (20.0, 1, 1)
    # And the book really does know nothing about that facility.
    assert m.rebate_book().price_at(BARE, DEPOT_ARRIVAL) is None


def test_negative_amount_debits_the_ledger():
    """Amounts are signed: a surcharge is a negative rebate, carried verbatim."""
    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": -12.5}],
        depot_points=[{"hour": 18, "amount": 5.0}],
    ))
    m.accumulate_completed_trips([_trip("t1", "acme")], END)
    credited, priced, unpriced = _ledger(m, "acme")
    assert credited == -7.5, "a negative amount must debit, not clamp to zero"
    assert (priced, unpriced) == (2, 0)

    row = {r["id"]: r for r in m.build_breakdown("haulier", END)}["acme"]
    assert row["rebate_credited"] == -7.5


def test_rebate_never_touches_km_accumulators():
    """Settlement is a LEDGER, strictly separate from every distance accumulator."""
    trips = [
        _trip("t1", "acme"),
        _trip("t2", "borax",
              collab={"shared": True, "owner_haulier_id": "acme",
                      "carrier_haulier_id": "borax", "benefit_km": 3.0}),
    ]
    km_keys = ("empty_km", "loaded_km", "active_seconds", "trip_count",
               "benefit_km_received", "dual_cycle_count", "chain_opportunities")

    without = _mgr(_facility_docs())  # no facility publishes a schedule
    without.accumulate_completed_trips([dict(t) for t in trips], END)

    with_sched = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}],
        depot_points=[{"hour": 18, "amount": -5.0}],
    ))
    with_sched.accumulate_completed_trips([dict(t) for t in trips], END)

    for hid in ("acme", "borax"):
        a = without._haulier_acc[hid]
        b = with_sched._haulier_acc[hid]
        for key in km_keys:
            assert repr(a[key]) == repr(b[key]), (
                f"{hid}.{key} moved when a rebate schedule was added: {a[key]} -> {b[key]}"
            )
    for tid in ("t1", "t2"):
        a = without._truck_acc[tid]
        b = with_sched._truck_acc[tid]
        for key in ("empty_km", "loaded_km", "active_seconds", "trip_count"):
            assert repr(a[key]) == repr(b[key])
        assert "rebate_credited" not in b, (
            "the ledger is a company-level concept; truck accs must stay unchanged"
        )
    # ... and the schedule genuinely bit, so the equality above is not vacuous.
    assert without._haulier_acc["acme"]["rebate_credited"] == 0.0
    assert with_sched._haulier_acc["acme"]["rebate_credited"] == 15.0


def test_breakdown_carries_the_ledger_under_haulier_scope():
    """The ledger rides the EXISTING ``scope: "haulier"`` breakdown — no new scope.

    ``scope`` is a closed Eve enum and ``kpi_breakdown_persist`` silently returns for an
    unknown value: a new scope fails twice, both times quietly (plan §10 wiring point 4).
    """
    from apps.container_logistics.analytics import kpi_breakdown_persist

    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}],
        depot_points=[{"hour": 18, "amount": 5.0}],
    ))
    m.accumulate_completed_trips([_trip("t1", "acme")], END)

    row = {r["id"]: r for r in m.build_breakdown("haulier", END)}["acme"]
    assert row["rebate_credited"] == 25.0
    assert row["rebate_arrivals_priced"] == 2
    assert row["rebate_arrivals_unpriced"] == 0
    assert row["rebate_currency"] == "credit"

    captured = []

    def _capture(user, run_id, *, scope, sim_clock, rows, final=False, **_kw):
        captured.append((scope, rows))

    original = kpi_breakdown_persist.persist_kpi_breakdown
    kpi_breakdown_persist.persist_kpi_breakdown = _capture
    try:
        m.set_metric_window(m._run_start_time, END)
        m.save_breakdowns(END, final=True)
    finally:
        kpi_breakdown_persist.persist_kpi_breakdown = original

    scopes = [scope for scope, _ in captured]
    assert scopes == ["truck", "haulier", "lane"], (
        f"settlement must not introduce a scope; got {scopes}"
    )
    haulier_rows = dict(captured)["haulier"]
    persisted = {r["id"]: r for r in haulier_rows}["acme"]
    assert persisted["rebate_credited"] == 25.0
    assert persisted["rebate_arrivals_unpriced"] == 0
    # The denominator ships with the money, always (reporting contract, plan §10).
    truck_rows = dict(captured)["truck"]
    assert all("rebate_credited" not in r for r in truck_rows)


def test_full_recompute_does_not_double_count_the_ledger():
    """The finalize replay must start from zero — the G29 trap.

    ``recompute_breakdowns_full`` re-folds EVERY completed trip to produce the
    authoritative ``final=True`` document that every analysis reads. A ledger that
    survives the reset is silently doubled there, and doubled money looks exactly like
    real money.
    """
    from apps.container_logistics.analytics import kpi_breakdown_persist

    trips = [_trip("t1", "acme"), _trip("t2", "acme", pickup_at=DEPOT_ARRIVAL)]
    facilities = _facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}, {"hour": 18, "amount": 1.0}],
        depot_points=[{"hour": 18, "amount": 5.0}],
    )

    single = _mgr(facilities, trips)
    single.accumulate_completed_trips([dict(t) for t in trips], END)
    expected = _ledger(single, "acme")
    assert expected[0] != 0.0 and expected[1] > 0, "fixture must actually pay something"

    # Same manager, already carrying a full window of accumulated trips: the replay must
    # reproduce the single-pass figure, not add to it.
    original = kpi_breakdown_persist.persist_kpi_breakdown
    kpi_breakdown_persist.persist_kpi_breakdown = lambda *a, **k: None
    try:
        single.recompute_breakdowns_full(END)
    finally:
        kpi_breakdown_persist.persist_kpi_breakdown = original

    assert _ledger(single, "acme") == expected, (
        "the rebate ledger was double-counted by the finalize recompute"
    )

    # A fresh manager replaying the same trips must agree with both.
    fresh = _mgr(facilities, trips)
    kpi_breakdown_persist.persist_kpi_breakdown = lambda *a, **k: None
    try:
        fresh.recompute_breakdowns_full(END)
    finally:
        kpi_breakdown_persist.persist_kpi_breakdown = original
    assert _ledger(fresh, "acme") == expected


def test_ledger_is_zero_not_absent_for_a_haulier_with_no_priced_arrivals():
    """Zero-with-a-denominator, never a missing key: absence reads as "not measured"."""
    # acme only visits facilities that publish nothing; borax visits a paying port. The
    # contrast is deliberate: a zero ledger must mean "acme earned nothing", not "the
    # feature pays nothing" — without borax this test would still pass against a no-op
    # pricer (mutations M4/M5) and would certify precisely nothing.
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    m.accumulate_completed_trips(
        [
            _trip("t1", "acme", pickup_fac=BARE, dropoff_fac=BARE),
            _trip("t2", "borax"),
        ],
        END,
    )
    assert m._haulier_acc["borax"]["rebate_credited"] == 20.0

    acc = m._haulier_acc["acme"]
    assert acc["rebate_credited"] == 0.0
    assert acc["rebate_arrivals_priced"] == 0
    assert acc["rebate_arrivals_unpriced"] == 2
    assert acc["rebate_currency"] is None

    row = {r["id"]: r for r in m.build_breakdown("haulier", END)}["acme"]
    for key in ("rebate_credited", "rebate_arrivals_priced",
                "rebate_arrivals_unpriced", "rebate_currency"):
        assert key in row, f"{key} must be published even when nothing was priced"
    assert row["rebate_credited"] == 0.0
    assert row["rebate_arrivals_unpriced"] == 2
    assert row["rebate_currency"] is None

    # A haulier seeded from the roster that has completed no trip at all still carries
    # a zeroed ledger rather than an absent one.
    seeded = AnalyticsManager._new_haulier_acc("cargo", "Cargo")
    seeded_row = AnalyticsManager._entity_row(seeded, 1.0)
    assert seeded_row["rebate_credited"] == 0.0
    assert seeded_row["rebate_arrivals_priced"] == 0
    assert seeded_row["rebate_arrivals_unpriced"] == 0


def test_planner_rows_aggregate_the_ledger_over_members():
    """The planner key tuple is hardcoded; the ledger is deliberately listed in it."""
    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}],
        depot_points=[{"hour": 18, "amount": 5.0}],
    ))
    m.set_cooperation({"structure_id": "pair", "components": [["acme", "borax"]]})
    m.accumulate_completed_trips([_trip("t1", "acme"), _trip("t2", "borax")], END)
    rows = m.build_planner_breakdown(END)
    assert len(rows) == 1
    assert rows[0]["rebate_credited"] == 50.0
    assert rows[0]["rebate_arrivals_priced"] == 4
    assert rows[0]["rebate_currency"] == "credit"


def test_book_read_failure_degrades_to_unpriced_without_raising():
    """Analytics must survive a facility read that blows up; arrivals count as unpriced."""
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))

    def _boom(*a, **k):
        raise RuntimeError("facility collection unreachable")

    m._paged_where = _boom
    m.accumulate_completed_trips([_trip("t1", "acme")], END)
    assert _ledger(m, "acme") == (0.0, 0, 2)
    assert not hasattr(m, "_rebate_book_cache"), (
        "a failed read must not be cached, or the whole run freezes unpriced"
    )


def test_empty_facility_collection_is_not_cached_but_a_rebateless_one_is():
    """The caching rule: cache on a successful, non-empty COLLECTION read.

    An empty collection means facilities are not created yet (or the read failed), so it
    retries — the ``fleet_haulier_roster`` intent. A collection that exists but publishes
    no schedules is the normal steady state and IS cached, otherwise every analytics tick
    re-pages every facility for the rest of the run.
    """
    calls = []
    m = _mgr()

    facility_url = m._facility_url()
    state = {"docs": []}

    def _counting(base_url, where_clause, projection=None, **kwargs):
        if base_url == facility_url:
            calls.append(base_url)
            return list(state["docs"])
        return []

    m._paged_where = _counting

    assert len(m.rebate_book()) == 0
    assert not hasattr(m, "_rebate_book_cache")
    assert len(calls) == 1

    state["docs"] = _facility_docs()  # facilities exist, none publishes a rebate
    assert len(m.rebate_book()) == 0
    assert hasattr(m, "_rebate_book_cache")
    assert len(calls) == 2
    m.rebate_book()
    assert len(calls) == 2, "a successful read must not be repeated every tick"


def test_pricing_uses_the_recorded_stamp_not_a_step_index():
    """The G5 trap: an hour derived from a step index is 4-8 hours wrong.

    Settlement must hand ``price_at`` the recorded RFC-1123 string; a numeric stamp is
    refused by the pricing module and lands in the unpriced denominator.
    """
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    with pytest.raises(ValueError):
        m.rebate_book().price_at(PORT, 1234)

    m.accumulate_completed_trips(
        [_trip("t1", "acme", pickup_at=1234, dropoff_at=None)], END
    )
    assert _ledger(m, "acme") == (0.0, 0, 2)


def test_datetime_stamps_are_priced_too():
    """Some call sites hold a datetime rather than the wire string; both must price."""
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    m.accumulate_completed_trips(
        [
            _trip(
                "t1", "acme",
                pickup_at=datetime(2020, 1, 1, 16, 0, tzinfo=timezone.utc),
                dropoff_at=None,
            )
        ],
        END,
    )
    assert _ledger(m, "acme") == (20.0, 1, 1)


# ===========================================================================
# Revision 2 — the counter split (R2-2/3/4/5; review F4, F6, F7, F8)
# ===========================================================================
#
# The pre-split ledger published one signed scalar and one undifferentiated
# "unpriced" count. Three separate defects followed from that, and one change
# discharges all of them:
#   * F7/F8 — the total cannot say WHICH leg earned it, and half of it is priced
#     on a clock the run's own stamp declares non-independent (plan §16.1);
#   * F6    — "the scenario chose not to pay depots" and "the arrival stamp is
#     missing" landed in the same counter, so a wiring failure was
#     indistinguishable from a design choice;
#   * F4    — forfeited arrivals reached neither counter, pinning
#     `priced + unpriced` to exactly 2 x completed.


def _split(m, haulier_id):
    acc = m._haulier_acc[haulier_id]
    return (
        acc["rebate_credited_pickup"],
        acc["rebate_credited_dropoff"],
        acc["rebate_credited"],
    )


def _reasons(m, haulier_id):
    acc = m._haulier_acc[haulier_id]
    return (
        acc["rebate_arrivals_unpriced_no_schedule"],
        acc["rebate_arrivals_unpriced_no_stamp"],
        acc["rebate_arrivals_unpriced_unparseable"],
    )


def test_ledger_splits_by_leg_and_sums_to_the_total():
    """R2-2. The two legs pay different amounts, and the halves must reconstruct it.

    This is also the back-compat check plan §18.6 relies on: option C preserved the
    ability to verify that the split reproduces the old total exactly.
    """
    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}],
        depot_points=[{"hour": 18, "amount": 7.0}],
    ))
    m.accumulate_completed_trips([_trip("t1", "acme")], END)

    pickup, dropoff, total = _split(m, "acme")
    assert (pickup, dropoff) == (20.0, 7.0), "the legs were not attributed separately"
    assert pickup + dropoff == total == 27.0
    # Asymmetric on purpose: equal halves would pass even if the leg label were ignored.
    assert pickup != dropoff


def test_the_leg_split_survives_into_the_published_row():
    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}],
        depot_points=[{"hour": 18, "amount": 7.0}],
    ))
    m.accumulate_completed_trips([_trip("t1", "acme")], END)
    (row,) = [r for r in m.build_breakdown("haulier", END) if r["id"] == "acme"]

    assert row["rebate_credited_pickup"] == 20.0
    assert row["rebate_credited_dropoff"] == 7.0
    assert row["rebate_credited"] == 27.0


def test_a_dropoff_only_schedule_credits_nothing_to_pickup():
    """The quarantine plan §16.1 needs: the invalid-clock half must be isolable."""
    m = _mgr(_facility_docs(depot_points=[{"hour": 18, "amount": 7.0}]))
    m.accumulate_completed_trips([_trip("t1", "acme")], END)

    pickup, dropoff, total = _split(m, "acme")
    assert pickup == 0.0 and dropoff == 7.0 and total == 7.0


def test_three_unpriced_conditions_are_counted_separately():
    """R2-3 / review F6, using his probe P8's three conditions as one fixture.

    Each arrival below fails for a DIFFERENT structural reason, and the point of the
    split is that a reader can tell which.
    """
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    m.accumulate_completed_trips(
        [
            # dropoff facility publishes nothing -> no_schedule (EXPECTED)
            _trip("t1", "acme"),
            # dropoff stamp absent entirely -> no_stamp (DATA defect)
            _trip("t2", "acme", dropoff_at=None),
            # dropoff stamp present but unreadable -> unparseable (CODE defect)
            _trip("t3", "acme", dropoff_fac=PORT, dropoff_at="not a timestamp"),
        ],
        END,
    )

    no_sched, no_stamp, unparseable = _reasons(m, "acme")
    assert (no_sched, no_stamp, unparseable) == (1, 1, 1), (
        "the three structurally different unpriced conditions were not distinguished"
    )
    # The three must still reconcile with the total every existing reader uses.
    assert m._haulier_acc["acme"]["rebate_arrivals_unpriced"] == 3


def test_unpriced_reasons_always_sum_to_the_total():
    """A drift between the sub-counters and the total would be silent, so pin it."""
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    m.accumulate_completed_trips(
        [_trip("t1", "acme"), _trip("t2", "acme", pickup_at=None, dropoff_at=None),
         _trip("t3", "acme", dropoff_fac=PORT, dropoff_at="rubbish")],
        END,
    )
    acc = m._haulier_acc["acme"]
    assert sum(_reasons(m, "acme")) == acc["rebate_arrivals_unpriced"]


def test_unpriced_reasons_reach_the_published_row():
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    m.accumulate_completed_trips([_trip("t1", "acme", dropoff_at=None)], END)
    (row,) = [r for r in m.build_breakdown("haulier", END) if r["id"] == "acme"]
    assert row["rebate_arrivals_unpriced_no_stamp"] == 1
    assert row["rebate_arrivals_unpriced_no_schedule"] == 0
    assert row["rebate_arrivals_unpriced_unparseable"] == 0


def _incomplete(truck_id, carrier, state="cancelled", **kw):
    t = _trip(truck_id, carrier, **kw)
    t["state"] = state
    return t


def test_forfeited_arrivals_are_counted_and_excluded_from_credit():
    """R2-4 / review F4. Earned at arrival, never paid, and previously invisible."""
    import apps.container_logistics.analytics.kpi_breakdown_persist as persist

    completed = [_trip("t1", "acme")]
    dead = [_incomplete("t2", "acme"), _incomplete("t3", "acme", state="expired")]
    m = _mgr(
        _facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
        trips=completed, incomplete_trips=dead,
    )
    original = persist.persist_kpi_breakdown
    persist.persist_kpi_breakdown = lambda *a, **k: None
    try:
        m.recompute_breakdowns_full(END)
    finally:
        persist.persist_kpi_breakdown = original

    acc = m._haulier_acc["acme"]
    # Two dead trips, each with ONE priceable arrival (the port leg).
    assert acc["rebate_arrivals_in_flight_at_horizon"] == 2
    assert acc["rebate_value_in_flight_at_horizon"] == 40.0
    # ...and none of it leaked into the money actually paid.
    assert acc["rebate_credited"] == 20.0
    assert acc["rebate_arrivals_priced"] == 1


def test_priced_plus_unpriced_no_longer_equals_two_times_completed():
    """The F4 regression check, stated as an INEQUALITY on purpose.

    Before the fix this identity held by construction, which is precisely why the
    denominator could never reveal forfeiture. If it ever holds again on a fixture with
    forfeited arrivals, the counter has been silently re-pinned.
    """
    import apps.container_logistics.analytics.kpi_breakdown_persist as persist

    completed = [_trip("t1", "acme")]
    dead = [_incomplete("t2", "acme"), _incomplete("t3", "acme")]
    m = _mgr(
        _facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
        trips=completed, incomplete_trips=dead,
    )
    original = persist.persist_kpi_breakdown
    persist.persist_kpi_breakdown = lambda *a, **k: None
    try:
        m.recompute_breakdowns_full(END)
    finally:
        persist.persist_kpi_breakdown = original

    acc = m._haulier_acc["acme"]
    accounted = (acc["rebate_arrivals_priced"] + acc["rebate_arrivals_unpriced"]
                 + acc["rebate_arrivals_in_flight_at_horizon"])
    assert accounted != 2 * len(completed), (
        "priced + unpriced + forfeited is still pinned to 2x completed — forfeiture is "
        "not actually being surfaced"
    )
    assert accounted == 2 * len(completed) + acc["rebate_arrivals_in_flight_at_horizon"]


def test_an_incomplete_trip_with_no_arrival_stamp_forfeits_nothing():
    """Only a RECORDED arrival can be forfeit — nothing was earned without one."""
    import apps.container_logistics.analytics.kpi_breakdown_persist as persist

    dead = [_incomplete("t2", "acme", pickup_at=None, dropoff_at=None)]
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
             trips=[_trip("t1", "acme")], incomplete_trips=dead)
    original = persist.persist_kpi_breakdown
    persist.persist_kpi_breakdown = lambda *a, **k: None
    try:
        m.recompute_breakdowns_full(END)
    finally:
        persist.persist_kpi_breakdown = original
    assert m._haulier_acc["acme"]["rebate_arrivals_in_flight_at_horizon"] == 0
    assert m._haulier_acc["acme"]["rebate_value_in_flight_at_horizon"] == 0.0


def test_forfeiture_is_not_double_counted_by_a_second_recompute():
    """The G29 trap again, on the newest counter."""
    import apps.container_logistics.analytics.kpi_breakdown_persist as persist

    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
             trips=[_trip("t1", "acme")],
             incomplete_trips=[_incomplete("t2", "acme")])
    original = persist.persist_kpi_breakdown
    persist.persist_kpi_breakdown = lambda *a, **k: None
    try:
        m.recompute_breakdowns_full(END)
        first = dict(m._haulier_acc["acme"])
        m.recompute_breakdowns_full(END)
    finally:
        persist.persist_kpi_breakdown = original
    acc = m._haulier_acc["acme"]
    assert acc["rebate_arrivals_in_flight_at_horizon"] == first["rebate_arrivals_in_flight_at_horizon"] == 1
    assert acc["rebate_value_in_flight_at_horizon"] == first["rebate_value_in_flight_at_horizon"] == 20.0


def test_planner_rows_carry_every_rebate_key():
    """R2-5 / the G30 trap, made self-maintaining.

    The expected set is DERIVED from ``_new_haulier_acc`` rather than listed, so a key
    added tomorrow is covered by this test today. A hardcoded list here would rot in
    exactly the way the production tuple rotted.
    """
    template = AnalyticsManager._new_haulier_acc("_", "_")
    numeric_rebate_keys = {
        k for k, v in template.items()
        if k.startswith("rebate_") and isinstance(v, (int, float))
        and not isinstance(v, bool)
    }
    assert len(numeric_rebate_keys) >= 10, "the split keys are missing from the template"

    summable = set(AnalyticsManager._planner_summable_keys())
    missing = numeric_rebate_keys - summable
    assert not missing, (
        f"these rebate keys would be silently dropped from planner rows: {sorted(missing)}"
    )


def test_planner_rows_aggregate_the_split_ledger_over_members():
    m = _mgr(_facility_docs(
        port_points=[{"hour": 16, "amount": 20.0}],
        depot_points=[{"hour": 18, "amount": 7.0}],
    ))
    m._coop_structure_id = "s1"
    m._coop_components = [{"acme", "borax"}]
    m.accumulate_completed_trips([_trip("t1", "acme"), _trip("t2", "borax")], END)

    (row,) = m.build_planner_breakdown(END)
    assert row["rebate_credited_pickup"] == 40.0
    assert row["rebate_credited_dropoff"] == 14.0
    assert row["rebate_credited"] == 54.0
    assert row["rebate_arrivals_priced"] == 4


# ===========================================================================
# Revision 2 — hardening (R2-10 review F9, R2-11 review F14)
# ===========================================================================


def test_no_code_path_patches_facility_rebate_during_a_run():
    """R3-12 / review R2-7 — TEST the invariant; the runtime digest assertion is declined.

    The analytics and assignment books are cached for a whole run, justified by
    "``profile.rebate`` is compiled data, never mutated during a run". True today, but a
    load-bearing invariant asserted only in a comment is worth nothing.

    The reviewer's *additional* proposal — a runtime assertion comparing the cached
    book's digest set on the next facility read — is REJECTED (plan §19.5) for the same
    reason digest-keying the cache was rejected in §18.5: it needs the facility page the
    cache exists to avoid, merely deferred to a different read, and it adds work to a
    live path to guard an invariant that holds. The scan is strengthened instead.

    The previous scan was line-by-line and rooted at ``apps/`` only, so four real
    mechanisms evaded it: a multi-line ``$set`` (the house style), a variable key, an
    Eve REST nested-dict patch, and anything under ``orsim/`` — where the generic
    resource-PATCH helper actually lives. Facility profiles demonstrably DO gain runtime
    keys (``avg_queue_wait_seconds``, ``peak_queue_length``), so the writer mechanism is
    real; the claim is only that none of them targets ``rebate``.
    """
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parent.parent
    roots = [repo / "apps"]

    # Locate orsim through the IMPORT SYSTEM, not as a sibling directory. This
    # scan used to look for `<workspace>/orsim/orsim`, which exists only on a
    # machine that happens to keep an engine source checkout next to this repo.
    # Since orsim became a released dependency it installs into site-packages,
    # so on any clean install the sibling was absent and this assertion fired --
    # correctly: the scan really would have narrowed. Resolving it by import
    # also scans the engine that will actually run, rather than a checkout that
    # may be at a different revision.
    import orsim as _orsim
    orsim_pkg = pathlib.Path(_orsim.__file__).resolve().parent
    assert orsim_pkg.is_dir(), f"orsim package dir not found at {orsim_pkg}"
    roots.append(orsim_pkg)          # where the generic resource PATCH helper lives
    assert len(roots) == 2, "orsim/ was not found; the scan would silently narrow"

    # Patterns run over the JOINED file text with DOTALL, so a $set spread across lines
    # cannot slip through a line-by-line matcher.
    patterns = [
        # profile["rebate"] = ... / doc["profile"]["rebate"] = ...
        (re.compile(r"""\[\s*["']rebate["']\s*\]\s*=[^=]"""), "direct index assignment"),
        # obj.rebate = ...
        (re.compile(r"""\.rebate\s*=[^=]"""), "attribute assignment"),
        # a Mongo/Eve update naming the dotted path, possibly across lines
        (re.compile(r"""\$set.{0,400}?["']profile\.rebate["']""", re.S), "$set on profile.rebate"),
        (re.compile(r"""["']profile\.rebate["'].{0,400}?\$set""", re.S), "$set on profile.rebate"),
        # a nested-dict PATCH body: {"profile": {..."rebate": ...}}
        (re.compile(r"""["']profile["']\s*:\s*\{.{0,300}?["']rebate["']\s*:""", re.S),
         "nested-dict patch body"),
        # a variable bound to the literal key, then used as an index
        (re.compile(r"""=\s*["']rebate["']\s*(?:#.*)?\n.{0,300}?\[[A-Za-z_][A-Za-z_0-9]*\]\s*=[^=]""", re.S),
         "variable-key assignment"),
    ]

    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            parts = path.parts
            if "datagen" in parts:
                continue  # generation is where the block is legitimately WRITTEN
            if "__pycache__" in parts or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "rebate" not in text:
                continue  # cheap pre-filter; every pattern requires the literal
            for rx, label in patterns:
                for hit in rx.finditer(text):
                    line_no = text.count("\n", 0, hit.start()) + 1
                    snippet = " ".join(hit.group(0).split())[:120]
                    offenders.append(f"{path}:{line_no}: [{label}] {snippet}")

    assert offenders == [], (
        "a runtime code path writes to profile.rebate, which invalidates the whole-run "
        "rebate book cache in analytics/manager.py and assignment/app.py:\n"
        + "\n".join(offenders)
    )


def test_the_book_cache_is_stable_across_windows_by_design():
    """The behaviour the invariant above licenses, pinned so it is deliberate.

    A second window must not re-page the facility collection. This is the counterpart of
    the assignment-side once-per-app test.
    """
    calls = {"n": 0}
    docs = _facility_docs(port_points=[{"hour": 16, "amount": 20.0}])
    m = _mgr(docs)
    inner = m._paged_where

    def _counting(base_url, where_clause, projection=None, **kw):
        if base_url == m._facility_url():
            calls["n"] += 1
        return inner(base_url, where_clause, projection=projection, **kw)

    m._paged_where = _counting
    m.accumulate_completed_trips([_trip("t1", "acme")], END)
    m.accumulate_completed_trips([_trip("t2", "acme")], END)
    assert calls["n"] == 1, f"facility collection paged {calls['n']} times across 2 windows"


def test_enabled_stamp_with_empty_book_is_loud(caplog):
    """R2-11 / review F14 — an all-zero ledger must never be silent.

    Facility documents that CARRY a rebate block but produce an empty book means the
    parse or the id join broke. Left silent, the run publishes a perfectly plausible
    zero. The failure is simulated at the id level, which is the real F14 trap: a book
    keyed by ObjectId and queried by str misses every facility.
    """
    import logging as _logging

    # R3-7: the alarm now keys on "documents came back with profiles, book is empty"
    # for the PROJECTION shape, and an unparseable block is still caught because such a
    # facility never enters the book. Here the projection itself failed — no `profile`
    # on any document — which is one of the two shapes that used to be SILENT.
    broken = [{"_id": PORT}, {"_id": DEPOT}]
    m = _mgr(broken)
    with caplog.at_level(_logging.ERROR):
        book = m.rebate_book()

    assert len(book) == 0
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "EMPTY" in text
    assert "PROJECTION" in text
    # ...and it must NOT be cached, or it freezes silent for the whole run.
    assert getattr(m, "_rebate_book_cache", None) is None


def test_a_genuinely_rebate_less_run_is_not_loud(caplog):
    """The normal steady state must stay quiet, or the loud check is just noise."""
    import logging as _logging

    m = _mgr([{"_id": BARE, "profile": {"gate_count": 4}}])
    with caplog.at_level(_logging.ERROR):
        book = m.rebate_book()
    assert len(book) == 0
    assert not [r for r in caplog.records if "EMPTY" in r.getMessage()]


def test_the_book_keys_and_looks_up_on_str_for_both_id_types():
    """R2-11 / F14 — the contract that makes a PyMongo-sourced book safe."""
    from bson import ObjectId

    from apps.container_logistics.rebate import RebateBook

    oid = ObjectId()
    book = RebateBook.from_facility_docs(
        [{"_id": oid, "profile": {"rebate": _schedule([{"hour": 16, "amount": 20.0}])}}]
    )
    assert book.price_at(str(oid), PORT_ARRIVAL) == 20.0
    assert book.price_at(oid, PORT_ARRIVAL) == 20.0


# ===========================================================================
# Revision 3 — R3-2/6/7/10/11
# ===========================================================================


def _no_persist():
    import apps.container_logistics.analytics.kpi_breakdown_persist as persist

    class _Ctx:
        def __enter__(self):
            self.orig = persist.persist_kpi_breakdown
            self.calls = []
            persist.persist_kpi_breakdown = lambda *a, **k: self.calls.append((a, k))
            return self

        def __exit__(self, *exc):
            persist.persist_kpi_breakdown = self.orig

    return _Ctx()


def _dead(truck_id, carrier, clock, **kw):
    t = _trip(truck_id, carrier, sim_clock=clock, **kw)
    t["state"] = "cancelled"
    return t


HORIZON = "Wed, 08 Jan 2020 06:00:00 GMT"
MID_RUN = "Fri, 03 Jan 2020 11:00:00 GMT"


def test_horizon_censoring_and_mid_run_abandonment_are_counted_separately():
    """R3-2 / review R2-1 (HIGH-2). Censoring is not loss, and must not be pooled with it.

    Verified on run_20260821_055316: **all 400** non-completed trips were cancelled at
    one instant, ``2020-01-08 06:00:00`` — the horizon. Cancel probability is zero, so
    nothing dies mid-run. A counter named "forfeited" therefore measured job attrition in
    a world that has none. The rename makes it unquotable as money lost; this split makes
    genuine attrition visible if it is ever introduced.
    """
    m = _mgr(
        _facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
        trips=[_trip("t1", "acme")],
        incomplete_trips=[
            _dead("t2", "acme", HORIZON),   # cut off by the horizon
            _dead("t3", "acme", HORIZON),
            _dead("t4", "acme", MID_RUN),   # genuinely abandoned
        ],
    )
    with _no_persist():
        m.recompute_breakdowns_full(END)

    acc = m._haulier_acc["acme"]
    assert acc["rebate_arrivals_in_flight_at_horizon"] == 2
    assert acc["rebate_value_in_flight_at_horizon"] == 40.0
    assert acc["rebate_arrivals_abandoned"] == 1
    assert acc["rebate_value_abandoned"] == 20.0
    # Neither touches the money actually paid.
    assert acc["rebate_credited"] == 20.0


def test_the_old_forfeiture_names_are_gone():
    """The rename must be complete — a survivor would be quoted as money lost."""
    acc = AnalyticsManager._new_haulier_acc("_", "_")
    assert "rebate_arrivals_forfeited" not in acc
    assert "rebate_forfeited_value" not in acc
    assert "rebate_value_in_flight_at_horizon" in acc


def test_stamp_flags_pure_horizon_censoring():
    """R3-2: the run-level verdict, published beside the rows that carry the number."""
    m = _mgr(
        _facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
        trips=[_trip("t1", "acme")],
        incomplete_trips=[_dead("t2", "acme", HORIZON), _dead("t3", "acme", HORIZON)],
    )
    with _no_persist() as cap:
        m.recompute_breakdowns_full(END)

    assert m._rebate_censoring["is_horizon_censoring"] is True
    assert m._rebate_censoring["non_completed_trips"] == 2
    assert m._rebate_censoring["terminal_instant"] is not None
    # ...and it reaches the persisted haulier document.
    haulier_extra = [k.get("extra") for _a, k in cap.calls if k.get("scope") == "haulier"]
    assert any(e and e.get("rebate_censoring", {}).get("is_horizon_censoring") is True
               for e in haulier_extra)


def test_stamp_does_not_claim_pure_censoring_when_a_trip_died_early():
    m = _mgr(
        _facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
        trips=[_trip("t1", "acme")],
        incomplete_trips=[_dead("t2", "acme", HORIZON), _dead("t3", "acme", MID_RUN)],
    )
    with _no_persist():
        m.recompute_breakdowns_full(END)
    assert m._rebate_censoring["is_horizon_censoring"] is False


def test_forfeiture_is_published_when_nothing_completed():
    """R3-6 / review R2-2 — the exact run where censoring is the whole story.

    The early return fired before the censoring scan, so a run in which NOTHING
    completed reported zero censored value and read as a clean empty result rather than
    a totally truncated one.
    """
    m = _mgr(
        _facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
        trips=[],                                  # nothing completed at all
        incomplete_trips=[_dead("t2", "acme", HORIZON)],
    )
    with _no_persist():
        m.recompute_breakdowns_full(END)

    acc = m._haulier_acc["acme"]
    assert acc["rebate_arrivals_in_flight_at_horizon"] == 1
    assert acc["rebate_value_in_flight_at_horizon"] == 20.0


def test_forfeit_only_haulier_publishes_a_currency_and_its_truck():
    """R3-11 / review R2-11 — a monetary quantity with a null unit and zero trucks."""
    m = _mgr(
        _facility_docs(port_points=[{"hour": 16, "amount": 20.0}]),
        trips=[],
        incomplete_trips=[_dead("t9", "solo", HORIZON)],
    )
    with _no_persist():
        m.recompute_breakdowns_full(END)

    (row,) = [r for r in m.build_breakdown("haulier", END) if r["id"] == "solo"]
    assert row["rebate_value_in_flight_at_horizon"] == 20.0
    assert row["rebate_currency"] == "credit", "money published with a null unit"
    assert row["num_trucks"] == 1, "the truck that made the arrival was not counted"


def test_missing_facility_id_is_counted_separately_from_missing_stamp():
    """R3-10 / review R2-10. A missing id is an ASSIGNMENT defect; a missing stamp is a
    TRUCK-side one. **This is the last scalar counter** — the set is frozen here."""
    m = _mgr(_facility_docs(port_points=[{"hour": 16, "amount": 20.0}]))
    t = _trip("t1", "acme", dropoff_at=None)                 # no stamp
    t2 = _trip("t2", "acme")
    t2["meta"]["dropoff_facility_resource_id"] = None        # stamp, no facility id
    m.accumulate_completed_trips([t, t2], END)

    acc = m._haulier_acc["acme"]
    assert acc["rebate_arrivals_unpriced_no_stamp"] == 1
    assert acc["rebate_arrivals_unpriced_no_facility_id"] == 1
    assert (acc["rebate_arrivals_unpriced_no_schedule"]
            + acc["rebate_arrivals_unpriced_no_stamp"]
            + acc["rebate_arrivals_unpriced_no_facility_id"]
            + acc["rebate_arrivals_unpriced_unparseable"]
            == acc["rebate_arrivals_unpriced"])


def test_the_unpriced_counter_set_is_frozen():
    """Plan §19.5 froze the counter set at four reasons. Pinned so it stays frozen."""
    acc = AnalyticsManager._new_haulier_acc("_", "_")
    reasons = {k for k in acc if k.startswith("rebate_arrivals_unpriced_")}
    assert reasons == {
        "rebate_arrivals_unpriced_no_schedule",
        "rebate_arrivals_unpriced_no_stamp",
        "rebate_arrivals_unpriced_no_facility_id",
        "rebate_arrivals_unpriced_unparseable",
    }, (
        "the unpriced counter set was frozen at four reasons (plan §19.5); a fifth "
        "scalar costs more than it explains — use a nested dict if more is truly needed"
    )


@pytest.mark.parametrize("label,docs,expect_error,expect_cached", [
    ("id-join / where failure -> zero docs", [], False, False),
    ("projection drops profile", [{"_id": PORT}, {"_id": DEPOT}], True, False),
    ("genuinely rebate-less (profile, no rebate)",
     [{"_id": PORT, "profile": {"gate_count": 4}}], False, True),
    ("unparseable block",
     [{"_id": PORT, "profile": {"rebate": {"points": [{"hour": 99, "amount": 1.0}]}}}],
     False, True),
])
def test_projection_failure_alarms_and_does_not_cache(label, docs, expect_error,
                                                      expect_cached, caplog):
    """R3-7 / review R2-6, using his probe D3 table as the fixture.

    The old check required a *declared* rebate block before it would alarm, which made
    it silent for BOTH causes its own message named. Note the deliberate deviation from
    §19.4's literal "refuse to cache whenever documents were returned": that would page
    all 300 facility documents once per TRIP on the 12 rebate-less scenarios (measured:
    50 trips -> 50 pages). The projection shape is discriminated instead, which is what
    the finding is actually about.
    """
    import logging as _logging

    m = _mgr(docs)
    with caplog.at_level(_logging.ERROR):
        book = m.rebate_book()
    assert len(book) == 0, label

    errored = any("EMPTY" in r.getMessage() for r in caplog.records)
    assert errored is expect_error, f"{label}: error={errored}, expected {expect_error}"
    cached = getattr(m, "_rebate_book_cache", None) is not None
    assert cached is expect_cached, f"{label}: cached={cached}, expected {expect_cached}"


def test_a_rebate_less_run_pages_the_facility_collection_once():
    """The cost the R3-7 deviation protects, pinned so it cannot regress."""
    docs = [{"_id": f"f{i}", "profile": {"gate_count": 4}} for i in range(20)]
    m = _mgr(docs)
    calls = {"n": 0}
    inner = m._paged_where

    def _counting(base_url, where_clause, projection=None, **kw):
        if base_url == m._facility_url():
            calls["n"] += 1
        return inner(base_url, where_clause, projection=projection, **kw)

    m._paged_where = _counting
    m.accumulate_completed_trips([_trip(f"t{i}", "acme") for i in range(25)], END)
    assert calls["n"] == 1, (
        f"a rebate-less run paged the facility collection {calls['n']} times for 25 "
        "trips; that is the per-lookup HTTP cost plan §3.1 rejected option (b) over"
    )


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
