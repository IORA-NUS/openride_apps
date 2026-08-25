"""Uniform and weighted-categorical distributions over a discrete support."""

from __future__ import annotations

import random
from typing import Any, Sequence

from .base import Distribution


class Uniform(Distribution):
    """Uniform choice over a fixed list of items."""

    def __init__(self, items: Sequence[Any]):
        self.items = list(items)
        if not self.items:
            raise ValueError("Uniform distribution requires at least one item")

    def sample(self, rng: random.Random) -> Any:
        return rng.choice(self.items)


class Categorical(Distribution):
    """Weighted choice over items (a general weighted distribution).

    ``weights`` maps item -> non-negative weight; zero-weight items are dropped.
    The marginal/conditional slice of a :class:`~.matrix.ProbabilityMatrix` is a
    ``Categorical``.
    """

    def __init__(self, weights: dict[Any, float]):
        items, w = [], []
        for item, weight in (weights or {}).items():
            try:
                fw = float(weight)
            except (TypeError, ValueError):
                continue
            if fw > 0:
                items.append(item)
                w.append(fw)
        if not items:
            raise ValueError("Categorical requires at least one positive weight")
        self.items = items
        self.weights = w

    def sample(self, rng: random.Random) -> Any:
        return rng.choices(self.items, weights=self.weights, k=1)[0]
