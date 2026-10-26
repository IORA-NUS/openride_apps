"""``CompositePolicy``: an OPTIONAL helper base for a solver that delegates to an ``inner`` one.

Single responsibility: the shared shape of a decomposition solver (``Partitioned``,
``PooledMarket``; plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2.3, §4). It
holds the built ``inner`` policy and the ``inner_node`` (the inner's
``ResolvedDispatch``) and forwards the opt-in injections (``set_rng``,
``set_tiebreak``, ``set_rebate_book``) to the inner, so a caller that injects into the
top reaches the per-scope solver that actually reads them.

A node with an ``inner`` ref is built by ``dispatch/profile.build_policy_tree`` as
``cls(params=<dict>, inner=<policy>, inner_node=<ResolvedDispatch>)`` (the construction
protocol); any class accepting those keywords works, this base is a convenience. The
framework never ``isinstance``-checks it: it walks its own bound tree
(``dispatch.bind.BoundTree``) and delegates through ``tick.call``.

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

__all__ = ["CompositePolicy"]


class CompositePolicy(DispatchPolicy):
    """A dispatch policy with an ``inner`` per-scope solver it delegates each scope to.

    ``inner`` is a built leaf policy (any registered class: the ``DispatchPolicy`` base
    is not required of it, WS8 litmus (b)); ``inner_node`` its resolved selection
    (name, params) for the runner and the stamp. The three injections are
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
        inner: Any,
        inner_node: ResolvedDispatch,
    ):
        super().__init__(params)
        self.inner = inner
        self.inner_node = inner_node

    def _forward(self, method: str, value: Any) -> None:
        """Call ``inner.<method>(value)`` when the inner offers that injection.

        The inner is duck-typed (plan ``plan_contract_final.md`` §2.1/§2.2: the
        ``DispatchPolicy`` base is optional, a registered class only needs
        ``solve_tick``), so an inner without the injection API simply does not receive
        it; ``tick.call`` then reads its ``rng`` / ``tiebreak_seed`` / ``rebate_book``
        with the documented defaults. Every built-in inner has the API, so this is
        byte-identical for them.
        """
        forward = getattr(self.inner, method, None)
        if callable(forward):
            forward(value)

    def set_rng(self, rng: Optional[_random_module.Random]) -> None:
        super().set_rng(rng)
        self._forward("set_rng", rng)

    def set_tiebreak(self, tick_seed: Optional[int]) -> None:
        super().set_tiebreak(tick_seed)
        self._forward("set_tiebreak", tick_seed)

    def set_rebate_book(self, book: Optional[RebateBook]) -> None:
        super().set_rebate_book(book)
        self._forward("set_rebate_book", book)

