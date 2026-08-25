"""
Spatial candidate generation for truck<->order matching.

The greedy solver scores *every* (truck, order) pair — O(trucks x orders) — which
is fine at a few hundred trucks but collapses at fleet scale (a 5000-truck fleet
builds ~2.5M pairs/tick and never finishes inside the scheduler step barrier).

Almost all of those pairs are immediately rejected by the ``max_travel_time_pickup``
constraint or rank terribly on deadhead cost: a truck on one side of the map is
measured against an order on the other only to be thrown away. This module avoids
*generating* those far pairs in the first place by bucketing trucks into a uniform
lat/lon grid and, for each order, returning only the trucks in nearby cells.

Why this beats the random-sampling cap: it's equally bounded, but the candidates
are the *nearest* trucks — exactly the low-deadhead matches the simulation exists
to optimise — so it bounds cost AND improves match quality. Pure-Python + the
existing haversine helpers; no new dependencies (h3/scipy/sklearn are absent).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import constraints as assign_constraints

# ~0.02 deg ~= 2.2 km cells: tuned so a cell holds ~10-50 trucks at Singapore
# fleet density. Overridable via the assignment profile.
DEFAULT_CELL_DEG = 0.02
DEFAULT_PER_ORDER_CANDIDATES = 40
DEFAULT_MAX_RINGS = 8

Cell = Tuple[int, int]


def _cell_of(lon: float, lat: float, cell_deg: float) -> Cell:
    return (int(math.floor(lat / cell_deg)), int(math.floor(lon / cell_deg)))


def _ring(center: Cell, r: int) -> Iterator[Cell]:
    """Yield the cells at exactly Chebyshev distance ``r`` from ``center``."""
    cy, cx = center
    if r == 0:
        yield center
        return
    for dx in range(-r, r + 1):
        yield (cy - r, cx + dx)
        yield (cy + r, cx + dx)
    for dy in range(-r + 1, r):
        yield (cy + dy, cx - r)
        yield (cy + dy, cx + r)


class SpatialTruckIndex:
    """Buckets trucks by grid cell using their best-effort anchor position."""

    def __init__(self, trucks: List[Dict[str, Any]], *, cell_deg: float = DEFAULT_CELL_DEG):
        self.cell_deg = cell_deg
        self._grid: Dict[Cell, List[Dict[str, Any]]] = defaultdict(list)
        # Trucks with no usable coordinate (no current/last/init loc, no home
        # facility) — kept as last-resort candidates so they are never starved.
        self._unplaced: List[Dict[str, Any]] = []
        for t in trucks:
            pt = assign_constraints.truck_anchor_loc(t)
            if pt is None:
                self._unplaced.append(t)
                continue
            lon, lat = pt["coordinates"][0], pt["coordinates"][1]
            self._grid[_cell_of(lon, lat, cell_deg)].append(t)

    def nearby(
        self,
        lon: float,
        lat: float,
        *,
        cap: int,
        max_rings: int,
    ) -> List[Dict[str, Any]]:
        center = _cell_of(lon, lat, self.cell_deg)
        out: List[Dict[str, Any]] = []
        r = 0
        while r <= max_rings and len(out) < cap:
            for cell in _ring(center, r):
                bucket = self._grid.get(cell)
                if bucket:
                    out.extend(bucket)
            r += 1
        if len(out) < cap and self._unplaced:
            out.extend(self._unplaced)
        return out[:cap]


def iter_candidate_pairs(
    trucks: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
    *,
    pair_allowed: Callable[[Dict[str, Any], Dict[str, Any]], bool],
    cell_deg: float = DEFAULT_CELL_DEG,
    per_order: int = DEFAULT_PER_ORDER_CANDIDATES,
    max_rings: int = DEFAULT_MAX_RINGS,
) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Yield feasible (truck, order) pairs for spatially-near trucks only.

    ``trucks``/``orders`` are expected to already share a haulier (callers bucket
    by haulier first). Builds the index once, then expands rings per order until
    ``per_order`` candidates are gathered, applying ``pair_allowed`` so only
    feasible pairs are emitted.
    """
    if not trucks or not orders:
        return
    index = SpatialTruckIndex(trucks, cell_deg=cell_deg)
    for order in orders:
        pickup = assign_constraints.order_pickup_loc(order)
        if pickup is None:
            # No pickup geometry: fall back to an arbitrary bounded subset so the
            # order can still be matched rather than silently dropped.
            candidates: List[Dict[str, Any]] = trucks[:per_order]
        else:
            lon, lat = pickup["coordinates"][0], pickup["coordinates"][1]
            candidates = index.nearby(lon, lat, cap=per_order, max_rings=max_rings)
        for truck in candidates:
            if pair_allowed(truck, order):
                yield (truck, order)
