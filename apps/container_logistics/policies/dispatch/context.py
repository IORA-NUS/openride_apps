"""``DispatchContext``: everything one dispatch decision may read, for one scope.

A context is built only by ``dispatch/scopes.py`` (``partition_context`` /
``company_context``) and handed to ``policy.decide(ctx)``. It carries the raw
documents, the framework's feasibility (``allowed_raw``) and accounting cost, the
injected RNG / tiebreak seed / rebate book, and lazily-computed candidates.

Laziness is load-bearing, not an optimisation: :meth:`DispatchContext.sampled_trucks`
consumes RNG, so it runs at most once per context and only when a policy asks.
Today's code sampled exactly once and only on the non-spatial branch; any extra or
earlier draw would shift every later draw in the worker (plan
``docs/plans/policy_slots_p0_p3.md`` §7 risk 1).

Not yet here (P4): ``ctx.travel`` (a run-scoped travel-time table),
``ctx.facilities``, ``ctx.state`` and ``held`` plan tails.

Framework side of the boundary. Not a leaf module (it reaches
``assignment.constraints`` through ``views`` and ``helpers``; plan §2.7).
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

from apps.container_logistics.policies.framework.errors import DispatchContractViolation

from .helpers import candidates as _candidates
from .helpers import objective as _objective
from .helpers.candidates import CandidateSettings
from .views import OrderView, TruckView, order_view, truck_view

logger = logging.getLogger(__name__)

__all__ = ["DispatchContext", "DispatchClock", "Scope"]

Pair = tuple[dict[str, Any], dict[str, Any]]


@dataclass(frozen=True)
class Scope:
    """Which slice of the tick a context covers.

    ``key`` is ``partition:haulier:<id>``, ``partition:component:<n>`` or
    ``company:<haulier_id>``; ``round`` is the pooled bidding round (``None`` on the
    partitioned path).
    """

    kind: Literal["partition", "company"]
    key: str
    round: int | None = None


@dataclass(frozen=True)
class DispatchClock:
    """The tick being decided: the run's RFC-1123 ``sim_clock`` string and step index."""

    sim_clock: str
    time_step: int
    horizon_steps: int | None = None


def _unwrap(entity: Any) -> dict[str, Any]:
    return entity.raw if isinstance(entity, (TruckView, OrderView)) else entity


