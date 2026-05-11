"""
Emit trip_route / trip_end messages to Kafka trip_geo_stream for the analytics dashboard.

Uses full OSRM geometry from driver trip routes.planned (encoded polyline), keyed by run_id.
Deduplicates identical (leg, geometry) emissions; emits trip_end when a passenger trip leaves the active set.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import polyline

from apps.ridehail.statemachine import RidehailDriverTripStateMachine
from apps.utils import str_to_time
from apps.utils.kafka_utils import flush_trip_geo_producer, push_trip_geo_to_topic

# Decode and require more than two points so the UI never draws a single segment "straight line" artifact only.
_MIN_DECODED_POINTS = 3


def sim_clock_gmt_to_iso_z(sim_clock_gmt: str) -> str:
    """Match dashboard expectation: ISO-8601 UTC with Z (simulation time, not wall clock)."""
    dt = str_to_time(sim_clock_gmt)
    dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _oid_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _planned_leg_for_state(
    trip: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """
    Returns (route_dict, leg_name, ui_state) for the active leg, or (None, None, None).
    Uses routes.planned for moving_* and waiting_* (same geometry as the current leg).
    """
    state = trip.get("state")
    planned = (trip.get("routes") or {}).get("planned") or {}
    sm = RidehailDriverTripStateMachine

    if state in (sm.driver_moving_to_pickup.name, sm.driver_waiting_to_pickup.name):
        r = planned.get("moving_to_pickup")
        return r, "moving_to_pickup", "moving_to_pickup"
    if state in (sm.driver_moving_to_dropoff.name, sm.driver_waiting_to_dropoff.name):
        r = planned.get("moving_to_dropoff")
        return r, "moving_to_dropoff", "moving_to_dropoff"
    return None, None, None


def _polyline_from_projected_path(projected_path: Any) -> Optional[str]:
    """
    Build an encoded polyline from pinged LineString coords when routes.planned has no geometry string.
    Coords are GeoJSON-style (lon, lat) per point from shapely LineString.coords.
    """
    if not projected_path or not isinstance(projected_path, list):
        return None
    if len(projected_path) < _MIN_DECODED_POINTS:
        return None
    try:
        latlng = []
        for c in projected_path:
            if not isinstance(c, (list, tuple)) or len(c) < 2:
                return None
            lon, lat = float(c[0]), float(c[1])
            latlng.append((lat, lon))
        return polyline.encode(latlng, precision=5)
    except (TypeError, ValueError):
        return None


def _geometry_signature(encoded: str) -> str:
    try:
        pts = polyline.decode(encoded.rstrip())
    except Exception:
        return encoded[:64]
    return f"{len(pts)}:" + encoded[:32]


class TripGeoPublisher:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self._last_emit: Dict[str, str] = {}
        self._last_active_passenger_trips: Set[str] = set()

    def publish_from_driver_trips(self, driver_trips: Dict[str, Any], sim_clock_gmt: str) -> None:
        """
        driver_trips: map driver_id -> trip document (same shape as AnalyticsManager.get_active_driver_trips).
        """
        sim_iso = sim_clock_gmt_to_iso_z(sim_clock_gmt)
        active_pt: Set[str] = set()

        for _driver_key, trip in driver_trips.items():
            if not trip.get("is_occupied"):
                continue
            ptid = trip.get("ridehail_passenger_trip")
            if not ptid:
                continue
            ptx = _oid_str(ptid)
            active_pt.add(ptx)
            self._maybe_emit_trip_route(trip, sim_iso, sim_clock_gmt, ptx)

        self._emit_trip_ends_for_removed(active_pt, sim_iso)
        self._last_active_passenger_trips = active_pt

        flush_trip_geo_producer(timeout=0.5)

    def _maybe_emit_trip_route(
        self,
        trip: Dict[str, Any],
        sim_clock_iso: str,
        sim_clock_gmt: str,
        passenger_trip_id: str,
    ) -> None:
        route, leg, ui_state = _planned_leg_for_state(trip)
        geom: Optional[str] = None
        if route and isinstance(route, dict):
            g = route.get("geometry")
            if g and isinstance(g, str):
                geom = g
        if not geom:
            geom = _polyline_from_projected_path(trip.get("projected_path"))
        if not geom:
            logging.debug(
                "trip_geo: no geometry or projected_path for passenger_trip_id=%s state=%s",
                passenger_trip_id,
                trip.get("state"),
            )
            return
        if leg is None:
            leg = "projected_path"
        if ui_state is None:
            ui_state = trip.get("state") or "unknown"
        try:
            n_pts = len(polyline.decode(geom.rstrip()))
        except Exception as e:
            logging.warning("trip_geo: polyline decode failed for %s: %s", passenger_trip_id, e)
            return
        if n_pts < _MIN_DECODED_POINTS:
            logging.debug(
                "trip_geo: skip geometry with too few points (%s) for %s",
                n_pts,
                passenger_trip_id,
            )
            return

        sig = f"{leg}:{_geometry_signature(geom)}"
        if self._last_emit.get(passenger_trip_id) == sig:
            return
        self._last_emit[passenger_trip_id] = sig

        payload: Dict[str, Any] = {
            "type": "trip_route",
            "run_id": self.run_id,
            "sim_clock": sim_clock_iso,
            "passenger_trip_id": passenger_trip_id,
            "driver_trip_id": _oid_str(trip.get("_id")),
            "driver_id": _oid_str(trip.get("driver")),
            "passenger_id": _oid_str(trip.get("passenger")),
            "geometry_encoding": "polyline",
            "polyline_precision": 5,
            "geometry": geom,
            "state": ui_state,
            "leg": leg,
        }

        t_start = trip.get("_created") or trip.get("sim_clock")
        if t_start:
            try:
                if isinstance(t_start, str) and "GMT" in t_start:
                    payload["trip_start_sim_clock"] = sim_clock_gmt_to_iso_z(t_start)
                elif isinstance(t_start, datetime):
                    dt = t_start if t_start.tzinfo else t_start.replace(tzinfo=timezone.utc)
                    payload["trip_start_sim_clock"] = (
                        dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    )
            except Exception:
                pass
        payload["trip_end_sim_clock"] = None

        push_trip_geo_to_topic(self.run_id, payload)

    def _emit_trip_ends_for_removed(self, active_passenger_trips: Set[str], sim_clock_iso: str) -> None:
        ended = self._last_active_passenger_trips - active_passenger_trips
        for ptid in ended:
            self._last_emit.pop(ptid, None)
            push_trip_geo_to_topic(
                self.run_id,
                {
                    "type": "trip_end",
                    "run_id": self.run_id,
                    "sim_clock": sim_clock_iso,
                    "passenger_trip_id": ptid,
                },
            )
