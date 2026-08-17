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
