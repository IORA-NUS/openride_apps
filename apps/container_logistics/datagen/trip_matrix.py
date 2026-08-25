"""Pickup -> delivery trip distribution.

Fully data-driven: the set of location codes is taken from the matrix itself (and
from the address book), not a hardcoded list. A code that appears in the matrix is
kept; one that has no real addresses is dropped via :func:`restrict_trip_matrix`.

This module is pure (stdlib only) and holds **no** baked-in probabilities or code
list — the matrix is an *input* to data generation. Empty/all-zero input raises
rather than silently inventing a default (the scenario/config layer supplies the
observed default, e.g. ``scenario_config.DEFAULT_TRIP_MATRIX``).
"""

from __future__ import annotations

import random
from typing import Any, Optional


def location_type_for_code(code: str, metadata: Optional[dict] = None) -> str:
    """Display/location-type label for a code.

    Uses ``metadata[code]["label"]`` when provided, otherwise derives a label from
    the code itself (title-cased). No hardcoded code->label table lives here.
    """
    code = str(code or "").strip().upper()
    entry = (metadata or {}).get(code) or {}
    return entry.get("label") or (code.title() if code else "Unknown")


def _codes_in(raw: Any) -> list[str]:
    """All codes referenced as a pickup or delivery key in a raw matrix."""
    codes: list[str] = []
    if isinstance(raw, dict):
        for pickup, row in raw.items():
            codes.append(str(pickup).strip().upper())
            if isinstance(row, dict):
                for delivery in row:
                    codes.append(str(delivery).strip().upper())
    # stable, de-duplicated, sorted for determinism
    return sorted({c for c in codes if c})


def parse_trip_matrix(raw: Any) -> dict[str, dict[str, float]]:
    """Validate/normalize a supplied pickup->delivery trip matrix.

    The code set is discovered from ``raw`` itself, so any code present is kept.
    Missing cells are 0, negatives are clamped to 0, the diagonal is forced to 0,
    and the grid is normalized to sum to 1.

    Raises ``ValueError`` if ``raw`` yields no positive off-diagonal weight — the
    matrix is a required input, not something this module invents a default for.
    """
    codes = _codes_in(raw)
    grid: dict[str, dict[str, float]] = {p: {d: 0.0 for d in codes} for p in codes}
    if isinstance(raw, dict):
        for pickup, row in raw.items():
            p = str(pickup).strip().upper()
            if p not in grid or not isinstance(row, dict):
                continue
            for delivery, weight in row.items():
                d = str(delivery).strip().upper()
                if d not in grid[p] or p == d:
                    continue  # force-zero the diagonal
                try:
                    grid[p][d] = max(0.0, float(weight))
                except (TypeError, ValueError):
                    continue

    total = sum(grid[p][d] for p in codes for d in codes)
    if total <= 0:
        raise ValueError(
            "A pickup->delivery trip matrix must be provided with at least one "
            f"positive off-diagonal weight; got: {raw!r}"
        )
    return {p: {d: grid[p][d] / total for d in codes} for p in codes}


def restrict_trip_matrix(matrix: dict, allowed_codes) -> dict[str, dict[str, float]]:
    """Drop codes not in ``allowed_codes`` (e.g. excluded, or with no addresses)
    and renormalize. Raises ``ValueError`` if nothing survives."""
    allowed = {str(c).strip().upper() for c in allowed_codes}
    grid = {
        p: {d: float(w) for d, w in row.items() if d in allowed and d != p}
        for p, row in matrix.items()
        if p in allowed
    }
    total = sum(w for row in grid.values() for w in row.values())
    if total <= 0:
        raise ValueError(
            f"No trip-matrix weight remains after restricting to codes {sorted(allowed)}"
        )
    return {p: {d: w / total for d, w in row.items()} for p, row in grid.items()}


def _pairs_and_weights(matrix: dict):
    """Flatten a (required) normalized matrix into parallel (pair, weight) lists."""
    if not matrix:
        raise ValueError("sample_pickup_delivery_codes requires a trip matrix")
    pairs: list[tuple[str, str]] = []
    weights: list[float] = []
    for pickup, row in matrix.items():
        for delivery, weight in row.items():
            pairs.append((pickup, delivery))
            weights.append(float(weight))
    if sum(weights) <= 0:
        raise ValueError("trip matrix has no positive weight to sample from")
    return pairs, weights


def sample_pickup_delivery_codes(rng=None, *, matrix) -> tuple[str, str]:
    """Return a (pickup_code, delivery_code) pair sampled from ``matrix``.

    ``matrix`` is required (a normalized nested mapping from :func:`parse_trip_matrix`).
    Codes are whatever the matrix contains — there is no built-in code list.
    """
    rng = rng or random
    pairs, weights = _pairs_and_weights(matrix)
    return rng.choices(pairs, weights=weights, k=1)[0]
