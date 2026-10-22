"""The slot table: every decision point a policy can fill.

Every slot is registry-backed and fail-loud; adding one is one ``SlotSpec`` entry in
``_TABLE``. Plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.1; round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §3.1 (Deviations: five slots became
one -- idle trucks, the facility queue, unserved-demand exit and durations are the
simulation's own state machines and world model, not slots). The pooled market's
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

__all__ = ["SlotSpec", "SLOTS", "RENAMED_SLOTS", "slot_spec", "unknown_slot_message"]


@dataclass(frozen=True)
class SlotSpec:
    """One slot's declaration.

    ``timings`` are the timings a config may name; ``executable_timings`` the subset
    the runner can execute today. ``kind`` is echoed into every compiled profile entry
    (``ResolvedPolicy.to_profile``), so it is part of the compiled bundle's bytes.

    Raises ``ValueError`` at construction for an internally inconsistent declaration.
    """

    name: str
    kind: Literal["operator"]
    default: str
    description: str
    timings: tuple[str, ...] = ()
    executable_timings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind != "operator":
            raise ValueError(f"slot {self.name!r}: kind {self.kind!r} is not 'operator'")
        if not self.default:
            raise ValueError(f"slot {self.name!r}: a slot needs a default policy")
        extra = set(self.executable_timings) - set(self.timings)
        if extra:
            raise ValueError(
                f"slot {self.name!r}: executable_timings {sorted(extra)} are not declared timings"
            )


_TABLE = (
    SlotSpec(
        name="planner",
        kind="operator",
        # The whole-tick default: per-cooperation-group decomposition whose inner
        # defaults to RandomAssignment (the pre-v2 no-strategy behaviour). The flat
        # legacy leaf default is policies/dispatch/legacy.LEGACY_LEAF_DEFAULT.
        default="Partitioned",
        description="Which free truck serves which unassigned order, decided for the whole tick "
                    "(the decomposition, e.g. Partitioned or PooledMarket, is part of the selection).",
        timings=("online", "plan"),
        executable_timings=("online",),
    ),
)

SLOTS: Mapping[str, SlotSpec] = MappingProxyType({s.name: s for s in _TABLE})

#: Former slot names -> current ones. NOT aliases: a renamed name is still refused;
#: the refusal just says what it became (``dispatch`` was renamed 2026-10-22).
RENAMED_SLOTS: Mapping[str, str] = MappingProxyType({"dispatch": "planner"})


def unknown_slot_message(name: object, where: str | None = None) -> str:
    """The one unknown-slot refusal: names the value, every known slot, and a rename hint."""
    loc = f" in {where!r}" if where else ""
    msg = f"Unknown policy slot {name!r}{loc}. Known slots: {', '.join(SLOTS)}."
    renamed = RENAMED_SLOTS.get(name) if isinstance(name, str) else None
    if renamed:
        msg += f" (The slot was renamed to {renamed!r}.)"
    return msg


def slot_spec(name: str) -> SlotSpec:
    """Return the declaration of slot ``name``.

    Raises :class:`UnknownSlotError` naming the value and every known slot.
    """
    spec = SLOTS.get(name) if isinstance(name, str) else None
    if spec is None:
        raise UnknownSlotError(unknown_slot_message(name))
    return spec
