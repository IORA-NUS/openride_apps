"""Distribution registry — name -> class, plus a ``build`` from a config dict.

Extensibility: register a new sampler once (``@register("my_dist")``) and it is
selectable from ``spec.json``. This is the developer extension seam (plan §7).
"""

from __future__ import annotations

from typing import Any, Callable

from .base import Distribution

DISTRIBUTION_REGISTRY: dict[str, type] = {}


def register(name: str) -> Callable[[type], type]:
    def _wrap(cls: type) -> type:
        DISTRIBUTION_REGISTRY[name] = cls
        return cls

    return _wrap


def known_distributions() -> list[str]:
    return sorted(DISTRIBUTION_REGISTRY)
