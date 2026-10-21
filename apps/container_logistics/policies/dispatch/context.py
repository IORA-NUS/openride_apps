"""``DispatchContext``: everything one dispatch decision may read, for one scope of a tick.

One class for every scope (round 2, ``docs/plans/policy_slots_round2_whole_tick.md``
§2.1). A context is built only by ``dispatch/scopes.py``:

- ``tick`` -- the WHOLE tick (``scopes.tick_context``): every candidate truck and
  unassigned order, the framework's world rule as feasibility, the tick seed. This is
  what ``DispatchRunner.run`` hands the selected (top-level) policy.
- ``partition`` / ``company`` -- one cooperation group or one pooled-market company
  (``scopes.partition_context`` / ``scopes.company_context``), built by a
  decomposition solver from its parent; they inherit the parent's ``world``,
  ``tick_seed``, ``execute``, ``stats``, ``scope_log`` and ``capture``.

It carries the raw documents, the framework's feasibility (``allowed_raw``) and
accounting cost, the RNG / tiebreak seed / rebate book a decision may read, and
lazily-computed candidates.

Laziness is load-bearing, not an optimisation:

- :meth:`DispatchContext.sampled_trucks` consumes RNG, so it runs at most once per
  context and only when a policy asks (plan ``docs/plans/policy_slots_p0_p3.md`` §7
  risk 1).
- On the tick scope :attr:`DispatchContext.rng` is ``random.Random(tick_seed)``,
  CREATED the first time it is read. ``Partitioned`` never reads it, so the
  partitioned default constructs no tick ``Random`` (byte identity, round-2 §5 C3).

Tick-scope sampling cap (round-2 C3 deviation): on the tick scope the
``max_trucks_per_scope`` cap applies PER HAULIER (:data:`TICK_SAMPLE_SCOPE`): each
haulier's trucks are capped independently, concatenated in first-seen haulier order,
drawing from ``ctx.rng``. A central leaf therefore gets the same per-company
candidate coverage as the same leaf per group under ``Partitioned``; partition and
company scopes sample their whole slice as before.

Not yet here (P4): ``ctx.travel`` (a run-scoped travel-time table),
``ctx.facilities``, ``ctx.state`` and ``held`` plan tails.

Framework side of the boundary. Not a leaf module (it reaches
``assignment.constraints`` through ``views`` and ``helpers``; plan §2.7).
"""

from __future__ import annotations

import functools
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable, Literal

from apps.container_logistics.policies.framework.errors import DispatchContractViolation

from .helpers import candidates as _candidates
from .helpers import objective as _objective
from .helpers.candidates import CandidateSettings
from .views import OrderView, TruckView, order_view, truck_view

logger = logging.getLogger(__name__)

__all__ = ["DispatchContext", "DispatchClock", "Scope", "TICK_SCOPE_KEY", "TICK_SAMPLE_SCOPE", "direct_execute"]

Pair = tuple[dict[str, Any], dict[str, Any]]

#: The scope key of the whole-tick context.
TICK_SCOPE_KEY = "tick"
#: How ``sampled_trucks`` applies the cap on the tick scope (documented, see module doc).
TICK_SAMPLE_SCOPE = "per_haulier"


def direct_execute(fn: Callable[..., Any], *args: Any) -> Any:
    """``ctx.execute`` without ``solve_in_thread``: a plain call."""
    return fn(*args)


