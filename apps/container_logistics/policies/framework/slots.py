"""The slot table: every decision point a policy can fill, and its lifecycle status.

A slot is ``implemented`` (registry-backed, fail-loud) or ``planned`` (declared so the
catalog and error messages can name the phase it lands in). Plan
``docs/plans/policy_slots_p0_p3.md`` §4 P0.1; round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §3.1: five slots. The pooled market's
offer/claim/arbitration are not slots but components of the ``PooledMarket`` dispatch
policy (``framework/components.py``).

Leaf module: stdlib only (plan §2.7). ``SLOTS`` is immutable and in table order;
that order is the catalog's order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from .errors import UnknownSlotError

logger = logging.getLogger(__name__)

__all__ = ["SlotSpec", "SLOTS", "slot_spec"]


@dataclass(frozen=True)
class SlotSpec:
    """One slot's declaration.

    ``timings`` are the timings a config may name; ``executable_timings`` the subset
    the runner can execute today. ``lands_in`` names the phase for planned slots.

    Raises ``ValueError`` at construction for an internally inconsistent declaration.
    """

    name: str
    kind: Literal["operator", "world"]
    status: Literal["implemented", "planned"]
    default: str | None
    description: str
    timings: tuple[str, ...] = ()
    executable_timings: tuple[str, ...] = ()
    lands_in: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("operator", "world"):
            raise ValueError(f"slot {self.name!r}: kind {self.kind!r} is not 'operator' or 'world'")
        if self.status not in ("implemented", "planned"):
            raise ValueError(f"slot {self.name!r}: unknown status {self.status!r}")
        if self.status == "implemented" and not self.default:
            raise ValueError(f"slot {self.name!r}: an implemented slot needs a default policy")
        if self.status == "planned" and not self.lands_in:
            raise ValueError(f"slot {self.name!r}: a planned slot must name the phase it lands in")
        extra = set(self.executable_timings) - set(self.timings)
        if extra:
            raise ValueError(
                f"slot {self.name!r}: executable_timings {sorted(extra)} are not declared timings"
            )


_TABLE = (
    SlotSpec(
        name="dispatch",
        kind="operator",
        status="implemented",
        # The whole-tick default: per-cooperation-group decomposition whose inner
        # defaults to RandomAssignment (the pre-v2 no-strategy behaviour). The flat
        # legacy leaf default is policies/dispatch/legacy.LEGACY_LEAF_DEFAULT.
        default="Partitioned",
        description="Which free truck serves which unassigned order, decided for the whole tick "
                    "(the decomposition, e.g. Partitioned or PooledMarket, is part of the selection).",
        timings=("online", "plan"),
        executable_timings=("online",),
    ),
    SlotSpec(
        name="idle",
        kind="operator",
        status="planned",
        default=None,
        description="Where a truck with no work goes and waits.",
        lands_in="P5",
    ),
    SlotSpec(
        name="queue",
        kind="operator",
        status="planned",
        default=None,
        description="Which queued truck a facility gate serves next.",
        lands_in="P5",
    ),
    SlotSpec(
        name="world.shedding",
        kind="world",
        status="planned",
        default=None,
        description="When unserved demand leaves the market.",
        lands_in="P5",
    ),
    SlotSpec(
        name="world.duration",
        kind="world",
        status="planned",
        default=None,
        description="How long a haul leg or a facility service takes.",
        lands_in="P5",
    ),
)

SLOTS: Mapping[str, SlotSpec] = MappingProxyType({s.name: s for s in _TABLE})


def slot_spec(name: str) -> SlotSpec:
    """Return the declaration of slot ``name``.

    Raises :class:`UnknownSlotError` naming the value and every known slot.
    """
    spec = SLOTS.get(name) if isinstance(name, str) else None
    if spec is None:
        raise UnknownSlotError(
            f"Unknown policy slot {name!r}. Known slots: {', '.join(SLOTS)}."
        )
    return spec
