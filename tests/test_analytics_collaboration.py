"""Gains attribution + planner-scope rows in AnalyticsManager (plan A5).

Exercises accumulate_completed_trips with meta.collaboration trips and the
planner breakdown aggregation, without the REST/Kafka machinery.
"""

from datetime import datetime, timezone

from apps.container_logistics.analytics.manager import AnalyticsManager


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _mgr():
    m = AnalyticsManager.__new__(AnalyticsManager)
    m._truck_acc = {}
    m._haulier_acc = {}
    m._lane_acc = {}
    m._idle_seconds_sum = 0.0
    m._idle_count = 0
    m._run_start_time = datetime(2020, 1, 1, 8, 0, tzinfo=timezone.utc)
    m._metric_window_start = m._run_start_time
    m._metric_window_end = None
    m._shared_trip_count = 0
    m._total_benefit_km = 0.0
    m._coop_structure_id = None
    m._coop_components = []
    return m


def _trip(truck_id, carrier, *, collab=None, sim_clock="2020-01-01 09:00:00"):
    meta = {
        "truck_profile": {"haulier_id": carrier, "haulier_name": carrier.title()},
        "order_profile": {},
        "pickup_facility_resource_id": None,
        "dropoff_facility_resource_id": None,
        "reposition_origin_loc": _pt(103.80, 1.30),
    }
    if collab:
        meta["collaboration"] = collab
    return {
        "truck": truck_id,
        "sim_clock": sim_clock,
        "state": "completed",
        "pickup_loc": _pt(103.85, 1.30),
        "dropoff_loc": _pt(103.90, 1.30),
        "meta": meta,
        "routes": {"planned": {}, "actual": {}},
    }


END = datetime(2020, 1, 1, 20, 0, tzinfo=timezone.utc)


def test_gains_double_entry_attribution():
    m = _mgr()
    trips = [
        _trip("t1", "acme"),  # plain own-fleet haul
        _trip(
            "t2", "borax",
            collab={"shared": True, "owner_haulier_id": "acme",
                    "carrier_haulier_id": "borax", "benefit_km": 7.5},
        ),
        _trip(
            "t3", "borax",
            collab={"shared": True, "owner_haulier_id": "acme",
                    "carrier_haulier_id": "borax", "benefit_km": None},
        ),
    ]
    m.accumulate_completed_trips(trips, END)

    acme = m._haulier_acc["acme"]
    borax = m._haulier_acc["borax"]
    assert acme["jobs_shared_out"] == 2
    assert acme["benefit_km_received"] == 7.5
    assert acme["unserveable_shares"] == 1
    assert borax["jobs_carried_for_partners"] == 2
    # Carrier keeps the km/trip_count (trips ran on borax trucks).
    assert borax["trip_count"] == 2 and acme["trip_count"] == 1
    assert m.shared_trip_rate() == 2 / 3
    assert m.total_benefit_km() == 7.5
    # Rows surface the gains fields.
    rows = m.build_breakdown("haulier", END)
    by_id = {r["id"]: r for r in rows}
    assert by_id["acme"]["jobs_shared_out"] == 2
    assert by_id["borax"]["jobs_carried_for_partners"] == 2


def test_planner_scope_aggregates_components():
    m = _mgr()
    m.set_cooperation({
        "structure_id": "pair",
        "components": [["acme", "borax"], ["cargo"]],  # singleton must be skipped
    })
    m.accumulate_completed_trips(
        [
            _trip("t1", "acme"),
            _trip("t2", "borax",
                  collab={"shared": True, "owner_haulier_id": "acme",
                          "carrier_haulier_id": "borax", "benefit_km": 3.0}),
            _trip("t4", "cargo"),
        ],
        END,
    )
    rows = m.build_planner_breakdown(END)
    assert len(rows) == 1, "singleton components must not become planner rows"
    row = rows[0]
    assert row["id"] == "planner:acme+borax"
    assert row["members"] == ["acme", "borax"]
    assert row["structure_id"] == "pair"
    assert row["num_orders_completed"] == 2  # acme's 1 + borax's 1 (cargo excluded)
    assert row["jobs_shared_out"] == 1 and row["jobs_carried_for_partners"] == 1
    assert row["benefit_km_received"] == 3.0


def test_no_structure_means_no_planner_rows():
    m = _mgr()
    m.accumulate_completed_trips([_trip("t1", "acme")], END)
    assert m.build_planner_breakdown(END) == []


def test_roster_seed_repairs_owner_created_name():
    # An owner acc created by a shared-job credit only knows the id; the roster
    # seed must repair the display name (S1 regression, seen live on borax).
    m = _mgr()
    m.accumulate_completed_trips(
        [_trip("t9", "acme",
               collab={"shared": True, "owner_haulier_id": "borax",
                       "carrier_haulier_id": "acme", "benefit_km": 1.0})],
        END,
    )
    assert m._haulier_acc["borax"]["haulier_name"] == "borax"  # frozen as id
    m._haulier_roster_cache = {
        "acme": {"name": "Acme", "truck_ids": {"t9"}},
        "borax": {"name": "Borax", "truck_ids": {"t10"}},
    }
    m._seed_haulier_roster()
    assert m._haulier_acc["borax"]["haulier_name"] == "Borax"


