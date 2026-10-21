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

__all__ = ["KIND_WITHOUT_CANDIDATES_PARAM", "ResolvedDispatch", "ResolvedSource", "candidate_kind_for"]

ResolvedSource = Literal["policies", "legacy-profile", "unregistered"]

#: The candidate kind of a policy that declares no ``candidates`` param: it gets no
#: prebuilt spatial pairs. The ONE copy of this rule (review F14).
KIND_WITHOUT_CANDIDATES_PARAM = "sampled"


def candidate_kind_for(params: PolicyParams) -> str:
    """``params.candidates`` when the policy declares it, else ``KIND_WITHOUT_CANDIDATES_PARAM``."""
    return getattr(params, "candidates", KIND_WITHOUT_CANDIDATES_PARAM)


@dataclass(frozen=True)
class ResolvedDispatch:
    """Which dispatch policy runs, with what params, timing and candidate kind.

    ``params`` is ``None`` only for ``source="unregistered"`` (a hand-injected
    solver class with no ``@policy`` declaration). ``candidate_kind`` is one of
    ``helpers.candidates.CANDIDATE_KINDS``.

    Tree fields (round 2): ``information_model`` and ``share_tag_format`` are the
    policy's registration (``None``/``"audited"`` for an unregistered class);
    ``label`` is this node's name plus its ``inner`` chain, slash-joined
    (``"PooledMarket/GreedyNearest"``); ``leaf`` the innermost name; ``inner`` the
    resolved ``inner`` ref of a composite. The label and leaf default to ``name``.
    """

    name: str
    version: str
    timing: str
    params: PolicyParams | None
    candidate_kind: str
    source: ResolvedSource
    information_model: str | None = None
    share_tag_format: str = "audited"
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
