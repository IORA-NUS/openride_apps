import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from apps.common.resource_client_mixin import ResourceClientMixin, get_http_session
from apps.config import kpi_ecosystems, settings, simulation_domains
from apps.container_logistics.statemachine import HaulTripStateMachine, OrderStateMachine
from apps.utils.kpi_save import save_kpi_batch
from apps.utils.kpi_catalog_client import KpiCatalogClient
from orsim.lifecycle import ORSimManager

logger = logging.getLogger(__name__)


def _sim_clock_http_param(value: Any) -> str:
    """RFC1123 GMT string for Eve ``where`` / aggregation ``sim_clock`` filters."""
    if isinstance(value, datetime):
        return value.strftime("%a, %d %b %Y %H:%M:%S GMT")
    if isinstance(value, str):
        return value
    raise TypeError(f"Unsupported sim_clock type: {type(value)!r}")


class AnalyticsManager(ResourceClientMixin, ORSimManager):
    """Fetches haul-trip and order snapshots from OpenRide and persists KPI rows."""

    _final_haul_states = (HaulTripStateMachine.completed.name, HaulTripStateMachine.cancelled.name)
    _final_order_states = (OrderStateMachine.completed.name, OrderStateMachine.cancelled.name)

    def __init__(self, run_id, sim_clock, user, persona):
        self.run_id = run_id
        self.sim_clock = sim_clock
        self.user = user
        self.persona = persona
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        self.resource = {}
        self._kpi_catalog = KpiCatalogClient(kpi_ecosystems["container_logistics"], user)
        self._metric_window_start: Optional[Any] = None
        self._metric_window_end: Optional[Any] = None
        self._aggregate_counts_enabled: Optional[bool] = None
        # Cumulative per-entity accumulators for distribution KPIs + breakdown artifact.
        # Keyed by truck id / haulier id; updated incrementally from each window's completed
        # trips so we never re-scan the whole run per tick. See accumulate_completed_trips().
        self._truck_acc: Dict[str, Dict[str, Any]] = {}
        self._haulier_acc: Dict[str, Dict[str, Any]] = {}
        # Directed loaded-lane accumulator keyed by (from_facility, to_facility): loaded-trip
        # counts per haulier + endpoint coords. Paired into undirected backhaul matches for the
        # shared-lane map (two hauliers running loaded in opposite directions on one corridor).
        self._lane_acc: Dict[tuple, Dict[str, Any]] = {}
        # Cumulative idle-duration accumulator. Folded incrementally from each window's
        # newly-ended idle trips so avg_idle is a run-wide cumulative gauge (like the other
        # distribution KPIs) instead of a per-window snapshot that collapses to 0 in any
        # window where no idle trip happens to end (notably the final window).
        self._idle_seconds_sum: float = 0.0
        self._idle_count: int = 0
        self._run_start_time: Optional[Any] = None
        # Collaboration (haulier job sharing): run-wide counters for the scalar
        # KPIs, plus the active structure's planner groups (set by the analytics
        # app from orsim_settings["COOPERATION"]; empty => no planner scope rows).
        self._shared_trip_count: int = 0
        self._total_benefit_km: float = 0.0
        self._coop_structure_id: Optional[str] = None
        self._coop_components: List[List[str]] = []

    def set_cooperation(self, coop: Optional[Dict[str, Any]]) -> None:
        """Receive the active cooperation structure (shipped via orsim_settings).

        Only multi-member components become planner groups — a solo haulier's
        "planner" is just the company itself (already a haulier row)."""
        if not isinstance(coop, dict):
            return
        self._coop_structure_id = coop.get("structure_id")
        comps = coop.get("components") or []
        self._coop_components = [
            [str(m) for m in members]
            for members in comps
            if isinstance(members, (list, tuple)) and len(members) > 1
        ]

    def on_init(self):
        pass

    def login(self, sim_clock):
        pass

    def logout(self, sim_clock):
        pass

    def set_metric_window(self, start_time: Any, end_time: Any) -> None:
        """Analytics interval ``[start_time, end_time)`` for window-scoped KPI queries."""
        self._metric_window_start = start_time
        self._metric_window_end = end_time

    @property
    def metric_window_start(self) -> Optional[Any]:
        return self._metric_window_start

    @property
    def metric_window_end(self) -> Optional[Any]:
        return self._metric_window_end

    def _haul_trip_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/truck/trip"

    def _truck_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/truck"

    def _order_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/order"

    def _kpi_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/kpi"

    def _get(self, url, params=None):
        response = get_http_session().get(
            url,
            headers=self.user.get_headers(),
            params=params or {},
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )
        self._check_response(response)
        return response.json()

    def _post(self, url, data):
        response = get_http_session().post(
            url,
            headers=self.user.get_headers(),
            data=json.dumps(data),
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )
        self._check_response(response)
        return response.json()

    def _try_count_aggregate(self, url: str, aggregate: Dict[str, Any]) -> Optional[int]:
        """Single-request Mongo aggregation count via Eve, or None if unavailable."""
        if self._aggregate_counts_enabled is False:
            return None
        try:
            result = self._get(url, params={"aggregate": json.dumps(aggregate)})
        except Exception as exc:
            if self._aggregate_counts_enabled is not False:
                logger.warning(
                    "Aggregate KPI count unavailable url=%s (%s) — "
                    "using paginated fallback (restart OpenRide API to enable fast counts)",
                    url,
                    exc,
                )
                self._aggregate_counts_enabled = False
            return None
        self._aggregate_counts_enabled = True
        items = result.get("_items") or []
        if not items:
            return 0
        row = items[0]
        for key in ("num_items", "num_trips", "count"):
            if key in row:
                try:
                    return int(row[key])
                except (TypeError, ValueError):
                    return 0
        return 0

    def _legacy_count_orders_by_state(self, state: str) -> int:
        return len(
            self._paged_where(
                self._order_url(),
                {"$and": [{"run_id": self.run_id}, {"state": state}]},
                projection={"_id": 1},
            )
        )

    def _legacy_count_active_orders(self) -> int:
        return len(
            self._paged_where(
                self._order_url(),
                {
                    "$and": [
                        {"run_id": self.run_id},
                        {
                            "state": {
                                "$nin": list(self._final_order_states)
                                + [OrderStateMachine.created.name]
                            }
                        },
                    ]
                },
                projection={"_id": 1},
            )
        )

    def _legacy_count_haul_trips_by_state(self, state: str) -> int:
        return len(
            self._paged_where(
                self._haul_trip_url(),
                {"$and": [{"run_id": self.run_id}, {"state": state}]},
                projection={"_id": 1},
            )
        )

    def count_orders_by_state(self, state: str) -> int:
        """Cumulative completed (or other terminal) orders for the run."""
        counted = self._try_count_aggregate(
            f"{self._order_url()}/count_by_state",
            {"$run_id": self.run_id, "$state": state},
        )
        if counted is not None:
            return counted
        return self._legacy_count_orders_by_state(state)

    def count_active_orders(self) -> int:
        """Orders not in a terminal state (snapshot gauge)."""
        counted = self._try_count_aggregate(
            f"{self._order_url()}/count_active",
            {"$run_id": self.run_id},
        )
        if counted is not None:
            return counted
        return self._legacy_count_active_orders()

    def count_orders_in_window(self, state: str, start_time: Any, end_time: Any) -> int:
        """Orders in ``state`` with ``sim_clock`` in ``[start_time, end_time)``."""
        counted = self._try_count_aggregate(
            f"{self._order_url()}/count_in_window",
            {
                "$run_id": self.run_id,
                "$state": state,
                "$sim_clock_gte": _sim_clock_http_param(start_time),
                "$sim_clock_lt": _sim_clock_http_param(end_time),
            },
        )
        if counted is not None:
            return counted
        return len(
            self.fetch_orders_in_window(start_time, end_time, states=[state])
        )

    def count_haul_trips_by_state(self, state: str) -> int:
        """Cumulative haul trips in ``state`` for the run."""
        counted = self._try_count_aggregate(
            f"{self._haul_trip_url()}/count_by_state",
            {"$run_id": self.run_id, "$state": state},
        )
        if counted is not None:
            return counted
        return self._legacy_count_haul_trips_by_state(state)

    def count_active_haul_trucks(self) -> int:
        """Distinct trucks with a non-terminal haul trip (snapshot gauge)."""
        counted = self._try_count_aggregate(
            f"{self._haul_trip_url()}/count_active_trucks",
            {"$run_id": self.run_id},
        )
        if counted is not None:
            return counted
        rows = self._paged_where(
            self._haul_trip_url(),
            {
                "$and": [
                    {"run_id": self.run_id},
                    {"state": {"$nin": list(self._final_haul_states)}},
                ]
            },
            projection={"truck": 1},
        )
        return self.active_haul_truck_count_from_rows(rows)

    def count_haul_trips_in_window(self, state: str, start_time: Any, end_time: Any) -> int:
        """Haul trips in ``state`` with ``sim_clock`` in ``[start_time, end_time)``."""
        counted = self._try_count_aggregate(
            f"{self._haul_trip_url()}/count_in_window",
            {
                "$run_id": self.run_id,
                "$state": state,
                "$sim_clock_gte": _sim_clock_http_param(start_time),
                "$sim_clock_lt": _sim_clock_http_param(end_time),
            },
        )
        if counted is not None:
            return counted
        return len(
            self.fetch_haul_trips_in_window(start_time, end_time, states=[state])
        )

    def _paged_where(
        self,
        base_url: str,
        where_clause: Dict[str, Any],
        projection: Optional[Dict[str, Any]] = None,
        *,
        page_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        batch_size = page_size or 500
        while True:
            params: Dict[str, Any] = {
                "where": json.dumps(where_clause),
                "page": page,
                "max_results": batch_size,
            }
            if projection is not None:
                params["projection"] = json.dumps(projection)
            result = self._get(base_url, params)
            batch = result.get("_items") or []
            if not batch:
                break
            items.extend(batch)
            page += 1
        return items

    def fetch_orders_in_window(
        self,
        start_time: Any,
        end_time: Any,
        *,
        states: Optional[List[str]] = None,
        projection: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Paginated order rows in the analytics interval (for calculated KPIs)."""
        state_clause: Any
        if states:
            state_clause = {"$in": list(states)}
        else:
            state_clause = {"$exists": True}
        return self._paged_where(
            self._order_url(),
            {
                "$and": [
                    {"run_id": self.run_id},
                    {"state": state_clause},
                    {
                        "sim_clock": {
                            "$gte": _sim_clock_http_param(start_time),
                            "$lt": _sim_clock_http_param(end_time),
                        }
                    },
                ]
            },
            projection=projection,
        )

    def fetch_haul_trips_in_window(
        self,
        start_time: Any,
        end_time: Any,
        *,
        states: Optional[List[str]] = None,
        projection: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Paginated haul-trip rows in the analytics interval (for calculated KPIs)."""
        state_clause: Any
        if states:
            state_clause = {"$in": list(states)}
        else:
            state_clause = {"$exists": True}
        return self._paged_where(
            self._haul_trip_url(),
            {
                "$and": [
                    {"run_id": self.run_id},
                    {"state": state_clause},
                    {
                        "sim_clock": {
                            "$gte": _sim_clock_http_param(start_time),
                            "$lt": _sim_clock_http_param(end_time),
                        }
                    },
                ]
            },
            projection=projection,
        )

    @staticmethod
    def _haversine_km(loc_a: Any, loc_b: Any) -> Optional[float]:
        """Straight-line distance in km between two GeoJSON Point dicts."""
        def _coords(loc: Any):
            if not isinstance(loc, dict):
                return None
            c = loc.get("coordinates")
            if not isinstance(c, (list, tuple)) or len(c) < 2:
                return None
            try:
                return float(c[0]), float(c[1])
            except (TypeError, ValueError):
                return None

        a = _coords(loc_a)
        b = _coords(loc_b)
        if a is None or b is None:
            return None
        lon1, lat1 = math.radians(a[0]), math.radians(a[1])
        lon2, lat2 = math.radians(b[0]), math.radians(b[1])
        dlon, dlat = lon2 - lon1, lat2 - lat1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))

    def _facility_url(self) -> str:
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/facility"

    def _idle_trip_url(self) -> str:
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/truck_idle_trip"

    def compute_avg_queue_wait_seconds(self) -> float:
        """Mean queue wait across all facilities (seconds). Reads from patched facility profiles."""
        facilities = self._paged_where(
            self._facility_url(),
            {"run_id": self.run_id},
            projection={"profile": 1},
        )
        values = [
            float(f.get("profile", {}).get("avg_queue_wait_seconds") or 0)
            for f in facilities
        ]
        return sum(values) / len(values) if values else 0.0

    def compute_peak_queue_length(self) -> int:
        """Maximum queue length seen across all facilities."""
        facilities = self._paged_where(
            self._facility_url(),
            {"run_id": self.run_id},
            projection={"profile": 1},
        )
        peaks = [
            int(f.get("profile", {}).get("peak_queue_length") or 0)
            for f in facilities
        ]
        return max(peaks) if peaks else 0

    def accumulate_idle_trips(self, start_time: Any, end_time: Any) -> None:
        """Fold this window's newly-ended idle trips into the cumulative idle accumulator.

        Fetches only the idle trips that ended in [start_time, end_time) — the windows are
        contiguous and non-overlapping, so each ended idle trip is counted exactly once across
        the run. Keeps the cost incremental (no growing full-collection re-scan), matching the
        accumulate_completed_trips pattern.
        """
        trips = self._paged_where(
            self._idle_trip_url(),
            {
                "$and": [
                    {"run_id": self.run_id},
                    {"state": "ended"},
                    {
                        "sim_clock": {
                            "$gte": _sim_clock_http_param(start_time),
                            "$lt": _sim_clock_http_param(end_time),
                        }
                    },
                ]
            },
            projection={"start_sim_clock": 1, "sim_clock": 1},
        )
        for trip in trips:
            raw_start = trip.get("start_sim_clock")
            raw_end = trip.get("sim_clock")
            if not raw_start or not raw_end:
                continue
            try:
                from apps.utils import str_to_time
                s = str_to_time(raw_start)
                e = str_to_time(raw_end)
                if s.tzinfo is None:
                    s = s.replace(tzinfo=timezone.utc)
                if e.tzinfo is None:
                    e = e.replace(tzinfo=timezone.utc)
                dt = (e - s).total_seconds()
                if dt > 0:
                    self._idle_seconds_sum += dt
                    self._idle_count += 1
            except Exception:
                pass

    def compute_avg_idle_time_seconds(self) -> float:
        """Run-wide cumulative mean idle period duration (seconds).

        Returns the average over *all* idle trips ended so far (via accumulate_idle_trips),
        not just the current window — so the gauge is meaningful run-to-date and never
        collapses to 0 in a window with no idle-trip ends. 0.0 only when no idle trip has
        ever ended.
        """
        if self._idle_count <= 0:
            return 0.0
        return self._idle_seconds_sum / self._idle_count

    def compute_orders_per_truck(self) -> float:
        """Cumulative completed haul trips divided by total fleet size."""
        from apps.container_logistics.scenario.scenario_config import truck_settings
        total_completed = self.count_haul_trips_by_state(HaulTripStateMachine.completed.name)
        num_trucks = truck_settings.get("num_trucks", 1)
        return total_completed / max(1, num_trucks)

    # Projection covering every field the distribution KPIs + breakdown artifact need from a
    # completed haul trip (superset of the avg-empty-distance projection).
    _COMPLETED_TRIP_PROJECTION = {
        "truck": 1,
        "meta": 1,
        "current_loc": 1,
        "pickup_loc": 1,
        "dropoff_loc": 1,
        "routes": 1,
        "stats": 1,
        "sim_clock": 1,
    }

    def fetch_completed_trips_window(self, start_time: Any, end_time: Any) -> List[Dict[str, Any]]:
        """Completed haul trips in [start_time, end_time) with the full breakdown projection."""
        return self.fetch_haul_trips_in_window(
            start_time,
            end_time,
            states=[HaulTripStateMachine.completed.name],
            projection=self._COMPLETED_TRIP_PROJECTION,
        )

    @staticmethod
    def _empty_km_of(trip: Dict[str, Any]) -> Optional[float]:
        """Empty/deadhead km for a trip: prefer OSRM road distance, straight-line fallback.

        Fallback measures from ``meta.reposition_origin_loc`` (truck position at assignment), NOT
        ``current_loc`` — on a completed trip current_loc has advanced to the dropoff, which would
        mirror the loaded leg and peg every deadhead ratio at 0.5.
        """
        try:
            osrm_m = float(trip["routes"]["planned"]["repositioning_to_pickup"]["distance"])
            if osrm_m >= 0:
                return osrm_m / 1000.0
        except (KeyError, TypeError, ValueError):
            pass
        origin = (trip.get("meta") or {}).get("reposition_origin_loc") or trip.get("current_loc")
        return AnalyticsManager._haversine_km(origin, trip.get("pickup_loc"))

    @staticmethod
    def _loaded_km_of(trip: Dict[str, Any]) -> Optional[float]:
        """Loaded km for a trip: prefer OSRM road distance, straight-line fallback."""
        try:
            osrm_m = float(trip["routes"]["planned"]["loaded_to_dropoff"]["distance"])
            if osrm_m >= 0:
                return osrm_m / 1000.0
        except (KeyError, TypeError, ValueError):
            pass
        return AnalyticsManager._haversine_km(trip.get("pickup_loc"), trip.get("dropoff_loc"))

    def compute_avg_empty_distance_km(
        self,
        start_time: Any,
        end_time: Any,
        trips: Optional[List[Dict[str, Any]]] = None,
    ) -> float:
        """Mean empty repositioning distance (km) for haul trips completed in [start_time, end_time).

        Accepts a pre-fetched ``trips`` list to avoid re-querying the same window already read
        by :meth:`accumulate_completed_trips`.
        """
        if trips is None:
            trips = self.fetch_completed_trips_window(start_time, end_time)
        distances: List[float] = []
        for trip in trips:
            km = self._empty_km_of(trip)
            if km is not None:
                distances.append(km)
        return sum(distances) / len(distances) if distances else 0.0

    # ── Per-entity distribution accumulators ──────────────────────────────────

    @staticmethod
    def _haulier_of_trip(trip: Dict[str, Any]) -> tuple:
        """(haulier_id, haulier_name) for a trip; falls back to ``unknown``."""
        prof = (trip.get("meta") or {}).get("truck_profile") or {}
        hid = prof.get("haulier_id") or "unknown"
        hname = prof.get("haulier_name") or str(hid)
        return str(hid), str(hname)

    @staticmethod
    def _new_truck_acc(truck_id: str, haulier_id: str, haulier_name: str) -> Dict[str, Any]:
        return {
            "id": truck_id,
            "haulier_id": haulier_id,
            "haulier_name": haulier_name,
            "empty_km": 0.0,
            "loaded_km": 0.0,
            "trip_count": 0,
            "active_seconds": 0.0,
            "dual_cycle_count": 0,
            "chain_opportunities": 0,
            "last_dropoff_facility_id": None,
            "last_dropoff_loc": None,
        }

    @staticmethod
    def _new_haulier_acc(haulier_id: str, haulier_name: str) -> Dict[str, Any]:
        return {
            "id": haulier_id,
            "haulier_name": haulier_name,
            "empty_km": 0.0,
            "loaded_km": 0.0,
            "trip_count": 0,
            "active_seconds": 0.0,
            "dual_cycle_count": 0,
            "chain_opportunities": 0,
            "trucks": set(),
            # Collaboration gains (double-entry: owner + carrier both credited).
            # From trip meta.collaboration set at assignment (haulier job sharing).
            "jobs_shared_out": 0,           # as OWNER: my jobs a partner carried
            "jobs_carried_for_partners": 0, # as CARRIER: partner jobs my trucks ran
            "benefit_km_received": 0.0,     # as OWNER: deadhead saved vs my best own option
            "unserveable_shares": 0,        # as OWNER: shares where I had no free truck
        }

    def _trip_sort_key(self, trip: Dict[str, Any]):
        from apps.utils import str_to_time

        raw = trip.get("sim_clock")
        try:
            t = str_to_time(raw)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return t
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    def fleet_haulier_roster(self) -> Dict[str, Dict[str, Any]]:
        """``{haulier_id: {"name": str, "truck_ids": set(str)}}`` for the whole run fleet.

        The breakdown is built purely from completed-trip accumulators, so a haulier stays
        invisible until it lands its first trip — low-volume hauliers can first appear on
        day 2. We read the (fixed) roster once from the pre-created truck collection so every
        configured company shows from the first snapshot. ``truck_ids`` are ``str(_id)`` to
        match ``str(trip["truck"])`` in :meth:`accumulate_completed_trips`, so seeding a
        haulier's ``trucks`` set yields the correct ``num_trucks`` and never double-counts.
        """
        cached = getattr(self, "_haulier_roster_cache", None)
        if cached is not None:
            return cached
        roster: Dict[str, Dict[str, Any]] = {}
        try:
            trucks = self._paged_where(
                self._truck_url(),
                {"$and": [{"run_id": self.run_id}]},
                projection={"profile.haulier_id": 1, "profile.haulier_name": 1},
            )
        except Exception as exc:
            logger.warning("fleet_haulier_roster fetch failed (%s) — breakdown will seed lazily", exc)
            return {}
        for truck in trucks:
            prof = truck.get("profile") or {}
            hid = prof.get("haulier_id")
            tid = truck.get("_id")
            if not hid:
                continue
            entry = roster.get(str(hid))
            if entry is None:
                entry = {"name": str(prof.get("haulier_name") or hid), "truck_ids": set()}
                roster[str(hid)] = entry
            if tid is not None:
                entry["truck_ids"].add(str(tid))
        # Only cache a non-empty roster: an empty result means trucks aren't created yet
        # (or the read failed), so we retry on the next flush rather than freezing empty.
        if roster:
            self._haulier_roster_cache = roster
        return roster

    def _seed_haulier_roster(self) -> None:
        """Ensure every fleet haulier has an accumulator entry (zero metrics, full truck count)
        so the Companies breakdown lists all companies from the first snapshot."""
        for hid, entry in self.fleet_haulier_roster().items():
            acc = self._haulier_acc.get(hid)
            if acc is None:
                acc = self._new_haulier_acc(hid, entry["name"])
                self._haulier_acc[hid] = acc
            elif acc.get("haulier_name") == hid and entry["name"] != hid:
                # The acc was first created from a shared-job credit (owner side),
                # which only knows the id — repair the display name from the roster.
                acc["haulier_name"] = entry["name"]
            # Union in the fleet truck ids; accumulate_completed_trips adds the same str(_id),
            # so this fixes num_trucks immediately without double-counting active trucks.
            acc["trucks"].update(entry["truck_ids"])

    def accumulate_completed_trips(self, trips: List[Dict[str, Any]], end_time: Any) -> None:
        """Fold a window's completed trips into the cumulative per-truck / per-haulier accumulators.

        Trips are processed in completion order so dual-cycle chaining (next pickup == previous
        drop-off) is detected across windows via each truck's stored last drop-off.
        """
        if self._run_start_time is None:
            self._run_start_time = self.metric_window_start
        from apps.container_logistics.haul_trip_duration import modeled_haul_duration_seconds

        for trip in sorted(trips, key=self._trip_sort_key):
            truck_id = trip.get("truck")
            if not truck_id:
                continue
            truck_id = str(truck_id)
            haulier_id, haulier_name = self._haulier_of_trip(trip)
            meta = trip.get("meta") or {}

            empty_km = self._empty_km_of(trip) or 0.0
            loaded_km = self._loaded_km_of(trip) or 0.0
            active_seconds = modeled_haul_duration_seconds(trip)

            t_acc = self._truck_acc.get(truck_id)
            if t_acc is None:
                t_acc = self._new_truck_acc(truck_id, haulier_id, haulier_name)
                self._truck_acc[truck_id] = t_acc
            h_acc = self._haulier_acc.get(haulier_id)
            if h_acc is None:
                h_acc = self._new_haulier_acc(haulier_id, haulier_name)
                self._haulier_acc[haulier_id] = h_acc

            # Dual-cycle: compare this pickup to the truck's previous drop-off.
            pickup_fac = meta.get("pickup_facility_resource_id")
            dropoff_fac = meta.get("dropoff_facility_resource_id")
            prev_fac = t_acc["last_dropoff_facility_id"]
            prev_loc = t_acc["last_dropoff_loc"]
            if prev_fac is not None or prev_loc is not None:
                t_acc["chain_opportunities"] += 1
                h_acc["chain_opportunities"] += 1
                if self._is_dual_cycle(prev_fac, prev_loc, pickup_fac, trip.get("pickup_loc")):
                    t_acc["dual_cycle_count"] += 1
                    h_acc["dual_cycle_count"] += 1
            t_acc["last_dropoff_facility_id"] = dropoff_fac
            t_acc["last_dropoff_loc"] = trip.get("dropoff_loc")

            for acc in (t_acc, h_acc):
                acc["empty_km"] += empty_km
                acc["loaded_km"] += loaded_km
                acc["trip_count"] += 1
                acc["active_seconds"] += active_seconds
            h_acc["trucks"].add(truck_id)

            # Collaboration gains (haulier job sharing): the trip's km already
            # accrue to the CARRIER (this trip's truck haulier) above; here we
            # credit both sides of the share for the Companies/gains views.
            collab = meta.get("collaboration") or {}
            if collab.get("shared"):
                h_acc["jobs_carried_for_partners"] += 1
                owner_id = collab.get("owner_haulier_id")
                if owner_id:
                    owner_id = str(owner_id)
                    o_acc = self._haulier_acc.get(owner_id)
                    if o_acc is None:
                        o_acc = self._new_haulier_acc(owner_id, owner_id)
                        self._haulier_acc[owner_id] = o_acc
                    o_acc["jobs_shared_out"] += 1
                    benefit = collab.get("benefit_km")
                    if benefit is not None:
                        try:
                            o_acc["benefit_km_received"] += float(benefit)
                        except (TypeError, ValueError):
                            pass
                    else:
                        # Owner had no free feasible truck: served only thanks
                        # to the partner ("unserveable otherwise").
                        o_acc["unserveable_shares"] += 1
                self._shared_trip_count += 1
                try:
                    self._total_benefit_km += float(collab.get("benefit_km") or 0.0)
                except (TypeError, ValueError):
                    pass

            self._fold_loaded_lane(
                pickup_fac,
                dropoff_fac,
                trip.get("pickup_loc"),
                trip.get("dropoff_loc"),
                haulier_id,
                haulier_name,
                loaded_km,
            )

    def _fold_loaded_lane(
        self,
        from_fac: Any,
        to_fac: Any,
        from_loc: Any,
        to_loc: Any,
        haulier_id: str,
        haulier_name: str,
        loaded_km: float,
    ) -> None:
        """Accumulate one loaded leg into its directed facility lane (from_fac → to_fac)."""
        if from_fac is None or to_fac is None or str(from_fac) == str(to_fac):
            return
        key = (str(from_fac), str(to_fac))
        lane = self._lane_acc.get(key)
        if lane is None:
            lane = {
                "from_fac": str(from_fac),
                "to_fac": str(to_fac),
                "from_loc": from_loc,
                "to_loc": to_loc,
                "loaded_trips": 0,
                "loaded_km": 0.0,
                "by_haulier": {},
                "haulier_names": {},
            }
            self._lane_acc[key] = lane
        lane["loaded_trips"] += 1
        lane["loaded_km"] += loaded_km or 0.0
        lane["by_haulier"][haulier_id] = lane["by_haulier"].get(haulier_id, 0) + 1
        lane["haulier_names"][haulier_id] = haulier_name

    @staticmethod
    def _is_dual_cycle(prev_fac, prev_loc, pickup_fac, pickup_loc, radius_km: float = 0.5) -> bool:
        """True if the next pickup is at (near) the previous drop-off.

        Exact match on facility resource id when both are present; otherwise a small haversine
        radius fallback when coordinates are available.
        """
        if prev_fac is not None and pickup_fac is not None:
            return str(prev_fac) == str(pickup_fac)
        km = AnalyticsManager._haversine_km(prev_loc, pickup_loc)
        return km is not None and km <= radius_km

    def _elapsed_days(self, end_time: Any) -> float:
        """Sim days elapsed since the run start (floored at 1h so early rates aren't absurd)."""
        from apps.utils import str_to_time

        def _to_dt(v):
            if isinstance(v, datetime):
                dt = v
            else:
                dt = str_to_time(v)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

        try:
            start = _to_dt(self._run_start_time)
            end = _to_dt(end_time)
            seconds = (end - start).total_seconds()
            return max(seconds / 86400.0, 1.0 / 24.0)
        except Exception:
            return 1.0

    def _fleet_size(self) -> int:
        from apps.container_logistics.scenario.scenario_config import truck_settings

        return max(1, int(truck_settings.get("num_trucks", 1)))

    def empty_distance_ratio(self) -> float:
        """Fleet-wide empty km ÷ total km over all completed trips so far (the deadhead ratio)."""
        total_empty = sum(a["empty_km"] for a in self._truck_acc.values())
        total = total_empty + sum(a["loaded_km"] for a in self._truck_acc.values())
        return total_empty / total if total > 0 else 0.0

    def orders_per_truck_day(self, end_time: Any) -> float:
        """Cumulative completed hauls ÷ fleet size ÷ elapsed sim-days."""
        total_trips = sum(a["trip_count"] for a in self._truck_acc.values())
        return total_trips / self._fleet_size() / self._elapsed_days(end_time)

    def avg_truck_active_hours(self) -> float:
        """Mean active (driving + service) hours per truck across the whole fleet."""
        total_seconds = sum(a["active_seconds"] for a in self._truck_acc.values())
        return total_seconds / self._fleet_size() / 3600.0

    def dual_cycle_rate(self) -> float:
        """Fraction of chained trip-pairs whose next pickup is at the previous drop-off."""
        opportunities = sum(a["chain_opportunities"] for a in self._truck_acc.values())
        matches = sum(a["dual_cycle_count"] for a in self._truck_acc.values())
        return matches / opportunities if opportunities > 0 else 0.0

    def shared_trip_rate(self) -> float:
        """Fraction of completed hauls carried by a partner haulier (job sharing)."""
        total_trips = sum(a["trip_count"] for a in self._truck_acc.values())
        return self._shared_trip_count / total_trips if total_trips > 0 else 0.0

    def total_benefit_km(self) -> float:
        """Cumulative measured deadhead saved by shared jobs (vs owners' best own options)."""
        return round(self._total_benefit_km, 3)

    @staticmethod
    def _entity_row(acc: Dict[str, Any], elapsed_days: float) -> Dict[str, Any]:
        empty = acc["empty_km"]
        loaded = acc["loaded_km"]
        total = empty + loaded
        trips = acc["trip_count"]
        opp = acc["chain_opportunities"]
        row = {
            "id": acc["id"],
            "haulier_id": acc.get("haulier_id"),
            "haulier_name": acc.get("haulier_name"),
            "num_orders_completed": trips,
            "empty_km": round(empty, 3),
            "loaded_km": round(loaded, 3),
            "total_km": round(total, 3),
            "empty_ratio": round(empty / total, 4) if total > 0 else 0.0,
            "active_hours": round(acc["active_seconds"] / 3600.0, 3),
            "orders_per_day": round(trips / elapsed_days, 3) if elapsed_days > 0 else 0.0,
            "dual_cycle_count": acc["dual_cycle_count"],
            "chain_opportunities": opp,
            "dual_cycle_rate": round(acc["dual_cycle_count"] / opp, 4) if opp > 0 else 0.0,
        }
        if "trucks" in acc:
            row["num_trucks"] = len(acc["trucks"])
        if "jobs_shared_out" in acc:
            row["jobs_shared_out"] = acc["jobs_shared_out"]
            row["jobs_carried_for_partners"] = acc["jobs_carried_for_partners"]
            row["benefit_km_received"] = round(acc["benefit_km_received"], 3)
            row["unserveable_shares"] = acc["unserveable_shares"]
        return row

    def build_breakdown(self, scope: str, end_time: Any) -> List[Dict[str, Any]]:
        """Per-entity rows for ``scope`` (``"truck"`` or ``"haulier"``) from the accumulators."""
        elapsed_days = self._elapsed_days(end_time)
        acc_map = self._truck_acc if scope == "truck" else self._haulier_acc
        return [self._entity_row(acc, elapsed_days) for acc in acc_map.values()]

    def build_planner_breakdown(self, end_time: Any) -> List[Dict[str, Any]]:
        """One row per multi-member planner group (connected component of the active
        cooperation structure), aggregating its member hauliers' accumulators — the
        per-collaboration measurement unit (plan §8.5). Empty when no structure."""
        rows: List[Dict[str, Any]] = []
        elapsed_days = self._elapsed_days(end_time)
        for members in self._coop_components:
            agg = self._new_haulier_acc(
                "planner:" + "+".join(sorted(members)),
                " + ".join(sorted(members)),
            )
            agg["trucks"] = set()
            found = False
            for hid in members:
                acc = self._haulier_acc.get(hid)
                if acc is None:
                    continue
                found = True
                for key in ("empty_km", "loaded_km", "trip_count", "active_seconds",
                            "dual_cycle_count", "chain_opportunities", "jobs_shared_out",
                            "jobs_carried_for_partners", "benefit_km_received",
                            "unserveable_shares"):
                    agg[key] += acc.get(key, 0)
                agg["trucks"].update(acc.get("trucks") or ())
            if not found:
                continue
            row = self._entity_row(agg, elapsed_days)
            row["members"] = sorted(members)
            row["structure_id"] = self._coop_structure_id
            rows.append(row)
        return rows

    @staticmethod
    def _loc_coords(loc: Any) -> Optional[List[float]]:
        if isinstance(loc, dict):
            c = loc.get("coordinates")
            return list(c) if isinstance(c, (list, tuple)) and len(c) >= 2 else None
        if isinstance(loc, (list, tuple)) and len(loc) >= 2:
            return [loc[0], loc[1]]
        return None

    def build_lane_breakdown(self) -> List[Dict[str, Any]]:
        """Undirected backhaul-match rows from the directed loaded-lane accumulator.

        Emits one row per facility corridor {A,B} that carries loaded traffic in *both*
        directions — the textbook backhaul: trucks loaded A→B deadhead back B→A while another
        company's trucks loaded B→A deadhead back A→B, so the two flows could be paired.
        ``backhaul_strength`` = min(ab, ba) pairable round-trips; ``cross_haulier`` flags the
        collaboration win (opposite directions dominated by different hauliers).
        """
        def _top(lane: Optional[Dict[str, Any]]):
            by = (lane or {}).get("by_haulier") or {}
            if not by:
                return (None, None)
            hid = max(by, key=by.get)
            return (hid, lane["haulier_names"].get(hid))

        seen: set = set()
        rows: List[Dict[str, Any]] = []
        for key, fwd in self._lane_acc.items():
            a, b = key
            if key in seen:
                continue
            rev = self._lane_acc.get((b, a))
            seen.add(key)
            if rev is None:
                continue  # one-directional corridor: not a backhaul opportunity
            seen.add((b, a))
            ab, ba = fwd["loaded_trips"], rev["loaded_trips"]
            if ab == 0 or ba == 0:
                continue
            ab_h, ab_hn = _top(fwd)
            ba_h, ba_hn = _top(rev)
            rows.append({
                "id": f"{a}|{b}",
                "a_loc": self._loc_coords(fwd.get("from_loc")),
                "b_loc": self._loc_coords(fwd.get("to_loc")),
                "ab_trips": ab,
                "ba_trips": ba,
                "ab_top_haulier": ab_h,
                "ab_top_haulier_name": ab_hn,
                "ba_top_haulier": ba_h,
                "ba_top_haulier_name": ba_hn,
                "backhaul_strength": min(ab, ba),
                "cross_haulier": ab_h is not None and ba_h is not None and ab_h != ba_h,
                "loaded_km": round(fwd["loaded_km"] + rev["loaded_km"], 2),
            })
        # Strongest pairable corridors first; cap to keep the arc layer light.
        rows.sort(key=lambda r: (r["backhaul_strength"], r["ab_trips"] + r["ba_trips"]), reverse=True)
        return rows[:200]

    def save_breakdowns(self, sim_clock: Any, *, final: bool = False) -> None:
        """Persist truck + haulier + lane breakdown snapshots from the current accumulators."""
        from apps.utils import time_to_str
        from .kpi_breakdown_persist import persist_kpi_breakdown

        end_time = self.metric_window_end or self._run_start_time or sim_clock
        clock_str = time_to_str(sim_clock) if isinstance(sim_clock, datetime) else sim_clock
        # Seed all configured hauliers (zero rows) so the Companies tab lists every company
        # from the first snapshot, rather than only those that have completed a trip yet.
        self._seed_haulier_roster()
        for scope in ("truck", "haulier"):
            rows = self.build_breakdown(scope, end_time)
            persist_kpi_breakdown(
                self.user,
                self.run_id,
                scope=scope,
                sim_clock=clock_str,
                rows=rows,
                final=final,
            )
        persist_kpi_breakdown(
            self.user,
            self.run_id,
            scope="lane",
            sim_clock=clock_str,
            rows=self.build_lane_breakdown(),
            final=final,
        )
        # Planner groups (cooperation structure components) — only when a structure
        # with real edges is active; absent structure => no rows, no scope doc.
        planner_rows = self.build_planner_breakdown(end_time)
        if planner_rows:
            persist_kpi_breakdown(
                self.user,
                self.run_id,
                scope="planner",
                sim_clock=clock_str,
                rows=planner_rows,
                final=final,
            )

    #: Sim-time spacing of the intermediate breakdown checkpoints emitted by the finalize
    #: recompute (~28 points for a 7-day run) — drives the per-haulier "over time" view.
    CHECKPOINT_INTERVAL_HOURS = 6

    def recompute_breakdowns_full(self, sim_clock: Any) -> None:
        """Authoritative end-of-run recompute: full scan of completed trips → a per-haulier
        time series of cumulative breakdown snapshots, ending with the definitive ``final=True``.

        Runs from the run-finalize path (a fresh manager, not the analytics agent's in-memory
        accumulators) so it is independent of the live agent's tick cadence: replaying the sorted
        trips and persisting at fixed sim-time checkpoints yields a deterministic time series even
        when no live snapshots were captured.
        """
        from datetime import timedelta

        from apps.utils import str_to_time

        self._truck_acc = {}
        self._haulier_acc = {}
        self._lane_acc = {}
        self._run_start_time = None
        # Collaboration scalars accumulate in accumulate_completed_trips too — reset
        # them with the accs or a second invocation on the same manager double-counts.
        self._shared_trip_count = 0
        self._total_benefit_km = 0.0
        trips = self._paged_where(
            self._haul_trip_url(),
            {
                "$and": [
                    {"run_id": self.run_id},
                    {"state": HaulTripStateMachine.completed.name},
                ]
            },
            projection=self._COMPLETED_TRIP_PROJECTION,
        )
        if not trips:
            self.set_metric_window(None, sim_clock)
            self.save_breakdowns(sim_clock, final=True)
            return

        ordered = sorted(trips, key=self._trip_sort_key)
        self._run_start_time = ordered[0].get("sim_clock")
        end_time = ordered[-1].get("sim_clock")

        def _to_dt(value: Any) -> datetime:
            dt = value if isinstance(value, datetime) else str_to_time(value)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

        def _flush(window_end: Any, *, final: bool) -> None:
            # Cumulative breakdown as of ``window_end``; window end drives elapsed-day rates.
            self.set_metric_window(self._run_start_time, window_end)
            self.save_breakdowns(window_end, final=final)

        step = timedelta(hours=self.CHECKPOINT_INTERVAL_HOURS)
        start_dt = _to_dt(self._run_start_time)
        end_dt = _to_dt(end_time)
        next_boundary = start_dt + step
        chunk: List[Dict[str, Any]] = []
        for trip in ordered:
            t_dt = _to_dt(trip.get("sim_clock"))
            while t_dt > next_boundary and next_boundary < end_dt:
                if chunk:
                    self.accumulate_completed_trips(chunk, next_boundary)
                    chunk = []
                _flush(next_boundary, final=False)
                next_boundary += step
            chunk.append(trip)

        # Final authoritative snapshot: fold any remaining trips, persist final=True.
        if chunk:
            self.accumulate_completed_trips(chunk, end_time)
        _flush(end_time or sim_clock, final=True)

    def get_active_haul_trips(self) -> List[Dict[str, Any]]:
        """Non-terminal haul trips (for live map geometry)."""
        try:
            return self._paged_where(
                self._haul_trip_url(),
                {
                    "$and": [
                        {"run_id": self.run_id},
                        {"state": {"$nin": list(self._final_haul_states)}},
                    ]
                },
            )
        except Exception as e:
            logging.error("get_active_haul_trips failed: %s", e)
            return []

    @staticmethod
    def active_haul_truck_count_from_rows(rows: List[Dict[str, Any]]) -> int:
        trucks = {str(r["truck"]) for r in rows if r.get("truck")}
        return len(trucks)

    def save_kpi(self, sim_clock, kpi_collection):
        save_kpi_batch(
            self.run_id,
            sim_clock,
            kpi_collection,
            self._kpi_catalog,
            ecosystem_label="container_logistics",
            mongo_post=lambda rows: self._post(self._kpi_url(), rows),
        )
