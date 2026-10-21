"""The only two places a ``DispatchContext`` is built: one per partition, one per company.

- :func:`partition_context` -- the partitioned topology (``AssignmentApp.assign``):
  spatial candidates from ``spatial_pairs_for_partition``; sampling draws from the
  ``random`` MODULE.
- :func:`company_context` -- the pooled topology (``pooled_planner._solve_company``):
  spatial candidates are the round's prebuilt slice; sampling draws from the tick
  ``Random``.

Both RNG choices reproduce exactly what the inline code drew from, which the dispatch
golden test locks (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P1.5, §1.2 item 3).

Framework side of the boundary. Not a leaf module (plan §2.7).
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable, Iterable

from .allowed import CompanyRule, PartitionRule
from .context import DispatchClock, DispatchContext, Scope
from .helpers import candidates as _candidates
from .helpers.candidates import CandidateSettings

logger = logging.getLogger(__name__)

__all__ = ["partition_context", "company_context", "partition_scope_key", "company_scope_key"]


def partition_scope_key(partition_key: tuple[str, Any]) -> str:
    """``("haulier", "h1")`` -> ``partition:haulier:h1``; ``("component", 2)`` -> ``partition:component:2``."""
    kind, value = partition_key
    return f"partition:{kind}:{value}"


def company_scope_key(haulier_id: str) -> str:
    """``"h1"`` -> ``company:h1``."""
    return f"company:{haulier_id}"


def partition_context(
    *,
    policy: Any,
    params: Any,
    candidate_kind: str,
    trucks: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    partition_key: tuple[str, Any],
    rule: PartitionRule,
    settings: CandidateSettings,
    accounting_cost: Callable[[dict[str, Any], dict[str, Any]], float],
    clock: DispatchClock,
) -> DispatchContext:
    """Context for one partitioned-topology bucket (a haulier or a cooperation component)."""
    bucket_trucks = list(trucks)
    bucket_orders = list(orders)

    def _spatial() -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return _candidates.spatial_pairs_for_partition(
            bucket_trucks, bucket_orders, rule=rule, settings=settings
        )

    return DispatchContext(
        scope=Scope(kind="partition", key=partition_scope_key(partition_key)),
        params=params,
        clock=clock,
        rng=policy.rng,
        tiebreak_seed=policy.tiebreak_seed,
        rebates=policy.rebate_book,
        candidate_settings=settings,
        default_candidate_kind=candidate_kind,
        raw_trucks=tuple(bucket_trucks),
        raw_orders=tuple(bucket_orders),
        allowed_raw=rule.allowed,
        accounting_cost=accounting_cost,
        spatial_source=_spatial,
        # The random MODULE on purpose: the inline fallback called random.sample, and
        # the golden test locks module-RNG consumption (plan §4 P1.5, §7 risk 1).
        sample_rng=random,
        rule=rule,
    )


def company_context(
    *,
    policy: Any,
    params: Any,
    candidate_kind: str,
    trucks: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    haulier_id: str,
    round: int | None,
    rule: CompanyRule,
    rng: Any,
    settings: CandidateSettings,
    accounting_cost: Callable[[dict[str, Any], dict[str, Any]], float],
    clock: DispatchClock,
    prebuilt_pairs: Iterable[tuple[dict[str, Any], dict[str, Any]]] | None = None,
) -> DispatchContext:
    """Context for one company in one pooled bidding round.

    ``rng`` is the tick-seeded ``Random`` (``PooledPlannerContext.rng``) that the
    inline fallback sampled from. ``prebuilt_pairs`` is this company's slice of
    ``build_round_candidates``; the caller keeps today's early returns
    (empty pairs / no trucks / no orders) ahead of building this context.
    """
    pairs = list(prebuilt_pairs or ())
    return DispatchContext(
        scope=Scope(kind="company", key=company_scope_key(haulier_id), round=round),
        params=params,
        clock=clock,
        rng=policy.rng,
        tiebreak_seed=policy.tiebreak_seed,
        rebates=policy.rebate_book,
        candidate_settings=settings,
        default_candidate_kind=candidate_kind,
        raw_trucks=tuple(trucks),
        raw_orders=tuple(orders),
        allowed_raw=rule.allowed,
        accounting_cost=accounting_cost,
        spatial_source=lambda: pairs,
        sample_rng=rng,
        rule=rule,
    )
