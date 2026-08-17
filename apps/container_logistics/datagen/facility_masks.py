"""Match a facility's real address to its footprint polygon (its "mask").

Masks are **optional and per-code**: a code only gets footprints when the caller
supplies a region file for it (via ``region_files``) and that file exists. Codes
with no configured/present mask simply get no footprint — better no footprint than
a wrong one. The mapping is data-driven (it comes from ``LOCATION_TYPE_METADATA``'s
``mask_file`` entries, surfaced by ``CodeRegistry.mask_files()``); this module
hardcodes no codes.

The region files live in ``openroad_locations/``. Today only ports and depots have
real footprint data (``port_regions_cleaned.geojson`` /
``depot_regions_cleaned.geojson``); customers/warehouses (CU) have none yet, so
they are mask-less until a real warehouse footprint file is provided.

A facility address is a postal coordinate that usually sits *inside* its parcel;
when it does not (a gate/office just outside the fence), we fall back to the
nearest parcel within a small radius. Beyond that radius we return ``None``.

Pure & optional, in keeping with the rest of ``datagen``: every lookup returns
``None`` when no mask is configured or the region file is absent, so generation
still runs standalone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def _default_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "openroad_locations"


# Nearest-parcel fallback only accepts a match within this centroid/edge distance
# (degrees; ~111 km per degree at the equator, so ~1.1 km here). A facility whose
# address is farther than this from any parcel simply gets no footprint.
_MAX_FALLBACK_DEG = 0.01

# Light Douglas-Peucker simplification (~3 m) trims OSM vertex noise so the
# polygon stays compact in the facility profile / SSE payload without visibly
# changing shape.
_SIMPLIFY_TOL_DEG = 0.00003


class _Region:
    """One region file's polygons indexed for fast point queries."""

    def __init__(self, geoms, tree):
        self.geoms = geoms
        self.tree = tree


class FacilityMaskIndex:
    """Resolves ``(code, lon, lat)`` to a footprint GeoJSON geometry, or ``None``.

    ``region_files`` maps a location code to the region file holding that type's
    real footprints (e.g. ``{"CT": "port_regions_cleaned.geojson"}``). Codes absent
    from the mapping have no mask; an empty mapping (the default) disables masks
    entirely. Region files are loaded lazily and cached the first time a code that
    needs them is looked up, so a run that only places ports never loads files for
    other codes.
    """

    def __init__(
        self,
        region_files: Optional[dict[str, str]] = None,
        base_dir: Optional[str] = None,
    ):
        self._base = Path(base_dir) if base_dir else _default_dir()
        # code -> region filename (only codes with a configured mask).
        self._region_files = {
            str(c or "").strip().upper(): f
            for c, f in (region_files or {}).items()
            if f
        }
        # filename -> _Region | None (None = tried and unavailable/empty)
        self._regions: dict[str, Optional[_Region]] = {}

    def _region(self, filename: str) -> Optional[_Region]:
        if filename in self._regions:
            return self._regions[filename]

        region: Optional[_Region] = None
        path = self._base / filename
        if path.is_file():
            try:
                from shapely.geometry import shape
                from shapely.strtree import STRtree

                with open(path) as fp:
                    data = json.load(fp)
                geoms = [
                    shape(feat["geometry"])
                    for feat in data.get("features", [])
                    if feat.get("geometry")
                ]
                geoms = [g for g in geoms if not g.is_empty]
                if geoms:
                    region = _Region(geoms, STRtree(geoms))
            except Exception:
                region = None  # corrupt/unreadable -> behave as absent

        self._regions[filename] = region
        return region

    def lookup(self, code: str, lon: float, lat: float) -> Optional[dict]:
        """Return the facility's footprint as a GeoJSON geometry dict, or None."""
        filename = self._region_files.get(str(code or "").strip().upper())
        if not filename:
            return None
        region = self._region(filename)
        if region is None:
            return None

        try:
            from shapely.geometry import Point, mapping
        except Exception:
            return None

        pt = Point(lon, lat)

        # 1) Exact: the parcel that contains the address.
        for idx in region.tree.query(pt):
            geom = region.geoms[int(idx)]
            if geom.contains(pt):
                return mapping(geom.simplify(_SIMPLIFY_TOL_DEG, preserve_topology=True))

        # 2) Fallback: the nearest parcel, but only if it is plausibly the same site.
        try:
            nearest_idx = int(region.tree.nearest(pt))
        except Exception:
            return None
        geom = region.geoms[nearest_idx]
        if geom.distance(pt) <= _MAX_FALLBACK_DEG:
            return mapping(geom.simplify(_SIMPLIFY_TOL_DEG, preserve_topology=True))

        return None