def test_benefit_aggregate_reports_undefined_count_and_does_not_impute():
    """R3-11 / review LOW-14. `benefit_km` is None on the MAJORITY of shared awards
    and that is a FINDING, not missing data: it means the owner had no free feasible
    truck, so the share was strictly ENABLING.

    The contract: never impute a value, report the mean over the DEFINED subset
    only, and always ship both counts so the denominator cannot be mistaken.
    """
    from apps.container_logistics.analytics.manager import AnalyticsManager

    acc = AnalyticsManager._new_haulier_acc("acme", "acme")
    # Two enabling shares (no benefit number) and one measurable 10 km share.
    acc["jobs_shared_out"] = 3
    acc["benefit_km_received"] = 10.0
    acc["benefit_defined_n"] = 1
    acc["unserveable_shares"] = 2
    acc["jobs_enabled"] = 2

    row = AnalyticsManager._entity_row(acc, 1.0)
    assert row["benefit_defined_n"] == 1
    assert row["benefit_undefined_n"] == 2
    assert row["jobs_enabled"] == 2
    # The mean is over the defined subset ONLY: 10.0 / 1, never 10.0 / 3.
    assert row["mean_benefit_km"] == 10.0, (
        f"mean was diluted across undefined shares: {row['mean_benefit_km']}"
    )
    assert row["benefit_km_received"] == 10.0


def test_mean_benefit_is_none_rather_than_zero_when_nothing_is_defined():
    """A 0.0 mean would read as 'cooperation saved nothing'; None reads as
    'no measurable share', which is the truth."""
    from apps.container_logistics.analytics.manager import AnalyticsManager

    acc = AnalyticsManager._new_haulier_acc("acme", "acme")
    acc["jobs_shared_out"] = 2
    acc["unserveable_shares"] = 2
    acc["jobs_enabled"] = 2
    row = AnalyticsManager._entity_row(acc, 1.0)
    assert row["mean_benefit_km"] is None
    assert row["benefit_defined_n"] == 0


def test_planner_mean_benefit_km_is_not_null_beside_a_nonzero_numerator():
    """R3-3 / rebate review R2-9 — a COOPERATION defect that rebate work surfaced.

    ``_PLANNER_NON_SUMMABLE`` excluded ``benefit_defined_n`` and ``jobs_enabled`` with
    the note "published via ``_entity_row``'s own logic". That justification was false:
    ``_entity_row`` reads both straight off the accumulator. So a planner row summed
    ``benefit_km_received`` across its members while its denominator stayed at the
    freshly-built zero, and ``mean_benefit_km`` published as ``None`` beside a non-zero
    numerator — breaking the "never impute, always publish the denominator" contract on
    a cooperation metric, not a rebate one.

    This test lives cooperation-side deliberately: the number it protects is
    ``mean_benefit_km``, and it must not be guarded from inside a rebate branch.
    """
    m = _mgr()
    m.set_cooperation({"structure_id": "pair", "components": [["acme", "borax"]]})
    m.accumulate_completed_trips(
        [
            _trip("t1", "borax",
                  collab={"shared": True, "owner_haulier_id": "acme",
                          "carrier_haulier_id": "borax", "benefit_km": 4.0}),
            _trip("t2", "borax",
                  collab={"shared": True, "owner_haulier_id": "acme",
                          "carrier_haulier_id": "borax", "benefit_km": 6.0}),
        ],
        END,
    )

    (row,) = m.build_planner_breakdown(END)
    assert row["benefit_km_received"] == 10.0, "the numerator must still aggregate"
    assert row["benefit_defined_n"] == 2, (
        "the DENOMINATOR was dropped from the planner aggregate, so the mean below "
        "cannot be computed and publishes as None beside a non-zero numerator"
    )
    assert row["mean_benefit_km"] == 5.0
    assert row["jobs_enabled"] == 0


def test_planner_jobs_enabled_aggregates_over_members():
    """The other key the false comment excluded."""
    m = _mgr()
    m.set_cooperation({"structure_id": "pair", "components": [["acme", "borax"]]})
    m.accumulate_completed_trips(
        [
            _trip("t1", "borax",
                  collab={"shared": True, "owner_haulier_id": "acme",
                          "carrier_haulier_id": "borax", "benefit_km": None}),
        ],
        END,
    )
    (row,) = m.build_planner_breakdown(END)
    assert row["jobs_enabled"] == 1
    assert row["benefit_defined_n"] == 0
    assert row["mean_benefit_km"] is None, "no defined benefit => no mean, never zero"
