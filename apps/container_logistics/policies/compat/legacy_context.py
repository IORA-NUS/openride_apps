"""Legacy ``decide(ctx) -> Plan`` policies on the generic contract: ``LegacyDecideContext`` and ``Plan``.

Single responsibility: let a policy written against the round-2 contract
(``decide(ctx)`` returning a ``Plan``) run unchanged on ``solve_tick(tick, params)``
(plan ``plan_contract_final.md`` §2.2, H1). ``DispatchPolicy.solve_tick`` wraps the
tick in :class:`LegacyDecideContext`, calls ``decide`` and converts the returned
``Plan`` with :func:`result_from_legacy`.

- :class:`Plan` -- moved VERBATIM from ``dispatch/plan.py`` (which re-exports it until
  the compat shims are removed). A chain longer than one is refused at conversion
  (tails are not a framework concept, decision 2), and so is an empty chain.
- :class:`LegacyDecideContext` -- the LEAF-style members a ``decide`` uses, mapped onto
  the tick: ``raw_trucks``, ``raw_orders``, ``trucks``, ``orders``, ``params``, ``rng``,
  ``tiebreak_seed``, ``rebates``, ``clock``, ``id_index``, ``execute`` passed through;
  ``allowed`` / ``allowed_raw`` -> ``tick.permitted``; ``accounting_cost`` ->
  ``tick.measure.cost``; ``tick_seed`` -> ``tick.seed``; ``deadhead_km`` /
  ``dual_cycle_cost`` -> ``solver_lib.objective``; ``candidate_pairs(kind)`` /
  ``sampled_trucks()`` -> ``solver_lib.candidates`` with the old per-context memo;
  ``default_candidate_kind`` (``params.candidates`` where declared, else
  ``"sampled"``); ``candidate_settings``; ``scope`` (a legacy :class:`Scope`).
  NOT served: the composite internals of the old context (``ctx.world``, the scope
  builders); a composite is written against ``tick.restrict`` / ``tick.call``.
- :func:`legacy_world_reads` -- what ``DispatchPolicy``'s default ``bind`` reads for a
  ``decide`` class: the run-world keys this adapter may read on its behalf (the four
  candidate knobs), so the bind-reads rule (a bind reads every key its solve reads)
  holds for legacy policies too.

Compat (old inputs). Leaf module at module top (stdlib + ``framework.errors`` + the
leaf ``dispatch.result``): ``solver_lib`` is imported inside the methods that need it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping

from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework.errors import DispatchContractViolation

logger = logging.getLogger(__name__)

__all__ = ["LegacyDecideContext", "Plan", "Scope", "legacy_world_reads", "result_from_legacy"]

#: The legacy candidate kind of a policy without a ``candidates`` param.
_KIND_WITHOUT_CANDIDATES_PARAM = "sampled"
_CANDIDATE_KINDS = ("spatial", "sampled", "full")



@dataclass(frozen=True)
class Plan:
    """Truck id -> ordered order ids, plus free-form ``notes`` (logged at DEBUG).

    ``chains`` is stored as a read-only mapping so a plan cannot be mutated after
    the invariants checked it. Keys and ids are strings (``str(doc["_id"])``).
    """

    chains: Mapping[str, tuple[str, ...]]
    notes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Lists become tuples; anything else is kept as-is so the conversion reports it
        # instead of a str being silently exploded into characters.
        frozen = {
            str(k): tuple(v) if isinstance(v, (list, tuple)) else v
            for k, v in dict(self.chains).items()
        }
        object.__setattr__(self, "chains", MappingProxyType(frozen))
        object.__setattr__(self, "notes", MappingProxyType(dict(self.notes or {})))

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[dict, dict]],
        notes: Mapping[str, Any] | None = None,
    ) -> "Plan":
        """Build a heads-only plan from ``(truck_doc, order_doc)`` pairs, in pair order.

        Raises :class:`DispatchContractViolation` when a truck appears twice: a pair
        list cannot express a chain, so a repeated truck is a double assignment.
        A repeated ORDER is left for ``invariants.check_result`` to report.
        """
        chains: dict[str, tuple[str, ...]] = {}
        for truck, order in pairs:
            tid = str(truck.get("_id"))
            if tid in chains:
                raise DispatchContractViolation(f"truck {tid!r} appears twice in pairs")
            chains[tid] = (str(order.get("_id")),)
        return cls(chains=chains, notes=dict(notes or {}))

    def heads(self) -> list[tuple[str, str]]:
        """``(truck_id, first_order_id)`` per non-empty chain, in commit order."""
        return [(tid, chain[0]) for tid, chain in self.chains.items() if chain]


def result_from_legacy(out: Any) -> Any:
    """A legacy ``decide`` return value as a :class:`Result` (anything else is returned as is).

    ``Plan`` -> ``Result``: chains in insertion order become pairs; a chain longer
    than one, or empty, is a ``DispatchContractViolation`` (tails are not a framework
    concept); ``notes["pair_notes"]`` become ``solver_notes`` and every other note key
    becomes a ``tick_notes`` key (so an old composite's ``market`` note keeps its
    meaning). A non-``Plan`` value is handed back for ``check_result`` to report.
    """
    if isinstance(out, Result) or not isinstance(out, Plan):
        return out
    pairs = []
    for tid, chain in out.chains.items():
        if not isinstance(chain, tuple) or not chain:
            raise DispatchContractViolation(f"chain for truck {tid!r} is empty or not a tuple: {chain!r}")
        if len(chain) > 1:
            raise DispatchContractViolation(
                f"chain for truck {tid!r} has {len(chain)} orders; a result is (truck, order) pairs "
                "(chains/tours are not a framework concept: return each truck's first order)")
        pairs.append((tid, chain[0]))
    notes = dict(out.notes)
    solver_notes = notes.pop("pair_notes", None) or {}
    return Result(pairs=tuple(pairs), solver_notes=solver_notes, tick_notes=notes)


@dataclass(frozen=True)
class Scope:
    """Which slice of the tick a legacy context covers (``kind`` is ``tick`` at depth 0, else the key's prefix)."""

    kind: str
    key: str
    round: int | None = None


def _unwrap(entity: Any) -> Any:
    return getattr(entity, "raw", entity) if not isinstance(entity, dict) else entity


class LegacyDecideContext:
    """The old ``DispatchContext`` members a LEAF-style ``decide`` reads, served from a ``Tick``."""

    def __init__(self, tick: Any, *, default_candidate_kind: str | None = None) -> None:
        self._tick = tick
        params = tick.params
        self.params = params
        self.raw_trucks = tick.raw_trucks
        self.raw_orders = tick.raw_orders
        self.clock = tick.clock
        self.execute = tick.execute
        self.allowed_raw = tick.permitted
        self.accounting_cost = tick.measure.cost
        self.tick_seed = tick.seed
        self.depth = tick.depth
        kind = default_candidate_kind
        if kind is None:
            kind = getattr(params, "candidates", _KIND_WITHOUT_CANDIDATES_PARAM)
        self.default_candidate_kind = kind
        prefix = "tick" if tick.depth == 0 else tick.scope.partition(":")[0]
        self.scope = Scope(kind=prefix, key=tick.scope)
        self._pairs_cache: dict[str, list] = {}

    @property
    def rng(self) -> Any:
        return self._tick.rng

    @property
    def tiebreak_seed(self) -> Any:
        return self._tick.tiebreak_seed

    @property
    def rebates(self) -> Any:
        return self._tick.rebates

    @property
    def trucks(self) -> Any:
        return self._tick.trucks

    @property
    def orders(self) -> Any:
        return self._tick.orders

    @property
    def id_index(self) -> Any:
        return self._tick.id_index

    @property
    def candidate_settings(self) -> Any:
        from apps.container_logistics.policies.solver_lib.candidates import CandidateSettings

        return CandidateSettings.from_world(self._tick.world)

    def allowed(self, truck: Any, order: Any) -> bool:
        return self._tick.permitted(truck, order)

    def run(self, decide: Any) -> Any:
        """``decide(self)`` converted by :func:`result_from_legacy` (``DispatchPolicy.solve_tick``'s shim)."""
        return result_from_legacy(decide(self))

    def sampled_trucks(self) -> list:
        """The tick's trucks, randomly capped (drawn once per tick: ``candidates.tick_sampled_trucks``)."""
        from apps.container_logistics.policies.solver_lib import candidates

        return candidates.tick_sampled_trucks(self._tick, self.candidate_settings)

    def candidate_pairs(self, kind: str | None = None) -> list:
        """Candidate pairs of ``kind`` (default :attr:`default_candidate_kind`), cached per kind.

        ``spatial``: the caller's ``tick.candidates`` when supplied, else the tick's
        own spatial pairs; ``sampled``: every permitted pair over :meth:`sampled_trucks`;
        ``full``: every permitted pair. Raises ``ValueError`` for an unknown kind.
        """
        from apps.container_logistics.policies.solver_lib import candidates

        kind = self.default_candidate_kind if kind is None else kind
        if kind not in self._pairs_cache:
            tick = self._tick
            if kind == "spatial":
                pairs = list(tick.candidates) if tick.candidates is not None else \
                    candidates.spatial_pairs(tick, self.candidate_settings)
            elif kind == "sampled":
                pairs = candidates.tick_full_pairs(tick, self.sampled_trucks())
            elif kind == "full":
                pairs = candidates.tick_full_pairs(tick)
            else:
                raise ValueError(f"candidate kind {kind!r} is not one of {list(_CANDIDATE_KINDS)}")
            self._pairs_cache[kind] = pairs
        return list(self._pairs_cache[kind])

    def deadhead_km(self, truck: Any, order: Any) -> float:
        from apps.container_logistics.policies.solver_lib import objective

        return objective.deadhead_km(_unwrap(truck), _unwrap(order))

    def dual_cycle_cost(self, truck: Any, order: Any, *, bonus_km: float, radius_km: float) -> float:
        from apps.container_logistics.policies.solver_lib import objective

        return objective.dual_cycle_cost(_unwrap(truck), _unwrap(order), bonus_km=bonus_km, radius_km=radius_km)


def legacy_world_reads(world: Mapping[str, Any]) -> None:
    """Read, from run world ``world``, every key a :class:`LegacyDecideContext` may read.

    That is the four candidate knobs (``candidate_settings``, ``candidate_pairs``,
    ``sampled_trucks`` parse them with ``CandidateSettings.from_world``). Called from a
    legacy ``decide`` class's default ``bind``; raises like that parse on an unparseable
    value.
    """
    from apps.container_logistics.policies.solver_lib.candidates import CandidateSettings

    CandidateSettings.from_world(world)
