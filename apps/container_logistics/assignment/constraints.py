"""
Domain constraints for matching trucks to orders in container logistics.

Policy knobs are driven primarily by ``scenario_config.assignment_settings["profile"]``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set

from apps.container_logistics.scenario.scenario_config import facility_settings


def _as_point(loc: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(loc, dict):
        return None
    coords = loc.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    return {"type": "Point", "coordinates": [lon, lat]}


def _haversine_seconds(start: Dict[str, Any], end: Dict[str, Any], assumed_kmh: float = 40.0) -> Optional[float]:
    """Rough drive-time proxy when OSRM is unavailable (straight-line / assumed speed)."""
    a = _as_point(start)
    b = _as_point(end)
    if a is None or b is None:
        return None
    lon1, lat1 = math.radians(a["coordinates"][0]), math.radians(a["coordinates"][1])
    lon2, lat2 = math.radians(b["coordinates"][0]), math.radians(b["coordinates"][1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    dist_m = 2 * 6371000 * math.asin(min(1.0, math.sqrt(h)))
    if assumed_kmh <= 0:
        return None
    return dist_m / (1000.0 * assumed_kmh / 3600.0)


def _configured_facilities() -> List[Dict[str, Any]]:
    """Mirror ``GenerateBehavior._get_facilities`` without importing scenario generators."""
    defaults = [
        {"name": "pickup_terminal_a", "lat": 1.290, "lon": 103.850},
        {"name": "dropoff_terminal_b", "lat": 1.330, "lon": 103.930},
    ]
    facilities = facility_settings.get("profile", {}).get("facilities", [])
    if not facilities:
        return list(defaults)
    resolved = []
    for idx, item in enumerate(facilities):
        d = defaults[idx % len(defaults)]
        resolved.append(
            {
                "name": item.get("name", d["name"]),
                "lat": item.get("lat", d["lat"]),
                "lon": item.get("lon", d["lon"]),
            }
        )
    return resolved


def _facility_center_by_name(name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    for fac in _configured_facilities():
        if fac.get("name") == name:
            lon = fac.get("lon")
            lat = fac.get("lat")
            if lon is None or lat is None:
                return None
            return {"type": "Point", "coordinates": [float(lon), float(lat)]}
    return None


def truck_anchor_loc(truck: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Best-effort truck position for repositioning cost.

    Live GPS is not guaranteed on the truck REST document; we fall back to the truck's
    configured home facility center when present.
    """
    prof = truck.get("profile") or {}
    for key in ("current_loc", "last_known_loc", "init_loc"):
        pt = _as_point(prof.get(key))
        if pt is not None:
            return pt
    return _facility_center_by_name(prof.get("home_facility_name"))


