"""The slot table: every decision point a policy can fill, and its lifecycle status.

A slot is ``implemented`` (registry-backed, fail-loud), ``legacy`` (configured
elsewhere, listed read-only in the catalog, fail-soft by doc 21 §7) or ``planned``
(declared so the catalog and error messages can name the phase it lands in).
Plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.1 and §1.2 item 10.

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
    the runner can execute today. ``lands_in`` names the phase for planned slots;
    ``configured_via`` names the legacy config key for legacy slots.

    Raises ``ValueError`` at construction for an internally inconsistent declaration.
    """

    name: str
    kind: Literal["operator", "world"]
    status: Literal["implemented", "legacy", "planned"]
    default: str | None
    description: str
    timings: tuple[str, ...] = ()
    executable_timings: tuple[str, ...] = ()
    lands_in: str | None = None
    configured_via: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("operator", "world"):
            raise ValueError(f"slot {self.name!r}: kind {self.kind!r} is not 'operator' or 'world'")
        if self.status not in ("implemented", "legacy", "planned"):
            raise ValueError(f"slot {self.name!r}: unknown status {self.status!r}")
        if self.status != "planned" and not self.default:
            raise ValueError(f"slot {self.name!r}: a {self.status} slot needs a default policy")
        if self.status == "planned" and not self.lands_in:
            raise ValueError(f"slot {self.name!r}: a planned slot must name the phase it lands in")
        if self.status == "legacy" and not self.configured_via:
            raise ValueError(f"slot {self.name!r}: a legacy slot must name its configured_via key")
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
        default="RandomAssignment",
        description="Which free truck serves which unassigned order, per scope per tick.",
        timings=("online", "plan"),
        executable_timings=("online",),
    ),
    SlotSpec(
        name="market.offer",
        kind="operator",
        status="legacy",
        default="OfferAll",
        description="Which of its own orders a company offers to the shared pool.",
        configured_via="planner.market.offer",
    ),
    SlotSpec(
        name="market.claim",
        kind="operator",
        status="legacy",
        default="ClaimAllPlanned",
        description="Which planned pool pairs a company bids for.",
        configured_via="planner.market.claim",
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
