"""The generalized, object-oriented sampler hierarchy.

A :class:`Distribution` answers one tiny contract — ``sample(rng)`` — so every
sampler (uniform, categorical, probability matrix, empirical/historical, curve)
is interchangeable. Policies *compose* distributions; the "trip matrix" is not a
special case, it is a :class:`~.matrix.ProbabilityMatrix` over ``(pickup_code,
dropoff_code)``.

Pure: distributions import nothing from the scenario/runtime layers and hold no
module globals. File parsing (``.csv``/``.xlsx``/``.json``) lives in
``sources.py`` and uses stdlib ``csv`` + ``openpyxl`` only.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Any


class Distribution(ABC):
    """Base class for all samplers. Subclasses implement :meth:`sample`."""

    @abstractmethod
    def sample(self, rng: random.Random) -> Any:
        """Return one draw using the supplied RNG."""

    def sample_n(self, n: int, rng: random.Random) -> list:
        """Return ``n`` independent draws (override for bulk/replay semantics)."""
        return [self.sample(rng) for _ in range(max(0, int(n)))]