def order_pickup_loc(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prof = order.get("profile") or {}
    return _as_point(order.get("pickup_loc")) or _as_point(prof.get("pickup_loc"))


def order_dropoff_loc(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prof = order.get("profile") or {}
    return _as_point(order.get("dropoff_loc")) or _as_point(prof.get("dropoff_loc"))


def estimate_reposition_duration_seconds(truck: Dict[str, Any], order: Dict[str, Any]) -> Optional[float]:
    start = truck_anchor_loc(truck)
    end = order_pickup_loc(order)
    if start is None or end is None:
        return None
    return _haversine_seconds(start, end)


def _haversine_km(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    pa = _as_point(a)
    pb = _as_point(b)
    if pa is None or pb is None:
        return None
    lon1, lat1 = math.radians(pa["coordinates"][0]), math.radians(pa["coordinates"][1])
    lon2, lat2 = math.radians(pb["coordinates"][0]), math.radians(pb["coordinates"][1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _truck_last_dropoff_loc(truck: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prof = truck.get("profile") or {}
    return _as_point(prof.get("last_dropoff_loc"))


def _truck_last_dropoff_facility(truck: Dict[str, Any]) -> Optional[str]:
    prof = truck.get("profile") or {}
    name = prof.get("last_dropoff_facility_name")
    return str(name) if name not in (None, "") else None


# Default cost knobs (overridable via assignment_settings["profile"]["solver_params"]).
_DEFAULT_DUAL_CYCLE_BONUS_KM = 5.0
_DEFAULT_DUAL_CYCLE_RADIUS_KM = 0.5  # ~500m, mirrors the dual_cycle_rate KPI fallback
_UNKNOWN_COST = float("inf")


def assignment_cost(
    truck: Dict[str, Any],
    order: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> float:
    """Soft objective the greedy solver minimises: repositioning (deadhead) km.

    Base term = straight-line km from the truck's anchor (current/last-known
    position, else last drop-off, else home facility) to the order pickup — the
    empty distance the truck would drive to start the haul, i.e. the headline
    metric.

    Dual-cycle term: if the order's pickup is at / very near where the truck just
    dropped off, subtract a bonus so chained hauls (a truck's next pickup at its
    previous drop-off) are preferred — this is the same notion as the
    ``dual_cycle_rate`` KPI, applied at decision time. Facility-name match OR a
    ~500m haversine radius both count. Cost is clamped at 0 (never negative).

    Returns ``inf`` when geometry is missing so the solver ranks such pairs last
    and breaks them randomly instead of crashing.
    """
    params = params or {}
    pickup = order_pickup_loc(order)
    anchor = truck_anchor_loc(truck)
    if pickup is None or anchor is None:
        return _UNKNOWN_COST

    base_km = _haversine_km(anchor, pickup)
    if base_km is None:
        return _UNKNOWN_COST

    bonus_km = float(params.get("dual_cycle_bonus_km", _DEFAULT_DUAL_CYCLE_BONUS_KM))
    radius_km = float(params.get("dual_cycle_radius_km", _DEFAULT_DUAL_CYCLE_RADIUS_KM))

    is_dual_cycle = False
    last_fac = _truck_last_dropoff_facility(truck)
    order_pickup_fac = (order.get("profile") or {}).get("pickup_facility_name")
    if last_fac is not None and order_pickup_fac is not None and last_fac == str(order_pickup_fac):
        is_dual_cycle = True
    else:
        last_loc = _truck_last_dropoff_loc(truck)
        if last_loc is not None:
            d = _haversine_km(last_loc, pickup)
            if d is not None and d <= radius_km:
                is_dual_cycle = True

    cost = base_km - (bonus_km if is_dual_cycle else 0.0)
    return max(0.0, cost)


def haulier_of(entity: Dict[str, Any]) -> Optional[str]:
    """Stable haulier id for a truck or order (top-level, then profile)."""
    if not isinstance(entity, dict):
        return None
    hid = entity.get("haulier_id")
    if hid is None:
        prof = entity.get("profile") or {}
        hid = prof.get("haulier_id")
    return str(hid) if hid not in (None, "") else None


def haulier_matches(truck: Dict[str, Any], order: Dict[str, Any]) -> bool:
    """Strict rule: a truck may only take orders issued by its own haulier.

    Fail-closed — if either side has no haulier id the pair is rejected, so stale
    or partially-generated data can never bypass the constraint.
    """
    t_haulier = haulier_of(truck)
    o_haulier = haulier_of(order)
    if t_haulier is None or o_haulier is None:
        return False
    return t_haulier == o_haulier


def size_compatible(truck: Dict[str, Any], order: Dict[str, Any]) -> bool:
    """Nominal container / chassis compatibility (coarse)."""
    t_prof = truck.get("profile") or {}
    o_prof = order.get("profile") or {}
    truck_size = t_prof.get("truck_size", "20ft")
    order_size = o_prof.get("order_size") or order.get("order_size") or "1x20"
    if truck_size == "20ft" and order_size in ("2x20", "2x20ft", "40ft"):
        return False
    return True


def restricted_area_blocks(truck: Dict[str, Any], order: Dict[str, Any]) -> bool:
    """
    If the truck lists ``restricted_areas`` and the order names a facility whose *name*
    appears in that list, treat the pair as incompatible.
    """
    t_prof = truck.get("profile") or {}
    restricted = t_prof.get("restricted_areas") or []
    if not isinstance(restricted, (list, tuple)) or not restricted:
        return False
    restricted_set = {str(x) for x in restricted}
    o_prof = order.get("profile") or {}
    for key in ("pickup_facility_name", "dropoff_facility_name"):
        name = o_prof.get(key)
        if name and str(name) in restricted_set:
            return True
    return False


def within_max_reposition_seconds(
    truck: Dict[str, Any], order: Dict[str, Any], max_seconds: Optional[float]
) -> bool:
    if max_seconds is None:
        return True
    try:
        lim = float(max_seconds)
    except (TypeError, ValueError):
        return True
    if lim <= 0:
        return True
    eta = estimate_reposition_duration_seconds(truck, order)
    if eta is None:
        # Without geometry we cannot enforce the cap strictly; allow the pair.
        return True
    return eta <= lim



def pair_allowed_ignoring_haulier(
    truck: Dict[str, Any],
    order: Dict[str, Any],
    *,
    max_travel_time_pickup: Optional[float],
) -> bool:
    """Physical feasibility only (size / restricted areas / reposition cap).

    Used for CROSS-haulier pairs under a cooperation structure, where the
    haulier-equality rule is replaced by structure eligibility (the caller
    enforces ``share_eligible``). Own-haulier pairs keep ``pair_allowed``.
    """
    if not size_compatible(truck, order):
        return False
    if restricted_area_blocks(truck, order):
        return False
    if not within_max_reposition_seconds(truck, order, max_travel_time_pickup):
        return False
    return True


def pair_allowed(
    truck: Dict[str, Any],
    order: Dict[str, Any],
    *,
    max_travel_time_pickup: Optional[float],
) -> bool:
    if not haulier_matches(truck, order):
        return False
    return pair_allowed_ignoring_haulier(
        truck, order, max_travel_time_pickup=max_travel_time_pickup
    )


# --- cooperation structure readers (collaboration plan §1/§3) ----------------

def active_cooperation_structure(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The active structure dict from a baked ``profile.cooperation`` block, or
    ``None`` when the profile carries no (valid) cooperation config — which means
    exactly today's own-haulier-only behavior."""
    coop = (profile or {}).get("cooperation")
    if not isinstance(coop, dict):
        return None
    structures = coop.get("structures")
    if not isinstance(structures, list):
        return None
    by_id = {s.get("id"): s for s in structures if isinstance(s, dict)}
    return by_id.get(coop.get("active"))


def share_adjacency(structure: Optional[Dict[str, Any]]) -> Dict[str, set]:
    """Partner adjacency {haulier_id: set(partner ids)} of a structure (possibly empty)."""
    if not isinstance(structure, dict):
        return {}
    adj = structure.get("adjacency")
    if not isinstance(adj, dict):
        return {}
    return {str(k): {str(p) for p in v} for k, v in adj.items() if isinstance(v, (list, tuple, set))}


def share_eligible(truck: Dict[str, Any], order: Dict[str, Any], adjacency: Dict[str, set]) -> bool:
    """True when the structure permits this CROSS-haulier pair (edge-direct only).

    Fail-closed like ``haulier_matches``: missing ids or an empty adjacency never
    become eligible."""
    t_haulier = haulier_of(truck)
    o_haulier = haulier_of(order)
    if t_haulier is None or o_haulier is None or t_haulier == o_haulier:
        return False
    return o_haulier in adjacency.get(t_haulier, ())


def pools_of_structure(structure: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Read ``structure['pools']`` defensively (list of ``{id, members}``);
    ``[]`` when absent, not a list, or the structure itself is missing.

    Mirrors ``share_adjacency``'s defensiveness: never raises. Malformed
    entries (not a dict, missing/empty id, missing/non-iterable members,
    fewer than 2 distinct string members) are skipped rather than
    propagated. Returned entries are normalized: ``members`` sorted, ``id``
    kept verbatim.
    """
    if not isinstance(structure, dict):
        return []
    raw_pools = structure.get("pools")
    if not isinstance(raw_pools, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in raw_pools:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        members_raw = entry.get("members")
        if not isinstance(members_raw, (list, tuple, set, frozenset)):
            continue
        members = sorted({str(m) for m in members_raw if isinstance(m, str) and m})
        if len(members) < 2:
            continue
        out.append({"id": pid, "members": members})
    return out


def filter_assignable_trucks(
    trucks: List[Dict[str, Any]],
    busy_truck_ids: Set[str],
    *,
    respect_truck_online_state: bool,
    reject_if_active_haul_trip: bool,
    online_state_name: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in trucks:
        tid = t.get("_id")
        if tid is None:
            continue
        tid_s = str(tid)
        if respect_truck_online_state and t.get("state") != online_state_name:
            continue
        if reject_if_active_haul_trip and tid_s in busy_truck_ids:
            continue
        out.append(t)
    return out
