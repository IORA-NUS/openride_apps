"""The only three places a ``DispatchContext`` is built: the tick, a group, a company.

- :func:`tick_context` -- the WHOLE tick (``DispatchRunner.tick_context``): every
  candidate truck and order, ``world.allowed`` as feasibility (``world.tick_allowed``), spatial candidates from
  ``spatial_pairs_for_partition`` over the whole tick, ``ctx.rng`` the lazily-built
  ``random.Random(tick_seed)``, ``tiebreak_seed = tick_seed``.
- :func:`partition_context` -- one cooperation group (``Partitioned``, via
  ``compose``): spatial candidates from ``spatial_pairs_for_partition``; sampling draws
  from the caller's ``sample_rng`` (a required keyword: ``Partitioned`` passes the
  ``random`` MODULE for byte identity, any other composite its decision RNG, e.g.
  ``ctx.rng``).
- :func:`company_context` -- one pooled-market company in one round (``PooledMarket``,
  via ``compose.ScopeDecider``): spatial candidates are the round's prebuilt slice;
  sampling draws from the tick ``Random``.

A sub-scope takes a ``parent`` context and inherits its ``world``, ``tick_seed``,
``execute``, ``stats``, ``scope_log``, ``capture`` and ``verified_heads`` (round-2 §2.3); its RNG,
tiebreak and rebate book are the values its builder passes (the inner policy's). The
partition and company RNG choices reproduce exactly what the pre-v2 inline code drew
from, which the dispatch golden test locks (plan ``docs/plans/policy_slots_p0_p3.md``
§4 P1.5, §1.2 item 3). Building a context draws no RNG.

Framework side of the boundary. Not a leaf module (plan §2.7).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from .allowed import CompanyRule, PartitionRule
from .context import TICK_SCOPE_KEY, DispatchClock, DispatchContext, Scope, direct_execute
from .helpers import candidates as _candidates
from .helpers.candidates import CandidateSettings

logger = logging.getLogger(__name__)

__all__ = [
    "tick_context",
    "partition_context",
    "company_context",
    "partition_scope_key",
    "company_scope_key",
]

Doc = dict[str, Any]


def partition_scope_key(partition_key: tuple[str, Any]) -> str:
    """``("haulier", "h1")`` -> ``partition:haulier:h1``; ``("component", 2)`` -> ``partition:component:2``."""
    kind, value = partition_key
    return f"partition:{kind}:{value}"


def company_scope_key(haulier_id: str) -> str:
    """``"h1"`` -> ``company:h1``."""
    return f"company:{haulier_id}"


def _inherited(parent: DispatchContext | None) -> dict[str, Any]:
    """What a sub-scope takes from its parent (defaults for an offline, parent-less scope)."""
    if parent is None:
        return {"world": None, "tick_seed": None, "execute": direct_execute,
                "stats": None, "scope_log": None, "capture": None, "verified_heads": None}
    return {"world": parent.world, "tick_seed": parent.tick_seed, "execute": parent.execute,
            "stats": parent.stats, "scope_log": parent.scope_log, "capture": parent.capture,
            "verified_heads": parent.verified_heads}


def _log_scope(ctx: DispatchContext) -> DispatchContext:
    """Record a built sub-scope on the audit log (only when auditing: ``scope_log`` set)."""
    if ctx.scope_log is not None:
        ctx.scope_log.append({
            "kind": ctx.scope.kind,
            "key": ctx.scope.key,
            "round": ctx.scope.round,
            "trucks": [str(t.get("_id")) for t in ctx.raw_trucks],
            "orders": [str(o.get("_id")) for o in ctx.raw_orders],
        })
    return ctx


def tick_context(
    *,
    policy: Any,
    params: Any,
    candidate_kind: str,
    trucks: Iterable[Doc],
    orders: Iterable[Doc],
    world: Any,
    settings: CandidateSettings,
    clock: DispatchClock,
    tick_seed: int,
    execute: Callable[..., Any] = direct_execute,
    stats: dict[str, Any] | None = None,
    scope_log: list[dict[str, Any]] | None = None,
    capture: Any = None,
) -> DispatchContext:
    """The whole-tick context for the selected (top-level) ``policy``.

    ``world`` is the tick's ``DispatchWorld``. ``ctx.rng`` is NOT built here (it is
    ``Random(tick_seed)`` on first read), so building the tick draws and constructs
    nothing. ``rebates`` is ``policy.rebate_book`` (a composite forwards it to its
    inner). Feasibility is ``world.tick_allowed`` over a fresh ``verified_heads`` set
    (equal to ``world.allowed``; heads a sub-scope already proved physically feasible
    only re-check the structure half).
    """
    all_trucks = list(trucks)
    all_orders = list(orders)
    verified_heads: set[tuple[int, int]] = set()

    def _spatial() -> list[tuple[Doc, Doc]]:
        return _candidates.spatial_pairs_for_partition(all_trucks, all_orders, rule=world.rule, settings=settings)

    return DispatchContext(
        scope=Scope(kind="tick", key=TICK_SCOPE_KEY),
        params=params,
        clock=clock,
        rng=None,
        tiebreak_seed=tick_seed,
        rebates=policy.rebate_book,
        candidate_settings=settings,
        default_candidate_kind=candidate_kind,
        raw_trucks=tuple(all_trucks),
        raw_orders=tuple(all_orders),
        allowed_raw=world.tick_allowed(verified_heads),
        accounting_cost=world.accounting_cost,
        spatial_source=_spatial,
        sample_rng=None,
        rule=world.rule,
        world=world,
        tick_seed=tick_seed,
        execute=execute,
        stats=stats,
        scope_log=scope_log,
        capture=capture,
        verified_heads=verified_heads,
    )


def partition_context(
    *,
    policy: Any,
    params: Any,
    candidate_kind: str,
    trucks: list[Doc],
    orders: list[Doc],
    partition_key: tuple[str, Any],
    rule: PartitionRule,
    settings: CandidateSettings,
    accounting_cost: Callable[[Doc, Doc], float],
    clock: DispatchClock,
    sample_rng: Any,
    parent: DispatchContext | None = None,
) -> DispatchContext:
    """Context for one cooperation group (a haulier or a cooperation component).

    ``policy`` is the solver that decides the group: its ``rng``, ``tiebreak_seed``
    and ``rebate_book`` become the context's (on the partitioned default the
    ``random`` module, ``None``, ``None``).

    ``sample_rng`` is what ``ctx.sampled_trucks()`` draws its cap sample from. It is
    REQUIRED, with no default, so the choice is visible at every call site (final
    review MAJOR-4): ``Partitioned`` passes the ``random`` MODULE (the pre-v2 inline
    fallback called ``random.sample``, and the dispatch golden locks module-RNG
    consumption); a composite that wants to pass the battery's
    ``determinism.no_module_rng`` without declaring ``module_rng`` passes its decision
    RNG (``ctx.rng``, the tick ``Random``), injects the same into its inner
    (``inner.set_rng(ctx.rng)``) and the tick's tiebreak seed with it
    (``inner.set_tiebreak(ctx.tick_seed)``; without it ``GreedyNearest`` falls back to
    a ``self.rng.shuffle`` tiebreak and the composite fails
    ``determinism.order_independent``, the way ``PooledMarket`` injects both). ``None`` means "draw from this context's ``rng``"
    (``policy.rng``), as on the tick scope.
    """
    bucket_trucks = list(trucks)
    bucket_orders = list(orders)

    def _spatial() -> list[tuple[Doc, Doc]]:
        return _candidates.spatial_pairs_for_partition(
            bucket_trucks, bucket_orders, rule=rule, settings=settings
        )

    return _log_scope(DispatchContext(
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
        sample_rng=sample_rng,
        rule=rule,
        **_inherited(parent),
    ))


def company_context(
    *,
    policy: Any,
    params: Any,
    candidate_kind: str,
    trucks: list[Doc],
    orders: list[Doc],
    haulier_id: str,
    round: int | None,
    rule: CompanyRule,
    rng: Any,
    settings: CandidateSettings,
    accounting_cost: Callable[[Doc, Doc], float],
    clock: DispatchClock,
    prebuilt_pairs: Iterable[tuple[Doc, Doc]] | None = None,
    parent: DispatchContext | None = None,
) -> DispatchContext:
    """Context for one company in one pooled bidding round.

    ``rng`` is the tick-seeded ``Random`` the inline fallback sampled from (the
    market's ``ctx.rng``). ``prebuilt_pairs`` is this company's slice of
    ``build_round_candidates``; the caller keeps today's early returns (empty pairs /
    no trucks / no orders) ahead of building this context.
    """
    pairs = list(prebuilt_pairs or ())
    return _log_scope(DispatchContext(
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
        **_inherited(parent),
    ))