@dataclass(frozen=True)
class Scope:
    """Which slice of the tick a context covers.

    ``key`` is ``tick``, ``partition:haulier:<id>``, ``partition:component:<n>`` or
    ``company:<haulier_id>``; ``round`` is the pooled bidding round (``None``
    elsewhere).
    """

    kind: Literal["tick", "partition", "company"]
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
        world: Any = None,
        tick_seed: int | None = None,
        execute: Callable[..., Any] = direct_execute,
        stats: dict[str, Any] | None = None,
        scope_log: list[dict[str, Any]] | None = None,
        capture: Any = None,
    ) -> None:
        """``spatial_source`` and ``sample_rng`` are supplied by ``scopes.py`` only.

        ``rng`` is the value a decision reads as ``ctx.rng``; ``None`` on the tick
        scope means "``random.Random(tick_seed)``, created on first read". A
        ``sample_rng`` of ``None`` means "draw from ``ctx.rng``". ``rule`` is the
        feasibility rule behind ``allowed_raw`` (kept for capture). ``stats`` is the
        runner's dict (shared by every scope of the tick); ``scope_log`` is ``None``
        unless an audit records the scopes built; ``capture`` is the runner's capture
        hook (``None``: off).
        """
        self.scope = scope
        self.params = params
        self.clock = clock
        self._rng = rng
        self.tiebreak_seed = tiebreak_seed
        self.rebates = rebates
        self.candidate_settings = candidate_settings
        self.default_candidate_kind = default_candidate_kind
        self.raw_trucks = tuple(raw_trucks)
        self.raw_orders = tuple(raw_orders)
        self.allowed_raw = allowed_raw
        self.accounting_cost = accounting_cost
        self.rule = rule
        self.world = world
        self.tick_seed = tick_seed
        self.execute = execute
        self.stats = stats if stats is not None else {}
        self.scope_log = scope_log
        self.capture = capture
        self._spatial_source = spatial_source
        self._sample_rng = sample_rng
        self._pairs_cache: dict[str, list[Pair]] = {}
        self._sampled: list[dict[str, Any]] | None = None

    @property
    def rng(self) -> Any:
        """The decision RNG; on the tick scope ``random.Random(tick_seed)``, built on first read."""
        if self._rng is None and self.scope.kind == "tick":
            self._rng = random.Random(self.tick_seed)
        return self._rng

    @functools.cached_property
    def trucks(self) -> tuple[TruckView, ...]:
        """Typed views of ``raw_trucks``, built on first access."""
        return tuple(truck_view(t) for t in self.raw_trucks)

    @functools.cached_property
    def orders(self) -> tuple[OrderView, ...]:
        """Typed views of ``raw_orders``, built on first access."""
        return tuple(order_view(o) for o in self.raw_orders)

    @functools.cached_property
    def id_index(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """``(trucks_by_id, orders_by_id)``, the FIRST document per id (``check_plan`` and commit share it)."""
        trucks: dict[str, dict[str, Any]] = {}
        for t in self.raw_trucks:
            trucks.setdefault(str(t.get("_id")), t)
        orders: dict[str, dict[str, Any]] = {}
        for o in self.raw_orders:
            orders.setdefault(str(o.get("_id")), o)
        return trucks, orders

    def allowed(self, truck: Any, order: Any) -> bool:
        """Framework feasibility; accepts views or raw documents."""
        return self.allowed_raw(_unwrap(truck), _unwrap(order))

    def sampled_trucks(self) -> list[dict[str, Any]]:
        """The scope's trucks, randomly capped at ``max_trucks_per_scope``.

        Draws at most once per context, on the first call only: from ``sample_rng``
        on a partition/company scope; per haulier from ``ctx.rng`` on the tick scope
        (:data:`TICK_SAMPLE_SCOPE`).
        """
        if self._sampled is None:
            cap = self.candidate_settings.max_trucks_per_scope
            if self.scope.kind == "tick":
                self._sampled = _candidates.sampled_trucks_per_haulier(
                    list(self.raw_trucks), cap=cap, rng=self.rng)
            else:
                rng = self.rng if self._sample_rng is None else self._sample_rng
                self._sampled = _candidates.sampled_trucks(list(self.raw_trucks), cap=cap, rng=rng)
        return list(self._sampled)

    def candidate_pairs(self, kind: str | None = None) -> list[Pair]:
        """Candidate ``(truck, order)`` pairs of ``kind`` (default: the scope's kind).

        ``spatial``: the scope's spatial source; ``sampled``: every allowed pair over
        :meth:`sampled_trucks`; ``full``: every allowed pair. Cached per kind.
        Raises ``ValueError`` for an unknown kind, and ``DispatchContractViolation``
        for ``spatial`` on a company scope whose resolved kind is not ``spatial``
        (no pairs were prebuilt; the plan is discarded). A spatial-kind company scope
        with an empty prebuilt slice returns ``[]``.
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
