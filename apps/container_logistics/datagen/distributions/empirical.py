"""EmpiricalData — raw historical records as a Distribution.

The base of the "historical" family. It can replay/resample records directly, or
be **extended into** a :class:`~.matrix.ProbabilityMatrix` (``to_probability_matrix``)
or a :class:`~.categorical.Categorical` (``to_categorical``) — so a hand-authored
matrix and a data-learned matrix are the same type produced two ways.

Records are loaded from a file named in ``spec.json`` (see ``sources.py``); no real
dataset ships yet, so tests use a small synthetic fixture.
"""

from __future__ import annotations

import random
from typing import Sequence

from .base import Distribution
from .categorical import Categorical
from .matrix import ProbabilityMatrix


class EmpiricalData(Distribution):
    def __init__(self, records: Sequence[dict], mode: str = "resample"):
        self.records = list(records or [])
        if mode not in ("replay", "resample"):
            raise ValueError(f"EmpiricalData mode must be 'replay' | 'resample', got {mode!r}")
        self.mode = mode
        self._cursor = 0

    # ---- extend into a fitted distribution --------------------------------

    def to_probability_matrix(self, axes=("pickup_code", "dropoff_code")) -> ProbabilityMatrix:
        """Fit an empirical joint over ``axes`` from the records."""
        return ProbabilityMatrix.from_records(self.records, axes)

    def to_categorical(self, axis: str) -> Categorical:
        counts: dict[str, float] = {}
        for rec in self.records:
            if isinstance(rec, dict):
                key = str(rec.get(axis, "")).strip()
                if key:
                    counts[key] = counts.get(key, 0.0) + 1.0
        return Categorical(counts)

    # ---- direct sampling --------------------------------------------------

    def sample(self, rng: random.Random) -> dict:
        if not self.records:
            raise ValueError("EmpiricalData has no records to sample")
        if self.mode == "resample":
            return rng.choice(self.records)
        rec = self.records[self._cursor % len(self.records)]
        self._cursor += 1
        return rec

    def sample_n(self, n: int, rng: random.Random) -> list:
        n = max(0, int(n))
        if self.mode == "replay" and self.records:
            return [self.records[i % len(self.records)] for i in range(n)]
        return super().sample_n(n, rng)
