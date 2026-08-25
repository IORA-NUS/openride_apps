"""
Emit haul trip_route / trip_end messages to Kafka trip_geo_stream (container logistics).

See: docs/container_logistics_map_streaming_spec.md
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import polyline

from apps.container_logistics.analytics.streaming_spec import ECOSYSTEM, HAUL_ROUTE_GEOMETRY_STATES
from apps.container_logistics.statemachine import HaulTripStateMachine
from apps.loc_service.osrm_client import OSRMClient
from apps.ridehail.analytics.trip_geo_publisher import sim_clock_gmt_to_iso_z
from apps.utils.kafka_utils import flush_trip_geo_producer, push_trip_geo_to_topic

_MIN_DECODED_POINTS = 3

# Cache OSRM polylines for endpoint pairs so repeated trip_geo updates per step
# (or many haul trips between the same facilities) hit OSRM at most once each.
_OSRM_CACHE_MAX = 4096

# Dedup: keyed by truck_agent_id, value is last emitted "lon:lat:state" signature.
_TRUCK_LOC_LAST: Dict[str, str] = {}


def publish_truck_location(
    run_id: str,
    sim_clock_gmt: str,
    truck_agent_id: str,
    lon: float,
    lat: float,
    haul_state: Optional[str],
    haulier_id: Optional[str] = None,
    haulier_name: Optional[str] = None,
) -> bool:
    """Push a truck_loc message to trip_geo_stream. Returns True if emitted."""
    sig = f"{round(lon, 5)},{round(lat, 5)},{haul_state or ''},{haulier_id or ''}"
    if _TRUCK_LOC_LAST.get(truck_agent_id) == sig:
        return False
    _TRUCK_LOC_LAST[truck_agent_id] = sig
    msg = {
        "type": "truck_loc",
        "run_id": run_id,
        "sim_clock": sim_clock_gmt_to_iso_z(sim_clock_gmt),
        "truck_agent_id": truck_agent_id,
        "lon": lon,
        "lat": lat,
        "haul_state": haul_state or "idle",
    }
    if haulier_id:
        msg["haulier_id"] = str(haulier_id)
    if haulier_name:
        msg["haulier_name"] = str(haulier_name)
    push_trip_geo_to_topic(run_id, msg)
    return True
_OSRM_CACHE: "OrderedDict[Tuple[Tuple[float, float], Tuple[float, float]], Optional[str]]" = (
    OrderedDict()
)
# Wall-clock stamp of the last failed lookup per key — see `_osrm_polyline`.
_OSRM_FAILED_AT: Dict[Tuple[Tuple[float, float], Tuple[float, float]], float] = {}
_OSRM_FAILURE_TTL_SECONDS = 60.0

_STATE_TO_LEG: Dict[str, str] = {
    HaulTripStateMachine.repositioning_to_pickup.name: "repositioning_to_pickup",
    HaulTripStateMachine.loaded_in_transit.name: "loaded_to_dropoff",
}

_PREVIEW_GEOMETRY_STATES: frozenset[str] = frozenset(
    {
        HaulTripStateMachine.assigned.name,
        HaulTripStateMachine.queued_for_pickup.name,
        HaulTripStateMachine.at_pickup_gate.name,
        HaulTripStateMachine.queued_for_dropoff.name,
        HaulTripStateMachine.at_dropoff_gate.name,
        HaulTripStateMachine.completed.name,
    }
)


def _oid_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _polyline_from_coords(coords: Any) -> Optional[str]:
    if not coords or not isinstance(coords, list) or len(coords) < _MIN_DECODED_POINTS:
        return None
    try:
        latlng = []
        for c in coords:
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


def _geometry_from_route(route: Dict[str, Any]) -> Optional[str]:
    geom = route.get("geometry")
    if geom and isinstance(geom, str):
        return geom
    coords = route.get("coordinates") or route.get("coords")
    return _polyline_from_coords(coords)


def _geo_point(loc: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(loc, dict):
        return None
    coords = loc.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        return float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None


def _legs_for_trip(trip: Dict[str, Any]) -> List[str]:
    state = trip.get("state")
    if state in HAUL_ROUTE_GEOMETRY_STATES:
        leg = _STATE_TO_LEG.get(state)
        legs = [leg] if leg else []
        # While the truck is still driving empty toward the pickup, also publish a
        # preview of the loaded (pickup -> dropoff) leg. Both endpoints are known the
        # moment the trip is assigned, so the route inspector can show the loaded leg
        # straight away instead of "Not available yet" until the truck actually loads.
        # The endpoints don't move during repositioning, so dedup + the OSRM cache make
        # the repeated per-tick emits essentially free after the first one.
        if (
            state == HaulTripStateMachine.repositioning_to_pickup.name
            and _geo_point(trip.get("pickup_loc"))
            and _geo_point(trip.get("dropoff_loc"))
        ):
            legs.append("loaded_to_dropoff")
        return legs
    if state in _PREVIEW_GEOMETRY_STATES:
        legs: List[str] = []
        if _geo_point(trip.get("current_loc")) and _geo_point(trip.get("pickup_loc")):
            legs.append("repositioning_to_pickup")
        if _geo_point(trip.get("pickup_loc")) and _geo_point(trip.get("dropoff_loc")):
            legs.append("loaded_to_dropoff")
        return legs
    return []


def _straight_line_polyline(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    *,
    n_points: int = 10,
) -> Optional[str]:
    lng0, lat0 = p0
    lng1, lat1 = p1
    if (lng0, lat0) == (lng1, lat1):
        return None
    n = max(_MIN_DECODED_POINTS, n_points)
    latlng = [
        (lat0 + (lat1 - lat0) * i / (n - 1), lng0 + (lng1 - lng0) * i / (n - 1))
        for i in range(n)
    ]
    return polyline.encode(latlng, precision=5)


def _osrm_cache_key(
    p0: Tuple[float, float], p1: Tuple[float, float]
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    # ~1m rounding is plenty to fold near-identical endpoints into the same cache slot.
    return (
        (round(p0[0], 5), round(p0[1], 5)),
        (round(p1[0], 5), round(p1[1], 5)),
    )


def _osrm_polyline(p0: Tuple[float, float], p1: Tuple[float, float]) -> Optional[str]:
    """Request a road-following polyline from OSRM between two (lng, lat) points.

    Successes are cached forever (geometry between two fixed points never changes).
    FAILURES are cached only briefly: this cache is module-level inside a long-lived
    Celery worker, so permanently memoising ``None`` let a single OSRM blip poison that
    OD pair for the worker's entire lifetime — across every later run — silently
    downgrading those legs to straight lines. The short negative TTL still stops a
    sustained outage from hammering OSRM (and from paying the request timeout) once per
    leg per tick.
    """
    key = _osrm_cache_key(p0, p1)
    if key in _OSRM_CACHE:
        _OSRM_CACHE.move_to_end(key)
        cached = _OSRM_CACHE[key]
        if cached is not None:
            return cached
        failed_at = _OSRM_FAILED_AT.get(key)
        if failed_at is not None and (time.monotonic() - failed_at) < _OSRM_FAILURE_TTL_SECONDS:
            return None
        # TTL expired — fall through and retry.
        _OSRM_FAILED_AT.pop(key, None)

    start = {"type": "Point", "coordinates": [p0[0], p0[1]]}
    end = {"type": "Point", "coordinates": [p1[0], p1[1]]}
    geom: Optional[str] = None
    try:
        route = OSRMClient.get_route(start, end)
        raw = route.get("geometry") if isinstance(route, dict) else None
        if isinstance(raw, str) and raw:
            geom = raw
    except Exception as exc:  # network / OSRM down — log once and fall back later.
        logging.debug("trip_geo: OSRM lookup failed for %s -> %s: %s", p0, p1, exc)
        geom = None

    _OSRM_CACHE[key] = geom
    if geom is None:
        _OSRM_FAILED_AT[key] = time.monotonic()
    else:
        _OSRM_FAILED_AT.pop(key, None)
    if len(_OSRM_CACHE) > _OSRM_CACHE_MAX:
        evicted, _ = _OSRM_CACHE.popitem(last=False)
        _OSRM_FAILED_AT.pop(evicted, None)
    return geom


def _prefetch_osrm_polylines(haul_trips: List[Dict[str, Any]]) -> None:
    """Resolve all cache-missing OSRM legs for this tick concurrently.

    Only legs whose planned route carries no geometry need OSRM (the same
    condition _resolve_leg_geometry uses), and only cache misses cost anything.
    Uses an eventlet GreenPool when available (the analytics agent runs in the
    monkey-patched Celery eventlet workers, so the HTTP calls yield); falls back
    to the serial in-loop lookups otherwise.
    """
    pending = []
    seen = set()
    for trip in haul_trips:
        planned = (trip.get("routes") or {}).get("planned") or {}
        for leg in _legs_for_trip(trip):
            route = planned.get(leg) if isinstance(planned, dict) else None
            if isinstance(route, dict) and _geometry_from_route(route):
                continue
            p0, p1 = _endpoints_for_leg(trip, leg)
            if not p0 or not p1:
                continue
            key = _osrm_cache_key(p0, p1)
            if key in _OSRM_CACHE or key in seen:
                continue
            seen.add(key)
            pending.append((p0, p1))
    if len(pending) < 2:
        return
    try:
        import eventlet
        pool = eventlet.GreenPool(size=16)
    except Exception:
        return
    try:
        for _ in pool.imap(lambda pair: _osrm_polyline(pair[0], pair[1]), pending):
            pass
    except Exception:
        logging.exception("trip_geo: OSRM prefetch failed; falling back to serial lookups")


def _endpoints_for_leg(
    trip: Dict[str, Any], leg: str
) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    if leg == "repositioning_to_pickup":
        return _geo_point(trip.get("current_loc")), _geo_point(trip.get("pickup_loc"))
    if leg == "loaded_to_dropoff":
        return _geo_point(trip.get("pickup_loc")), _geo_point(trip.get("dropoff_loc"))
    return None, None


def _fallback_geometry_for_leg(trip: Dict[str, Any], leg: str) -> Optional[str]:
    """Build route geometry from haul trip endpoints when planned routes lack OSRM data.

    Tries OSRM first so the UI gets a road-following polyline. If OSRM is
    unreachable or returns nothing, falls back to a straight-line polyline so
    the leg still renders something.
    """
    p0, p1 = _endpoints_for_leg(trip, leg)
    if not p0 or not p1:
        return None
    osrm_geom = _osrm_polyline(p0, p1)
    if osrm_geom:
        return osrm_geom
    return _straight_line_polyline(p0, p1)


def _resolve_leg_geometry(
    trip: Dict[str, Any],
    leg: str,
    route: Optional[Dict[str, Any]],
    authoritative_routes: bool = False,
) -> Optional[str]:
    if route:
        geom = _geometry_from_route(route)
        if geom:
            return geom
        # The route exists but carries no geometry because OSRM failed AT ASSIGNMENT
        # (marker written by `truck/app.py::_route_or_loud_failure`). Re-querying OSRM here
        # would produce a DIFFERENT answer from a different process, at a different time,
        # from `current_loc` rather than the assignment origin — so the live map would show
        # a road route for a leg the truck did not drive along, whose stored route says
        # "unavailable" and whose KPI used haversine. One authoritative route per leg means
        # this must stay empty.
        if route.get("geometry_source") == "unavailable":
            return None
    if authoritative_routes:
        # Same rule for the OTHER way a leg ends up with no stored route: when
        # `_plan_routes_for_assignment` bails early (missing start/pickup/dropoff point) it
        # returns Nones and writes no marker at all. On a run where the stored route is the
        # route — driven by the truck and measured by the KPI — re-deriving one here from
        # `current_loc`, in a different process at a different time, and straight-lining it
        # when OSRM is down, would put a path on the map that the simulation never used.
        return None
    return _fallback_geometry_for_leg(trip, leg)


def _truck_agent_id_from_trip(trip: Dict[str, Any]) -> Optional[str]:
    meta = trip.get("meta") or {}
    prof = meta.get("truck_profile") or {}
    if isinstance(prof.get("agent_id"), str) and prof["agent_id"]:
        return prof["agent_id"]
    email = prof.get("email")
    if isinstance(email, str) and "@" in email:
        return email.split("@", 1)[0]
    return None


class ContainerTripGeoPublisher:
    def __init__(self, run_id: str, authoritative_routes: bool = False):
        self.run_id = run_id
        # True when routes are planned+stored at assignment (USE_OSRM_AT_ASSIGNMENT). The
        # trip document is then the single source of truth for a leg's path, and this
        # publisher must never synthesize one it lacks. See `_resolve_leg_geometry`.
        self.authoritative_routes = bool(authoritative_routes)
        self._last_emit: Dict[str, str] = {}
        self._last_active_haul_trips: Set[str] = set()
        self._publish_count = 0

    def publish_from_haul_trips(self, haul_trips: List[Dict[str, Any]], sim_clock_gmt: str) -> None:
        sim_iso = sim_clock_gmt_to_iso_z(sim_clock_gmt)
        active: Set[str] = set()
        emitted_routes = 0
        emitted_ends = 0

        # Warm the OSRM cache for every leg this tick will resolve, concurrently.
        # Done serially inside the emit loop these lookups (~2 per fresh trip,
        # ~10ms each) made the analytics tick the slowest service step of the
        # run (2-4s every 48 steps at 1000-truck scale).
        _prefetch_osrm_polylines(haul_trips)

        for trip in haul_trips:
            haul_id = _oid_str(trip.get("_id"))
            if not haul_id:
                continue
            active.add(haul_id)
            emitted_routes += self._maybe_emit_trip_routes(trip, sim_iso, sim_clock_gmt, haul_id)

        emitted_ends = self._emit_trip_ends_for_removed(active, sim_iso)
        self._last_active_haul_trips = active
        flush_trip_geo_producer(timeout=0.5)

        self._publish_count += 1
        # Surface publishing cadence so we can tell from the sim log whether the
        # analytics tick actually pushed anything to trip_geo_stream.
        logging.info(
            "trip_geo: sim_clock=%s active_haul_trips=%d emitted_routes=%d emitted_ends=%d publish_tick=%d",
            sim_clock_gmt,
            len(active),
            emitted_routes,
            emitted_ends,
            self._publish_count,
        )

    def _maybe_emit_trip_routes(
        self,
        trip: Dict[str, Any],
        sim_clock_iso: str,
        sim_clock_gmt: str,
        haul_trip_id: str,
    ) -> int:
        planned = (trip.get("routes") or {}).get("planned") or {}
        emitted = 0
        for leg in _legs_for_trip(trip):
            route = planned.get(leg) if isinstance(planned, dict) else None
            if route is not None and not isinstance(route, dict):
                route = None
            if self._emit_trip_route_for_leg(
                trip,
                sim_clock_iso,
                sim_clock_gmt,
                haul_trip_id,
                leg,
                route if isinstance(route, dict) else None,
            ):
                emitted += 1
        return emitted

    def _emit_trip_route_for_leg(
        self,
        trip: Dict[str, Any],
        sim_clock_iso: str,
        sim_clock_gmt: str,
        haul_trip_id: str,
        leg: str,
        route: Optional[Dict[str, Any]],
    ) -> bool:
        ui_state = trip.get("state") or "unknown"

        geom = _resolve_leg_geometry(trip, leg, route, self.authoritative_routes)
        if not geom:
            logging.debug(
                "trip_geo: no geometry for haul_trip_id=%s leg=%s state=%s",
                haul_trip_id,
                leg,
                trip.get("state"),
            )
            return False

        try:
            n_pts = len(polyline.decode(geom.rstrip()))
        except Exception as e:
            logging.warning("trip_geo: polyline decode failed for %s: %s", haul_trip_id, e)
            return False
        if n_pts < _MIN_DECODED_POINTS:
            logging.debug(
                "trip_geo: skip geometry with too few points (%s) for %s",
                n_pts,
                haul_trip_id,
            )
            return False

        # Dedup must include `state`: when a haul trip transitions across queued /
        # at-gate / loaded-in-transit states the planned-route geometry often
        # stays identical (truck parked at the same facility), but the UI still
        # needs the fresh state to recolor the truck and advance lifecycle.
        # Geometry-only dedup masked all state changes after the first tick.
        stream_key = f"{haul_trip_id}:{leg}"
        sig = f"{leg}:{ui_state}:{_geometry_signature(geom)}"
        if self._last_emit.get(stream_key) == sig:
            return False
        self._last_emit[stream_key] = sig

        payload: Dict[str, Any] = {
            "type": "trip_route",
            "ecosystem": ECOSYSTEM,
            "run_id": self.run_id,
            "sim_clock": sim_clock_iso,
            "haul_trip_id": haul_trip_id,
            "truck_id": _oid_str(trip.get("truck")),
            "geometry_encoding": "polyline",
            "polyline_precision": 5,
            "geometry": geom,
            "state": ui_state or trip.get("state") or "unknown",
            "leg": leg,
            "trip_end_sim_clock": None,
        }

        order_id = trip.get("order")
        if order_id:
            payload["order_id"] = _oid_str(order_id)

        agent_id = _truck_agent_id_from_trip(trip)
        if agent_id:
            payload["truck_agent_id"] = agent_id

        # Haulier identity so the live map can color/filter trips by owning company.
        truck_prof = (trip.get("meta") or {}).get("truck_profile") or {}
        haulier_id = truck_prof.get("haulier_id")
        if haulier_id:
            payload["haulier_id"] = str(haulier_id)
        haulier_name = truck_prof.get("haulier_name")
        if haulier_name:
            payload["haulier_name"] = str(haulier_name)

        # Collaboration (haulier job sharing): a cross-haulier haul carries its tag on
        # trip meta — surface it so the live map can accent shared hauls (owner-color
        # route under carrier-color truck) and the feed/HUD can attribute benefit.
        collab = (trip.get("meta") or {}).get("collaboration") or {}
        if collab.get("shared"):
            payload["shared"] = True
            if collab.get("owner_haulier_id"):
                payload["owner_haulier_id"] = str(collab["owner_haulier_id"])
            if collab.get("carrier_haulier_id"):
                payload["carrier_haulier_id"] = str(collab["carrier_haulier_id"])
            if collab.get("benefit_km") is not None:
                try:
                    payload["benefit_km"] = float(collab["benefit_km"])
                except (TypeError, ValueError):
                    pass

        t_start = trip.get("sim_clock") or trip.get("_created")
        if t_start:
            try:
                if isinstance(t_start, str) and "GMT" in t_start:
                    payload["trip_start_sim_clock"] = sim_clock_gmt_to_iso_z(t_start)
                elif isinstance(t_start, datetime):
                    dt = t_start if t_start.tzinfo else t_start.replace(tzinfo=timezone.utc)
                    payload["trip_start_sim_clock"] = (
                        dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    )
                elif isinstance(t_start, str):
                    payload["trip_start_sim_clock"] = (
                        t_start.replace("+00:00", "Z")
                        if "Z" not in t_start and "+" not in t_start[-6:]
                        else t_start
                    )
            except Exception:
                pass

        push_trip_geo_to_topic(self.run_id, payload)
        return True

    def _emit_trip_ends_for_removed(self, active_haul_trips: Set[str], sim_clock_iso: str) -> int:
        ended = self._last_active_haul_trips - active_haul_trips
        for haul_id in ended:
            for key in list(self._last_emit.keys()):
                if key == haul_id or key.startswith(f"{haul_id}:"):
                    self._last_emit.pop(key, None)
            push_trip_geo_to_topic(
                self.run_id,
                {
                    "type": "trip_end",
                    "ecosystem": ECOSYSTEM,
                    "run_id": self.run_id,
                    "sim_clock": sim_clock_iso,
                    "haul_trip_id": haul_id,
                    "lifecycle_scope": "haul_trip",
                    "note": "Haul trip left active set; simulation may still be running.",
                },
            )
        return len(ended)
