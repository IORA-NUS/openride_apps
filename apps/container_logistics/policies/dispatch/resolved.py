"""``ResolvedDispatch``: the outcome of resolving an assignment profile's dispatch slot.

A value object only. ``dispatch/profile.py`` produces it (``resolve_dispatch_for_profile``,
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
    """

    name: str
    version: str
    timing: str
    params: PolicyParams | None
    candidate_kind: str
    source: ResolvedSource