class DispatchContext:
    """One scope's dispatch inputs. Plain class: ``functools.cached_property`` needs ``__dict__``.

    Policies read it and never mutate it. ``candidate_pairs`` and ``sampled_trucks``
    return fresh lists each call (the cached value is never handed out).
    """

    def __init__(
        self,
        *,
        scope: Scope,
        params: Any,
        clock: DispatchClock,
        rng: Any,
        tiebreak_seed: int | None,
        rebates: Any,
        candidate_settings: CandidateSettings,
        default_candidate_kind: str,
        raw_trucks: tuple[dict[str, Any], ...],
        raw_orders: tuple[dict[str, Any], ...],
        allowed_raw: Callable[[dict[str, Any], dict[str, Any]], bool],
        accounting_cost: Callable[[dict[str, Any], dict[str, Any]], float],
        spatial_source: Callable[[], list[Pair]],
        sample_rng: Any,
        rule: Any = None,
    ) -> None:
        """``spatial_source`` and ``sample_rng`` are supplied by ``scopes.py`` only.

        ``rng`` is ``policy.rng`` (the ``random`` module on the partitioned path, the
        tick ``Random`` on the pooled path); ``sample_rng`` is what
        :meth:`sampled_trucks` draws from, chosen per scope kind to match today.
        ``rule`` is the ``PartitionRule``/``CompanyRule`` behind ``allowed_raw``,
        kept for capture.
        """
        self.scope = scope
        self.params = params
        self.clock = clock
        self.rng = rng
        self.tiebreak_seed = tiebreak_seed
        self.rebates = rebates
        self.candidate_settings = candidate_settings
        self.default_candidate_kind = default_candidate_kind
        self.raw_trucks = tuple(raw_trucks)
        self.raw_orders = tuple(raw_orders)
        self.allowed_raw = allowed_raw
        self.accounting_cost = accounting_cost
        self.rule = rule
        self._spatial_source = spatial_source
        self._sample_rng = sample_rng
        self._pairs_cache: dict[str, list[Pair]] = {}
        self._sampled: list[dict[str, Any]] | None = None

    @functools.cached_property
    def trucks(self) -> tuple[TruckView, ...]:
        """Typed views of ``raw_trucks``, built on first access."""
        return tuple(truck_view(t) for t in self.raw_trucks)

    @functools.cached_property
    def orders(self) -> tuple[OrderView, ...]:
        """Typed views of ``raw_orders``, built on first access."""
        return tuple(order_view(o) for o in self.raw_orders)

    def allowed(self, truck: Any, order: Any) -> bool:
        """Framework feasibility; accepts views or raw documents."""
        return self.allowed_raw(_unwrap(truck), _unwrap(order))

    def sampled_trucks(self) -> list[dict[str, Any]]:
        """The scope's trucks, randomly capped at ``max_trucks_per_scope``.

        Draws from ``sample_rng`` at most once per context, on the first call only.
        """
        if self._sampled is None:
            self._sampled = _candidates.sampled_trucks(
                list(self.raw_trucks),
                cap=self.candidate_settings.max_trucks_per_scope,
                rng=self._sample_rng,
            )
        return list(self._sampled)

    def candidate_pairs(self, kind: str | None = None) -> list[Pair]:
        """Candidate ``(truck, order)`` pairs of ``kind`` (default: the scope's kind).

        ``spatial``: the scope's spatial source; ``sampled``: every allowed pair over
        :meth:`sampled_trucks`; ``full``: every allowed pair. Cached per kind.
        Raises ``ValueError`` for an unknown kind, and ``DispatchContractViolation``
        for ``spatial`` on a company scope whose resolved kind is not ``spatial``
        (no pairs were prebuilt; the runner logs it and discards the plan). A
        spatial-kind company scope with an empty prebuilt slice returns ``[]``.
        """
        kind = self.default_candidate_kind if kind is None else kind
        if kind not in self._pairs_cache:
            self._pairs_cache[kind] = self._build_pairs(kind)
        return list(self._pairs_cache[kind])

    def _build_pairs(self, kind: str) -> list[Pair]:
        if kind == "spatial":
            if self.scope.kind == "company" and self.default_candidate_kind != "spatial":
                # A company scope has only the round's prebuilt slice, built only for
                # the spatial kind; [] here would silently starve the policy.
                raise DispatchContractViolation(
                    f"candidate_pairs('spatial') on company scope {self.scope.key} needs the resolved "
                    f"candidate kind 'spatial' (pairs are prebuilt once per round only then; this scope's "
                    f"kind is {self.default_candidate_kind!r}). Declare a 'candidates' param with "
                    "'spatial' among its choices and select it, or ask for "
                    f"candidate_pairs({self.default_candidate_kind!r})."
                )
            return list(self._spatial_source())
        if kind == "sampled":
            return _candidates.full_pairs(self.sampled_trucks(), self.raw_orders, allowed=self.allowed_raw)
        if kind == "full":
            return _candidates.full_pairs(self.raw_trucks, self.raw_orders, allowed=self.allowed_raw)
        raise ValueError(
            f"candidate kind {kind!r} is not one of {list(_candidates.CANDIDATE_KINDS)}"
        )

    def deadhead_km(self, truck: Any, order: Any) -> float:
        """Haversine deadhead km (``inf`` when geometry is missing)."""
        return _objective.deadhead_km(_unwrap(truck), _unwrap(order))

    def dual_cycle_cost(self, truck: Any, order: Any, *, bonus_km: float, radius_km: float) -> float:
        """``constraints.assignment_cost`` with these two knobs."""
        return _objective.dual_cycle_cost(
            _unwrap(truck), _unwrap(order), bonus_km=bonus_km, radius_km=radius_km
        )
