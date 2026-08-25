"""Data-driven location-code registry.

datagen holds **no** hardcoded location codes. The active set is discovered from
the address book (which codes have real coordinates). Per-code presentation
(display label, facility-name prefix) and facility-mix share come from
caller-supplied metadata, with derived defaults so a brand-new code that simply
appears in the CSV (and the trip matrix) works with zero configuration:

    label     : metadata["label"]     -> else code.title()
    prefix    : metadata["prefix"]    -> else label/code lowercased
    weight    : metadata["weight"]    -> else its trip-matrix marginal -> else equal
    mask_file : metadata["mask_file"] -> else None (the code gets no footprint mask)

A new code therefore needs no edits to this package; supply a metadata entry only
when you want a nicer label than the auto-derived one, or a footprint mask file.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional


@dataclass(frozen=True)
class LocationType:
    code: str
    label: str          # display + facility_type, e.g. "Port", "Rail Terminal"
    name_prefix: str     # facility id prefix, e.g. "port"
    facility_weight: float  # relative share of the facility mix
    mask_file: Optional[str] = None  # real-footprint region file, or None (no mask)


def derive_label(code: str, metadata: Optional[dict] = None) -> str:
    """Display/location-type label for a code: explicit metadata, else derived."""
    code = str(code or "").strip().upper()
    entry = (metadata or {}).get(code) or {}
    return entry.get("label") or (code.title() if code else "Unknown")


def _derive_prefix(code: str, label: str, metadata: Optional[dict]) -> str:
    entry = (metadata or {}).get(code) or {}
    raw = entry.get("prefix") or label or code
    slug = "".join(ch if ch.isalnum() else "_" for ch in str(raw).strip().lower())
    return slug.strip("_") or code.lower()


def trip_matrix_marginals(trip_matrix: Optional[dict], codes) -> dict[str, float]:
    """Total incidence of each code as a pickup or delivery in ``trip_matrix``.

    This is the demand signal used to size the facility mix: a code that appears
    in more trips gets a proportionally larger share of facilities.
    """
    marg = {c: 0.0 for c in codes}
    if not trip_matrix:
        return marg
    for pickup, row in trip_matrix.items():
        if not isinstance(row, dict):
            continue
        for delivery, weight in row.items():
            try:
                w = float(weight)
            except (TypeError, ValueError):
                continue
            if pickup in marg:
                marg[pickup] += w
            if delivery in marg:
                marg[delivery] += w
    return marg


class CodeRegistry:
    """Single source of truth for the active location codes and their metadata."""

    def __init__(self, types: dict[str, LocationType]):
        self._types = dict(types)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(self._types)

    def has(self, code: str) -> bool:
        return code in self._types

    def label(self, code: str) -> str:
        t = self._types.get(code)
        return t.label if t else derive_label(code)

    # facility_type and label are the same concept here.
    facility_type = label

    def prefix(self, code: str) -> str:
        t = self._types.get(code)
        return t.name_prefix if t else str(code).lower()

    def weight(self, code: str) -> float:
        t = self._types.get(code)
        return t.facility_weight if t else 0.0

    def mask_files(self) -> dict[str, str]:
        """Map each code that has a configured footprint region file to its
        filename. Codes with no ``mask_file`` are omitted, so masks stay optional:
        a facility gets a footprint only when its code declares an available mask.
        """
        return {c: t.mask_file for c, t in self._types.items() if t.mask_file}

    @classmethod
    def build(
        cls,
        address_codes,
        trip_matrix: Optional[dict] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "CodeRegistry":
        """Build a registry for ``address_codes`` (codes that have real addresses).

        Facility weight per code: explicit ``metadata[code]["weight"]`` if given,
        else the code's trip-matrix marginal (demand-proportional), else 0. If no
        code ends up with positive weight, fall back to an equal split so a
        scenario without matrix signal still produces a balanced facility set.
        """
        metadata = metadata or {}
        codes = list(dict.fromkeys(str(c).strip().upper() for c in address_codes if c))
        marginals = trip_matrix_marginals(trip_matrix, codes)

        types: dict[str, LocationType] = {}
        for code in codes:
            label = derive_label(code, metadata)
            prefix = _derive_prefix(code, label, metadata)
            entry = metadata.get(code) or {}
            weight = entry.get("weight")
            if weight is None:
                weight = marginals.get(code, 0.0)
            mask_file = entry.get("mask_file") or None
            types[code] = LocationType(
                code, label, prefix, max(0.0, float(weight)), mask_file
            )

        if sum(t.facility_weight for t in types.values()) <= 0:
            types = {c: replace(t, facility_weight=1.0) for c, t in types.items()}
        return cls(types)
