"""The "sample" half of data generation.

``LocationCatalog`` loads the real Singapore address book
(``locations_cleaned.csv``) and samples genuine on-land coordinates by location
code. It is the single source of truth for *where* anything is placed.

What this deliberately removes / avoids
---------------------------------------
- No polygon-interior sampling and no land-clamp patchwork — every coordinate is a
  real postal address, on land by construction.
- **No hardcoded code list.** Codes are discovered from the CSV; an explicit
  ``excluded`` denylist (e.g. ``{"YD"}``) is the only filter. A brand-new code in
  the data is picked up automatically.
- Per-code presentation and facility-mix share come from a :class:`CodeRegistry`
  (passed in), not constants in this module.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Iterable, Optional

from .codes import CodeRegistry
from .facility_masks import FacilityMaskIndex

# Fixed seed for *facility-site* selection so a regenerated scenario keeps its
# facilities in the same real places. Per-agent sampling uses the live RNG.
_FACILITY_SAMPLE_SEED = 20260617


def _openroad_locations_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "openroad_locations"


def default_locations_csv() -> str:
    """Absolute path to the real address book under ``openroad_locations/``."""
    return str(_openroad_locations_dir() / "locations_cleaned.csv")


# Candidate filenames for the (gov-supplied) Singapore land mask, most specific
# first. Returns None if none exist — generation then runs without validation.
_SG_MASK_CANDIDATES = (
    "SingaporeMaskNoSea.geojson",
    "singapore_boundary.geojson",
    "singapore_mask.geojson",
    "sg_boundary.geojson",
)


def default_sg_mask_path() -> Optional[str]:
    """Path to the Singapore land mask in openroad_locations/, or None if absent."""
    base = _openroad_locations_dir()
    file = base / "SingaporeMaskNoSea.geojson"
    if file.is_file():
        return str(file)
    return None


def _allocate_facility_counts(n: int, available: dict[str, int], registry: CodeRegistry) -> dict[str, int]:
    """Split ``n`` facilities across codes by their registry weight.

    Largest-remainder allocation against each code's weight (demand-proportional by
    default), capped at the number of real addresses available, with overflow
    spilled to the highest-weight codes. Every weighted code is guaranteed at least
    one facility (availability and ``n`` permitting) so its orders can be placed.
    """
    n = max(1, int(n))
    weighted = [c for c in registry.codes if available.get(c, 0) > 0 and registry.weight(c) > 0]
    if not weighted:  # no demand signal — fall back to any code with addresses
        weighted = [c for c in registry.codes if available.get(c, 0) > 0]
    if not weighted:
        return {c: 0 for c in registry.codes}

    weights = {c: (registry.weight(c) or 1.0) for c in weighted}
    wtot = sum(weights.values()) or float(len(weighted))
    exact = {c: n * weights[c] / wtot for c in weighted}
    counts = {c: int(exact[c]) for c in weighted}

    remainder = n - sum(counts.values())
    by_frac = sorted(weighted, key=lambda c: exact[c] - counts[c], reverse=True)
    for i in range(remainder):
        counts[by_frac[i % len(weighted)]] += 1

    overflow = 0
    for c in weighted:
        cap = available.get(c, 0)
        if counts[c] > cap:
            overflow += counts[c] - cap
            counts[c] = cap
    for c in sorted(weighted, key=lambda c: weights[c], reverse=True):
        if overflow <= 0:
            break
        room = available.get(c, 0) - counts[c]
        if room > 0:
            take = min(room, overflow)
            counts[c] += take
            overflow -= take

    # Guarantee every weighted code at least one facility, stealing from the
    # largest holder (only when that does not zero the donor).
    for c in weighted:
        if counts[c] == 0 and available.get(c, 0) > 0:
            donor = max(weighted, key=lambda x: counts[x])
            if counts[donor] > 1:
                counts[donor] -= 1
                counts[c] += 1

    return {c: counts.get(c, 0) for c in registry.codes}


class SingaporeMask:
    """Optional on-land validation/clip against a Singapore boundary polygon.

    No-op until a GeoJSON path is supplied. Real addresses are already on land, so
    this is a safety net rather than load-bearing logic.
    """

    def __init__(self, geojson_path: Optional[str] = None):
        self.path = geojson_path
        self._geom = None
        if geojson_path and Path(geojson_path).is_file():
            self._geom = self._load(geojson_path)

    @staticmethod
    def _load(path: str):
        import json

        from shapely.geometry import shape
        from shapely.ops import unary_union
        from shapely.prepared import prep

        with open(path) as fp:
            data = json.load(fp)
        geoms = [shape(feat["geometry"]) for feat in data.get("features", [])]
        if not geoms:
            return None
        # Prepared geometry: ~200x faster point-in-polygon for many queries.
        return prep(unary_union(geoms))

    @property
    def active(self) -> bool:
        return self._geom is not None

    def contains(self, lon: float, lat: float) -> bool:
        if self._geom is None:
            return True
        from shapely.geometry import Point

        return bool(self._geom.contains(Point(lon, lat)))


class LocationCatalog:
    """Real-address sampler. Loads the CSV once and indexes by location code."""

    def __init__(
        self,
        locations_csv: Optional[str] = None,
        mask_path: Optional[str] = None,
        excluded: Iterable[str] = (),
    ):
        self.locations_csv = locations_csv or default_locations_csv()
        self.mask = SingaporeMask(mask_path)
        self.excluded = {str(c).strip().upper() for c in (excluded or ())}
        self._by_code = self._load(self.locations_csv, self.mask, self.excluded)

    @staticmethod
    def _load(csv_path: str, mask: SingaporeMask, excluded: set) -> dict[str, list[tuple[float, float]]]:
        by_code: dict[str, list[tuple[float, float]]] = {}
        with open(csv_path, newline="") as fp:
            for row in csv.DictReader(fp):
                code = (row.get("code") or "").strip().upper()
                if not code or code in excluded:
                    continue  # every other code is discovered automatically
                try:
                    lon, lat = float(row["lon"]), float(row["lat"])
                except (TypeError, ValueError, KeyError):
                    continue
                if mask.active and not mask.contains(lon, lat):
                    continue
                by_code.setdefault(code, []).append((lon, lat))
        return by_code

    def add_manual_site(self, code: str, lat: float, lon: float, *, name: Optional[str] = None) -> None:
        """Add a coordinate to ``code``'s sampling pool (e.g. a site not in the CSV).

        For use from a per-scenario ``customize_catalog`` override — makes the code
        samplable / facility-allocatable at this point. ``name`` is accepted for
        readability; facility names are still generated from the code's prefix.
        """
        code = str(code or "").strip().upper()
        if not code:
            raise ValueError("add_manual_site requires a non-empty location code")
        self._by_code.setdefault(code, []).append((float(lon), float(lat)))

    def codes(self) -> tuple[str, ...]:
        """All location codes that have at least one real address (sorted)."""
        return tuple(sorted(self._by_code))

    def available(self) -> dict[str, int]:
        return {c: len(v) for c, v in self._by_code.items()}

    def sample(self, code: str, rng=None) -> tuple[float, float]:
        """Return a real (lon, lat) for ``code``; falls back to the densest pool."""
        rng = rng or random
        code = str(code or "").strip().upper()
        pool = self._by_code.get(code) or []
        if not pool:
            for alt in sorted(self._by_code, key=lambda c: len(self._by_code[c]), reverse=True):
                pool = self._by_code[alt]
                if pool:
                    break
        if not pool:
            raise ValueError(f"No real addresses available for code {code!r}")
        return rng.choice(pool)

    def sample_origin(self, codes: Optional[Iterable[str]] = None, rng=None) -> tuple[float, float]:
        """Sample a real start coordinate from ``codes`` (default: any code)."""
        rng = rng or random
        pool_codes = [c for c in (codes or self.codes()) if self._by_code.get(c)]
        if not pool_codes:
            pool_codes = [c for c in self.codes() if self._by_code.get(c)]
        if not pool_codes:
            raise ValueError("No real addresses available to sample a truck origin")
        return self.sample(rng.choice(pool_codes), rng)

    def facility_sites(
        self,
        n: int,
        registry: CodeRegistry,
        *,
        gate_count: int = 1,
        service_time: int = 1800,
    ) -> list[dict]:
        """Build ``n`` facility site dicts spanning the registry's codes from real
        addresses. Each site carries ``code`` and ``facility_type`` so order
        generation can snap a leg to a facility of the matching type. Selection is
        stable across regenerations (fixed seed)."""
        counts = _allocate_facility_counts(n, self.available(), registry)
        rng = random.Random(_FACILITY_SAMPLE_SEED)
        # Real footprint polygons, only for codes that declare a mask file; no-op
        # for codes with no configured mask or whose file is absent.
        masks = FacilityMaskIndex(registry.mask_files())
        sites: list[dict] = []
        for code in registry.codes:
            want = counts.get(code, 0)
            pool = list(self._by_code.get(code, []))
            if want <= 0 or not pool:
                continue
            chosen = pool if want >= len(pool) else rng.sample(pool, want)
            prefix = registry.prefix(code)
            facility_type = registry.facility_type(code)
            for i, (lon, lat) in enumerate(chosen):
                site = {
                    "name": f"{prefix}_{i:03d}",
                    "lat": lat,
                    "lon": lon,
                    "gate_count": gate_count,
                    "service_time": service_time,
                    "code": code,
                    "facility_type": facility_type,
                }
                footprint = masks.lookup(code, lon, lat)
                if footprint is not None:
                    site["footprint"] = footprint
                sites.append(site)
        return sites
