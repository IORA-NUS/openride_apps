"""RebateCurve — a ``Curve`` container holding *signed prices* rather than weights.

Lives here, beside the ``Curve`` it extends, rather than in
``apps/container_logistics/rebate.py`` where plan §4.2 placed it. Two reasons, both
structural: ``datagen/preprocess.py`` imports ``rebate.py`` for validation, so a
``rebate.py -> datagen`` edge would be a package cycle; and ``rebate.py`` is imported
by the long-lived Celery analytics agent and by ``solver/base.py``, where dragging the
datagen catalog (shapely, the address book) behind a pure pricing function is a cost
with no payoff. The pricing rule itself is unaffected — it lives in ``rebate.py`` and
this class delegates to it.

``Curve`` is safe to reuse as a *container*: ``Curve.__init__`` stores ``bin_weights``
verbatim and does not normalize. Every *path into* it does not — ``demand``'s
``max(0.0, weight)`` and ``normalize_hourly_weights``' division both destroy a signed
price — which is why ``rebate.parse_rebate_schedule`` is a separate parser.
"""

from __future__ import annotations

import random
from typing import Sequence

from ...rebate import HOURS_PER_DAY, RebateSchedule, price_at
from .curve import Curve


class RebateCurve(Curve):
    """A 24-slot signed price curve. Sampling it is meaningless and is refused."""

    def __init__(self, schedule: RebateSchedule):
        super().__init__(list(schedule.amounts))
        self.schedule = schedule

    @classmethod
    def from_amounts(cls, amounts: Sequence[float], currency: str = "credit") -> "RebateCurve":
        return cls(RebateSchedule(amounts=tuple(float(a) for a in amounts), currency=currency))

    def price_at(self, when) -> float:
        """Delegate to the single pricing rule — never a second implementation."""
        return price_at(self.schedule, when)

    def sample(self, rng: random.Random) -> int:  # pragma: no cover - guard
        """Refused.

        ``Curve.sample`` walks a cumulative accumulator (``curve.py:91-93``), which is
        mathematically invalid under negative weights — the accumulator is no longer
        monotonic, so ``r <= acc`` picks an arbitrary bin — and it silently degrades to
        a uniform draw when the window sums to <= 0 (``curve.py:86``), which is exactly
        what a schedule of equal rebates and surcharges does. A rebate curve is a price
        lookup, never a distribution.
        """
        raise TypeError(
            "RebateCurve is a signed price schedule and must never be sampled: "
            "Curve.sample's cumulative walk is invalid under negative weights and "
            "degrades to uniform when the weights sum to <= 0. Use price_at(when) or "
            "value_at(hour)."
        )

    def sample_step(self, rng, **kwargs):  # pragma: no cover - guard
        raise TypeError("RebateCurve is a signed price schedule and must never be sampled.")


__all__ = ["RebateCurve", "HOURS_PER_DAY"]
