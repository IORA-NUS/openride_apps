"""``CompositePolicy``: a dispatch policy that decomposes the tick and delegates to an inner one.

Single responsibility: the shared shape of a decomposition solver (``Partitioned``,
``PooledMarket``; plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.3, §4). It
holds the built ``inner`` policy and the ``inner_node`` (the inner's
``ResolvedDispatch``) and forwards the opt-in injections (``set_rng``,
``set_tiebreak``, ``set_rebate_book``) to the inner, so a caller that injects into the
top reaches the per-scope solver that actually reads them. :func:`innermost_policy`
walks a built tree down to its leaf.

A composite is built by ``dispatch/profile.build_policy_tree`` as
``cls(params=<dict>, inner=<policy>, inner_node=<ResolvedDispatch>)``; its own
``decide(ctx)`` stays abstract.

Leaf module (plan §2.7, round-2 §4): imports only ``dispatch/base.py``,
``dispatch/resolved.py`` and the stdlib-only ``apps.container_logistics.rebate`` (for
the type of the forwarded book); never ``apps.container_logistics.assignment.*``.
Policy side of the boundary.
"""

from __future__ import annotations

import logging
import random as _random_module
from typing import Any, Dict, Optional

from apps.container_logistics.rebate import RebateBook

from .base import DispatchPolicy
from .resolved import ResolvedDispatch

logger = logging.getLogger(__name__)

__all__ = ["CompositePolicy", "innermost_policy"]


class CompositePolicy(DispatchPolicy):
    """A dispatch policy with an ``inner`` per-scope solver it delegates each scope to.

    ``inner`` is a built leaf policy; ``inner_node`` its resolved selection (name,
    params, candidate kind) for the runner and the stamp. The three injections are
    recorded here (so ``self.rng`` etc. agree) AND forwarded to ``inner`` by default.
    A composite whose scopes must keep the inner's OWN randomness (``Partitioned``:
    the pre-v2 partitioned path never injected one, its groups draw from the
    ``random`` module) overrides ``set_rng``/``set_tiebreak`` not to forward; one
    that wants per-scope RNGs builds them itself.
    """

    def __init__(
        self,
        params: Optional[Dict[str, Any]] = None,
        *,
        inner: DispatchPolicy,
        inner_node: ResolvedDispatch,
    ):
        super().__init__(params)
        if not isinstance(inner, DispatchPolicy):
            raise TypeError(
                f"{type(self).__name__}: inner must be a DispatchPolicy (got {type(inner).__name__})."
            )
        self.inner = inner
        self.inner_node = inner_node

    def set_rng(self, rng: Optional[_random_module.Random]) -> None:
        super().set_rng(rng)
        self.inner.set_rng(rng)

    def set_tiebreak(self, tick_seed: Optional[int]) -> None:
        super().set_tiebreak(tick_seed)
        self.inner.set_tiebreak(tick_seed)

    def set_rebate_book(self, book: Optional[RebateBook]) -> None:
        super().set_rebate_book(book)
        self.inner.set_rebate_book(book)


def innermost_policy(policy: DispatchPolicy) -> DispatchPolicy:
    """The leaf of a built policy tree: follow ``inner`` until a non-composite."""
    while isinstance(policy, CompositePolicy):
        policy = policy.inner
    return policy
