"""Haul-trip drive-time floors (Phase 2): minimum realistic leg and end-to-end durations."""

import logging
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple

from apps.container_logistics.duration_constants import (
    MAX_LEG_DROPOFF_SECONDS,
    MAX_LEG_PICKUP_SECONDS,
    MIN_HAUL_TRIP_SECONDS,
    MIN_LEG_DROPOFF_SECONDS,
    MIN_LEG_PICKUP_SECONDS,
)

_DEFAULT_GATE_SERVICE_SECONDS = 120


def _profile_bounds(profile: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    tp = profile or {}
    return {
        "min_pickup": int(tp.get("min_estimated_time_to_pickup", MIN_LEG_PICKUP_SECONDS)),
        "max_pickup": int(tp.get("max_estimated_time_to_pickup", MAX_LEG_PICKUP_SECONDS)),
        "min_dropoff": int(tp.get("min_estimated_time_to_dropoff", MIN_LEG_DROPOFF_SECONDS)),
        "max_dropoff": int(tp.get("max_estimated_time_to_dropoff", MAX_LEG_DROPOFF_SECONDS)),
        "min_haul": int(tp.get("min_haul_trip_seconds", MIN_HAUL_TRIP_SECONDS)),
    }


def _coerce_seconds(value: Any, default: float = 0.0) -> float:
    try:
        sec = float(value)
        return sec if sec >= 0 else default
    except (TypeError, ValueError):
        return default


def clamp_leg_eta(
    eta_seconds: Any,
    *,
    min_seconds: int,
    max_seconds: Optional[int] = None,
) -> float:
    eta = _coerce_seconds(eta_seconds, float(min_seconds))
    eta = max(float(min_seconds), eta)
    if max_seconds is not None:
        eta = min(eta, float(max_seconds))
    return eta


def gate_service_seconds(
    order: Optional[Dict[str, Any]],
    *,
    default_pickup: int = _DEFAULT_GATE_SERVICE_SECONDS,
    default_dropoff: int = _DEFAULT_GATE_SERVICE_SECONDS,
) -> Tuple[float, float]:
    o = order or {}
    pickup = _coerce_seconds(o.get("pickup_service_time"), default_pickup)
    dropoff = _coerce_seconds(o.get("dropoff_service_time"), default_dropoff)
    return pickup, dropoff


def apply_haul_trip_duration_floors(
    eta_pickup: Any,
    eta_dropoff: Any,
    *,
    order: Optional[Dict[str, Any]] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float]:
    """
    Clamp leg ETAs and ensure drive + gate service meets the minimum haul duration.
    Slack is added to the dropoff leg when the total is still too short.
    """
    bounds = _profile_bounds(profile)
    pickup_svc, dropoff_svc = gate_service_seconds(order)

    pickup = clamp_leg_eta(
        eta_pickup,
        min_seconds=bounds["min_pickup"],
        max_seconds=bounds["max_pickup"],
    )
    dropoff = clamp_leg_eta(
        eta_dropoff,
        min_seconds=bounds["min_dropoff"],
        max_seconds=bounds["max_dropoff"],
    )

    min_total = float(bounds["min_haul"])
    drive_plus_service = pickup + dropoff + pickup_svc + dropoff_svc
    if drive_plus_service < min_total:
        dropoff += min_total - drive_plus_service

    max_drop = bounds["max_dropoff"]
    if dropoff > max_drop and pickup + max_drop + pickup_svc + dropoff_svc >= min_total:
        dropoff = float(max_drop)
    elif pickup + dropoff + pickup_svc + dropoff_svc < min_total:
        dropoff = min_total - pickup - pickup_svc - dropoff_svc

    return pickup, dropoff


def patch_route_duration(route: Optional[Dict[str, Any]], duration_seconds: float) -> Optional[Dict[str, Any]]:
    """Keep planned route duration aligned with clamped stats for movement interpolation."""
    if not isinstance(route, dict):
        return route
    patched = deepcopy(route)
    patched["duration"] = float(duration_seconds)
    return patched


def modeled_haul_duration_seconds(
    trip: Dict[str, Any],
    *,
    profile: Optional[Dict[str, Any]] = None,
) -> float:
    """Sum of modeled drive legs and gate service for a haul trip document."""
    stats = trip.get("stats") or {}
    pickup_svc, dropoff_svc = gate_service_seconds(
        {
            "pickup_service_time": stats.get("pickup_service_time"),
            "dropoff_service_time": stats.get("dropoff_service_time"),
        }
    )
    return (
        _coerce_seconds(stats.get("estimated_time_to_pickup"))
        + _coerce_seconds(stats.get("estimated_time_to_dropoff"))
        + pickup_svc
        + dropoff_svc
    )


def warn_if_haul_trip_under_minimum(
    trip: Dict[str, Any],
    *,
    completed_at,
    profile: Optional[Dict[str, Any]] = None,
) -> None:
    """Log when a completed haul finished faster than the configured minimum (debug/tuning)."""
    bounds = _profile_bounds(profile)
    min_total = float(bounds["min_haul"])
    stats = trip.get("stats") or {}
    pickup_svc, dropoff_svc = gate_service_seconds(
        {
            "pickup_service_time": stats.get("pickup_service_time"),
            "dropoff_service_time": stats.get("dropoff_service_time"),
        }
    )
    modeled = (
        _coerce_seconds(stats.get("estimated_time_to_pickup"))
        + _coerce_seconds(stats.get("estimated_time_to_dropoff"))
        + pickup_svc
        + dropoff_svc
    )
    if modeled < min_total:
        logging.warning(
            "Haul trip %s modeled duration %.0fs below minimum %.0fs",
            trip.get("_id"),
            modeled,
            min_total,
        )
