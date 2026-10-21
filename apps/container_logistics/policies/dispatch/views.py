"""Typed, read-only views of the raw truck and order documents a policy sees.

Built lazily by ``DispatchContext.trucks`` / ``.orders`` with the SAME accessors the
constraints use (``truck_anchor_loc``, ``order_pickup_loc``, ``haulier_of``, ...), so
a view can never disagree with feasibility or cost. Positions are ``(lon, lat)``
tuples. Each view keeps its source document as ``.raw``; the built-in policies use
raw documents and never build views (plan ``docs/plans/policy_slots_p0_p3.md``
§4 P1.5).

Framework side of the boundary. Not a leaf module: imports
``assignment.constraints`` at top (plan §2.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from apps.container_logistics.assignment import constraints as assign_constraints

logger = logging.getLogger(__name__)

__all__ = ["TruckView", "OrderView", "truck_view", "order_view"]

# Defaults copied from constraints.size_compatible, which is where sizes are read.
_DEFAULT_TRUCK_SIZE = "20ft"
_DEFAULT_ORDER_SIZE = "1x20"


@dataclass(frozen=True)
class TruckView:
    """One truck as a policy sees it. ``raw`` is excluded from equality and hashing."""

    id: str
    haulier_id: str | None
    size: str
    position: tuple[float, float] | None
    last_dropoff_position: tuple[float, float] | None
    last_dropoff_facility: str | None
    raw: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class OrderView:
    """One order as a policy sees it. ``raw`` is excluded from equality and hashing."""

    id: str
    haulier_id: str | None
    size: str
    pickup: tuple[float, float] | None
    dropoff: tuple[float, float] | None
    # *_facility_name, not *_facility: the bare names are the order-embedded facility
    # SNAPSHOT, which tests/test_order_service_time_path.py keeps reader-free.
    pickup_facility_name: str | None
    dropoff_facility_name: str | None
    raw: dict[str, Any] = field(compare=False, repr=False)


def _lonlat(point: dict[str, Any] | None) -> tuple[float, float] | None:
    if point is None:
        return None
    lon, lat = point["coordinates"][0], point["coordinates"][1]
    return (float(lon), float(lat))


def _name_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def truck_view(truck: dict[str, Any]) -> TruckView:
    """Build a :class:`TruckView` from a raw truck document."""
    prof = truck.get("profile") or {}
    return TruckView(
        id=str(truck.get("_id")),
        haulier_id=assign_constraints.haulier_of(truck),
        size=str(prof.get("truck_size", _DEFAULT_TRUCK_SIZE)),
        position=_lonlat(assign_constraints.truck_anchor_loc(truck)),
        last_dropoff_position=_lonlat(assign_constraints._truck_last_dropoff_loc(truck)),
        last_dropoff_facility=assign_constraints._truck_last_dropoff_facility(truck),
        raw=truck,
    )


def order_view(order: dict[str, Any]) -> OrderView:
    """Build an :class:`OrderView` from a raw order document."""
    prof = order.get("profile") or {}
    return OrderView(
        id=str(order.get("_id")),
        haulier_id=assign_constraints.haulier_of(order),
        size=str(prof.get("order_size") or order.get("order_size") or _DEFAULT_ORDER_SIZE),
        pickup=_lonlat(assign_constraints.order_pickup_loc(order)),
        dropoff=_lonlat(assign_constraints.order_dropoff_loc(order)),
        pickup_facility_name=_name_or_none(prof.get("pickup_facility_name")),
        dropoff_facility_name=_name_or_none(prof.get("dropoff_facility_name")),
        raw=order,
    )
