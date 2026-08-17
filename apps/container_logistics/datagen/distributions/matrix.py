"""ProbabilityMatrix — a general joint distribution over a pair of discrete axes.

The container "trip matrix" (pickup_code -> dropoff_code) is one instance:
``ProbabilityMatrix(axes=["pickup_code", "dropoff_code"])``. The same class also
expresses a haulier mix, an order-type mix, etc. It can be built from authored
weights (``from_weights``) or **learned from historical records**
(``from_records``) — the generalization ladder the redesign hinges on.

Pairwise (2 axes) by design (see plan §4.5). Sampling reuses the pure,
behavior-tested helpers in ``trip_matrix`` so default generation is unchanged.
"""

from __future__ import annotations

import random
from typing import Any, Optional, Sequence

from ..trip_matrix import (
    parse_trip_matrix,
    restrict_trip_matrix,
    sample_pickup_delivery_codes,
)
from .base import Distribution
from .categorical import Categorical

# Default axis labels — the container OD matrix.
_DEFAULT_AXES = ("pickup_code", "dropoff_code")


class ProbabilityMatrix(Distribution):
    """Normalized joint distribution over two discrete axes; sampling yields a pair."""

    def __init__(self, grid: dict[str, dict[str, float]], axes: Sequence[str] = _DEFAULT_AXES):
        # ``grid`` is already-normalized {row: {col: prob}} (from parse_trip_matrix).
        self.grid = grid
        self.axes = tuple(axes)

    # ---- constructors -----------------------------------------------------

    @classmethod
    def from_weights(cls, raw: Any, axes: Sequence[str] = _DEFAULT_AXES) -> "ProbabilityMatrix":
        """Author a matrix from a raw ``{row: {col: weight}}`` mapping.

        Diagonal forced to 0, negatives clamped, normalized to sum 1 (delegates to
        the strict ``trip_matrix.parse_trip_matrix``). Raises on no positive weight.
        """
        return cls(parse_trip_matrix(raw), axes)

    @classmethod
    def from_records(
        cls,
        records: Sequence[dict],
        axes: Sequence[str] = _DEFAULT_AXES,
    ) -> "ProbabilityMatrix":
        """Learn the empirical joint from historical records.

        Each record contributes 1 to cell ``(record[axes[0]], record[axes[1]])``.
        The counts are then parsed/normalized exactly like an authored matrix, so a
        learned matrix and a hand-authored one are byte-identical downstream.
        """
        a, b = axes[0], axes[1]
        counts: dict[str, dict[str, float]] = {}
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            row = str(rec.get(a, "")).strip().upper()
            col = str(rec.get(b, "")).strip().upper()
            if not row or not col:
                continue
            counts.setdefault(row, {}).setdefault(col, 0.0)
            counts[row][col] += 1.0
        return cls.from_weights(counts, axes)

    # ---- ops --------------------------------------------------------------

    def restrict(self, allowed_codes) -> "ProbabilityMatrix":
        """Drop rows/cols outside ``allowed_codes`` and renormalize."""
        return ProbabilityMatrix(restrict_trip_matrix(self.grid, allowed_codes), self.axes)

    def conditional(self, given_row: str) -> Categorical:
        """The distribution over the second axis given a fixed first-axis value."""
        row = self.grid.get(str(given_row).strip().upper()) or {}
        return Categorical(dict(row))

    def sample(self, rng: random.Random) -> tuple[str, str]:
        """Sample a ``(row, col)`` pair proportional to the joint weights."""
        return sample_pickup_delivery_codes(rng=rng, matrix=self.grid)

    def as_grid(self) -> dict[str, dict[str, float]]:
        """The normalized nested mapping (for persistence / the recipe)."""
        return self.grid
