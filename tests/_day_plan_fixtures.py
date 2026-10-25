"""Synthetic raw truck / order documents for the day-plan tests (no I/O, no ortools).

Shapes follow the real Mongo documents (``container_logistics_truck`` /
``container_logistics_order``, read 2026-10-25 from ``run_20261021_175341``): ``_id`` a
24-hex ObjectId string, ``profile.haulier_id``, ``profile.truck_size``,
``profile.restricted_areas``, ``profile.current_loc`` / ``init_loc`` GeoJSON points,
``profile.pickup_loc`` / ``dropoff_loc``, ``profile.*_facility_name``, ``state``,
``meta.request_time_step``.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

STEP_S = 240.0
DAY_S = 86400.0

FACILITIES = {
    "port_000": (103.8319, 1.2605), "port_001": (103.8247, 1.2648),
    "depot_000": (103.6266, 1.3113), "depot_001": (103.7012, 1.3331),
    "customer_000": (103.7904, 1.2809), "customer_001": (103.9199, 1.3228),
    "customer_002": (103.7584, 1.4053), "customer_003": (103.8395, 1.4424),
}


def oid(kind: str, n: int) -> str:
    """A deterministic 24-hex id (the real ids are ObjectId strings)."""
    return hashlib.blake2b(f"{kind}:{n}".encode(), digest_size=12).hexdigest()


def point(lonlat) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [float(lonlat[0]), float(lonlat[1])]}


def truck(n: int, haulier: str, *, at: str = "depot_000", size: str = "20ft",
          areas=("West Coast", "MBS"), loc=None) -> dict[str, Any]:
    return {"_id": oid("truck", n), "state": "idle",
            "profile": {"haulier_id": haulier, "truck_size": size, "restricted_areas": list(areas),
                        "home_facility_name": at,
                        "current_loc": point(loc if loc is not None else FACILITIES[at])}}


def order(n: int, haulier: str, step: int, pickup: str, dropoff: str, *, state: str = "unassigned",
          size: str = "1x20") -> dict[str, Any]:
    return {"_id": oid("order", n), "state": state, "meta": {"request_time_step": int(step)},
            "profile": {"haulier_id": haulier, "order_size": size,
                        "pickup_facility_name": pickup, "dropoff_facility_name": dropoff,
                        "pickup_loc": point(FACILITIES[pickup]), "dropoff_loc": point(FACILITIES[dropoff])}}


def synthetic_day(seed: int, *, n_orders: int, n_trucks: int, hauliers=("acme", "cargo", "delta"),
                  day: int = 0, jitter: bool = True):
    """``(trucks, orders)`` for one run day: orders spread over steps of ``day``."""
    rng = random.Random(seed)
    names = sorted(FACILITIES)
    trucks = []
    for k in range(n_trucks):
        h = hauliers[k % len(hauliers)]
        at = names[rng.randrange(len(names))]
        lon, lat = FACILITIES[at]
        loc = (lon + rng.uniform(-0.01, 0.01), lat + rng.uniform(-0.01, 0.01)) if jitter else None
        trucks.append(truck(k, h, at=at, loc=loc))
    orders = []
    for k in range(n_orders):
        h = hauliers[rng.randrange(len(hauliers))]
        a, b = rng.sample(names, 2)
        step = day * 360 + rng.randrange(20, 340)
        orders.append(order(k, h, step, a, b, state="created"))
    return trucks, orders
