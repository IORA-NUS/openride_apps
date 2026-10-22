"""``ResolvedDispatch``: the outcome of resolving an assignment profile's dispatch slot.

A value object only: one NODE of the resolved selection tree (round 2,
``docs/plans/policy_slots_round2_whole_tick.md`` §3.5, §4). A composite node (e.g.
``Partitioned``) carries its ``inner`` node; a leaf has ``inner=None``.
``dispatch/profile.py`` produces it (``resolve_dispatch_for_profile``,
``resolve_for_instance``) and ``dispatch/runner.py`` consumes it; it lives in its own
module so neither has to import the other (plan ``docs/plans/policy_slots_p0_p3.md``
§4 P1.6).

Leaf module (plan §2.7): never imports ``apps.container_logistics.assignment.*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from apps.container_logistics.policies.framework.params import PolicyParams

__all__ = ["ResolvedDispatch", "ResolvedSource"]

ResolvedSource = Literal["policies", "legacy-profile", "unregistered"]


@dataclass(frozen=True)
class ResolvedDispatch:
    """Which dispatch policy runs, with what params and timing.

    ``params`` is ``None`` only for ``source="unregistered"`` (a hand-injected
    solver class with no ``@policy`` declaration). The candidate kind is not here: a
    solver reads its own params, and a caller reads a child's
    ``Declaration.candidate_hint`` (``dispatch/bind.py``).

    Tree fields (round 2): ``label`` is this node's name plus its ``inner`` chain, slash-joined
    (``"PooledMarket/GreedyNearest"``); ``leaf`` the innermost name; ``inner`` the
    resolved ``inner`` ref of a composite. The label and leaf default to ``name``.
    """

    name: str
    version: str
    timing: str
    params: PolicyParams | None
    source: ResolvedSource
    label: str = ""
    leaf: str = ""
    inner: "ResolvedDispatch | None" = None

    def __post_init__(self) -> None:
        # Derived, never restated: a hand-built node (tests, the battery) gets the
        # same label/leaf the resolver computes.
        if not self.leaf:
            object.__setattr__(self, "leaf", self.inner.leaf if self.inner is not None else self.name)
        if not self.label:
            chain = f"{self.name}/{self.inner.label}" if self.inner is not None else self.name
            object.__setattr__(self, "label", chain)

    @property
    def innermost(self) -> "ResolvedDispatch":
        """The leaf node of the ``inner`` chain (``self`` for a leaf)."""
        node = self
        while node.inner is not None:
            node = node.inner
        return node
