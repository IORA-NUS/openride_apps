"""``Tick``: everything one planner decision may read, for the whole tick or a narrowed sub-tick.

Single responsibility: the framework's per-decision value (plan ``plan_contract_final.md``
§2.5, §2.7). It replaces the old context and scope builders: one class, built for the
WHOLE tick by ``DispatchRunner.tick`` and narrowed only through :meth:`Tick.restrict`.

What a solver reads (the minimal author surface): ``raw_trucks`` / ``raw_orders`` (or
the typed ``trucks`` / ``orders`` views), :meth:`Tick.permitted`, ``measure`` and
``rng``. Advanced: ``serves``, ``world`` (the read-tracking ``bind.RunWorld``),
``clock``, ``seed``, ``tiebreak_seed``, ``rebates``, ``sample_rng``, ``candidates``,
``scratch``, ``execute``, and nesting through ``restrict`` / ``call`` / ``callee`` /
``docs`` / ``note_incident``.

Ownership is read off the documents, nothing else: ``constraints.haulier_of`` on the
truck and on the order. A pair is :meth:`Tick.permitted` at the top when it is OWN
(both hauliers present and equal) or its ``(owner, carrier)`` is in the top node's
declared ``cross_company`` set, AND it is physically feasible
(``constraints.pair_allowed_ignoring_haulier`` with the profile's
``max_travel_time_pickup``). A sub-tick's ``permitted`` is its parent's, optionally
narrowed by the caller's ``narrow`` predicate, never widened.

Randomness: the top ``rng`` is ``random.Random(seed)``, CREATED on first read (a solver
that never reads it builds none). A sub-tick's ``rng``, ``tiebreak_seed`` and
``rebates`` are the CALLEE instance's own (its injected values; the ``random`` module
by default), bound by :meth:`Tick.call`; its ``sample_rng`` is what the caller passed
to ``restrict`` (``None`` = the sub-tick's ``rng``).

Message 19 (a sub-tick result discarded) is logged here; message 18 (the whole tick)
by the runner.

Framework side of the boundary. Not a leaf module: it calls ``assignment.constraints``
(through the module, so tests may patch it) and imports ``views``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import random
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.framework.errors import DispatchContractViolation

from .invariants import check_result
from .result import Result
from .views import OrderView, TruckView, order_view, truck_view

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchClock",
    "Measure",
    "TOP_SCOPE",
    "Tick",
    "direct_execute",
    "report_scope_violation",
    "tick_seed",
]

Doc = dict[str, Any]
Pair = tuple[Doc, Doc]

#: The ``scope`` text of the whole tick.
TOP_SCOPE = "tick"


def direct_execute(fn: Callable[..., Any], *args: Any) -> Any:
    """``tick.execute`` without ``solve_in_thread``: a plain call."""
    return fn(*args)


def tick_seed(run_id: str, time_step: int) -> int:
    """Stable 64-bit per-tick seed: ``blake2b(f"{run_id}|{time_step}")``, big-endian.

    The formula of ``AssignmentApp._tick_seed`` (identical inputs give identical
    awards, I-P5). It makes one TICK reproducible, not a whole run: agent scheduling
    is async.
    """
    digest = hashlib.blake2b(f"{run_id}|{time_step}".encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


@dataclass(frozen=True)
class DispatchClock:
    """The tick being decided: the run's RFC-1123 ``sim_clock`` string and step index."""

    sim_clock: str
    time_step: int
    horizon_steps: int | None = None


def _unwrap(entity: Any) -> Doc:
    return entity.raw if isinstance(entity, (TruckView, OrderView)) else entity


class Measure:
    """The framework's measurement of a pair: deadhead km and the accounting cost.

    ``cost(t, o)`` is ``constraints.assignment_cost(t, o, dials)``: the cost the share
    accounting prices with, and the cost a cost-blind solver is handed. ``dials`` are
    the accounting dials (live: ``AssignmentApp._solver_params``; offline:
    ``compat.measurement.accounting_params_for``). Raw documents (views accepted by
    :meth:`deadhead_km` and :meth:`is_dual_cycle`).
    """

    __slots__ = ("dials",)

    def __init__(self, dials: Mapping[str, Any] | None) -> None:
        self.dials = MappingProxyType(dict(dials or {}))

    def cost(self, t: Doc, o: Doc) -> float:
        """Deadhead km minus the dual-cycle bonus under :attr:`dials` (``inf`` without geometry)."""
        return assign_constraints.assignment_cost(t, o, self.dials)

    @staticmethod
    def deadhead_km(t: Any, o: Any) -> float:
        """Haversine km from the truck's anchor to the order pickup; ``inf`` if unknown."""
        anchor = assign_constraints.truck_anchor_loc(_unwrap(t))
        pickup = assign_constraints.order_pickup_loc(_unwrap(o))
        if anchor is None or pickup is None:
            return float("inf")
        km = assign_constraints._haversine_km(anchor, pickup)
        return float("inf") if km is None else km

    @staticmethod
    def is_dual_cycle(t: Any, o: Any, *, radius_km: float) -> bool:
        """The exact dual-cycle predicate ``assignment_cost`` applies, at ``radius_km``."""
        o = _unwrap(o)
        pickup = assign_constraints.order_pickup_loc(o)
        return assign_constraints._is_dual_cycle_pair(_unwrap(t), o, pickup, float(radius_km))


def _ownership(cross_company: frozenset[tuple[str, str]]) -> Callable[[Doc, Doc], bool]:
    """Own (both hauliers present and equal, ``haulier_matches``) or a declared ``(owner, carrier)``."""
    haulier_of = assign_constraints.haulier_of

    def owns_or_declared(t: Doc, o: Doc) -> bool:
        carrier = haulier_of(t)
        owner = haulier_of(o)
        if carrier is None or owner is None:
            return False
        return carrier == owner or (owner, carrier) in cross_company

    return owns_or_declared


def _top_permitted(cross_company: frozenset[tuple[str, str]], mtt: Any) -> Callable[[Any, Any], bool]:
    """The top tick's predicate: ownership-or-declared AND physical feasibility; views or documents."""
    haulier_of = assign_constraints.haulier_of

    def permitted(t: Any, o: Any) -> bool:
        if type(t) is not dict:
            t = _unwrap(t)
        if type(o) is not dict:
            o = _unwrap(o)
        carrier = haulier_of(t)
        owner = haulier_of(o)
        if carrier is None or owner is None:
            return False
        if carrier != owner and (owner, carrier) not in cross_company:
            return False
        # Through the module: tests patch it (and so may a profiler).
        return assign_constraints.pair_allowed_ignoring_haulier(t, o, max_travel_time_pickup=mtt)

    return permitted


def _narrowed(parent: Callable[[Any, Any], bool], narrow: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    def permitted(t: Any, o: Any) -> bool:
        return parent(t, o) and bool(narrow(t, o))

    return permitted


class _TickShared:
    """What every tick and sub-tick of one decision shares (framework-private)."""

    __slots__ = ("tree", "stats", "scope_log", "capture", "verified", "cross_company", "owns")

    def __init__(self, *, tree: Any, stats: dict[str, Any], scope_log: list | None, capture: Any,
                 cross_company: frozenset[tuple[str, str]]) -> None:
        self.tree = tree
        self.stats = stats
        self.scope_log = scope_log
        self.capture = capture
        self.verified: set[tuple[int, int]] = set()
        self.cross_company = cross_company
        self.owns = _ownership(cross_company)


def report_scope_violation(name: str, version: str, tick: "Tick", problems: list[str], *,
                           stats: dict[str, Any]) -> None:
    """Log message 19 for a discarded sub-tick result and count it in ``stats["violations"]``."""
    stats["violations"] = stats.get("violations", 0) + 1
    logger.error(
        "PLANNER POLICY CONTRACT VIOLATION: inner %s v%s in scope %s at step %d returned an "
        "invalid result: %s. The scope's result was DISCARDED (nothing committed for this scope "
        "this tick). This is a policy bug: run `openride policy validate planner %s`.",
        name, version, tick.scope, tick.clock.time_step, "; ".join(problems), name,
    )


class Tick:
    """One decision's inputs. Plain class: ``functools.cached_property`` needs ``__dict__``.

    Built by :meth:`top` (the runner) and :meth:`restrict` (a narrowed sub-tick) only.
    Solvers read it and never mutate it or its documents (the read-only contract of
    ``DispatchPolicy``).
    """

    def __init__(
        self,
        *,
        shared: _TickShared,
        raw_trucks: tuple[Doc, ...],
        raw_orders: tuple[Doc, ...],
        clock: DispatchClock,
        seed: int,
        world: Any,
        measure: Measure,
        execute: Callable[..., Any],
        permitted: Callable[[Any, Any], bool],
        depth: int,
        scope: str,
        sample_rng: Any,
        candidates: tuple[Pair, ...] | None,
        parent: "Tick | None",
    ) -> None:
        self._shared = shared
        self.raw_trucks = raw_trucks
        self.raw_orders = raw_orders
        self.clock = clock
        self.seed = seed
        self.world = world
        self.measure = measure
        self.execute = execute
        self.permitted = permitted
        self.depth = depth
        self.scope = scope
        self.candidates = candidates
        self.scratch: dict[str, Any] = {}
        self._sample_rng = sample_rng
        self._parent = parent
        self._used = False
        self._node: Any = None
        self._params: Any = None
        self._rng: Any = None
        self.tiebreak_seed: int | None = None
        self.rebates: Any = None

    # --- construction -------------------------------------------------------

    @classmethod
    def top(
        cls,
        *,
        tree: Any,
        trucks: Iterable[Doc],
        orders: Iterable[Doc],
        world: Any,
        max_travel_time_pickup: Any,
        clock: DispatchClock,
        seed: int,
        measure: Measure,
        execute: Callable[..., Any] = direct_execute,
        stats: dict[str, Any] | None = None,
        scope_log: list | None = None,
        capture: Any = None,
    ) -> "Tick":
        """The whole tick for bound selection ``tree`` (``DispatchRunner.tick``). Draws and builds no RNG."""
        cross = frozenset(tree.declaration.cross_company)
        shared = _TickShared(tree=tree, stats=stats if stats is not None else {}, scope_log=scope_log,
                             capture=capture, cross_company=cross)
        tick = cls(shared=shared, raw_trucks=tuple(trucks), raw_orders=tuple(orders), clock=clock, seed=seed,
                   world=world, measure=measure, execute=execute,
                   permitted=_top_permitted(cross, max_travel_time_pickup), depth=0, scope=TOP_SCOPE,
                   sample_rng=None, candidates=None, parent=None)
        top = tree.top
        tick._node = top
        tick._params = top.params
        tick.tiebreak_seed = seed
        tick.rebates = getattr(top.instance, "rebate_book", None)
        return tick

    # --- what a solver reads --------------------------------------------------

    @property
    def params(self) -> Any:
        """This node's validated params (what ``solve_tick`` is handed; for the legacy ``decide`` shim)."""
        return self._params

    @property
    def rng(self) -> Any:
        """The decision RNG. Top: ``random.Random(seed)``, built on first read. Sub: the callee's own."""
        if self._rng is None and self.depth == 0:
            self._rng = random.Random(self.seed)
        return self._rng

    @property
    def sample_rng(self) -> Any:
        """What a cap sample draws from: the caller's choice at ``restrict`` (``None`` = :attr:`rng`)."""
        return self.rng if self._sample_rng is None else self._sample_rng

    @functools.cached_property
    def trucks(self) -> tuple[TruckView, ...]:
        """Typed views of ``raw_trucks`` (``haulier_id`` = ``constraints.haulier_of``), built on first access."""
        return tuple(truck_view(t) for t in self.raw_trucks)

    @functools.cached_property
    def orders(self) -> tuple[OrderView, ...]:
        """Typed views of ``raw_orders``, built on first access."""
        return tuple(order_view(o) for o in self.raw_orders)

    @functools.cached_property
    def id_index(self) -> tuple[dict[str, Doc], dict[str, Doc]]:
        """``(trucks_by_id, orders_by_id)``, the FIRST document per id (``check_result`` and commit share it)."""
        trucks: dict[str, Doc] = {}
        for t in self.raw_trucks:
            trucks.setdefault(str(t.get("_id")), t)
        orders: dict[str, Doc] = {}
        for o in self.raw_orders:
            orders.setdefault(str(o.get("_id")), o)
        return trucks, orders

    @functools.cached_property
    def _member_ids(self) -> tuple[frozenset[int], frozenset[int]]:
        return frozenset(id(t) for t in self.raw_trucks), frozenset(id(o) for o in self.raw_orders)

    def serves(self, carrier_id: Any, owner_id: Any) -> bool:
        """Whether ``carrier_id``'s trucks may serve ``owner_id``'s orders: own, or a declared pair."""
        return carrier_id == owner_id or (owner_id, carrier_id) in self._shared.cross_company

    # --- nesting --------------------------------------------------------------

    def restrict(
        self,
        *,
        trucks: Iterable[Any],
        orders: Iterable[Any],
        scope: str,
        sample_rng: Any,
        candidates: Iterable[Pair] | None = None,
        narrow: Callable[[Any, Any], bool] | None = None,
    ) -> "Tick":
        """A fresh, unbound sub-tick over a subset of this tick's documents (``call`` binds it).

        ``scope`` is free-form text (the solver's key, e.g. ``partition:haulier:h1``).
        ``sample_rng`` is REQUIRED so the choice is visible at every call site
        (``None``: the sub-tick's own ``rng``, i.e. the callee's). ``candidates`` are
        caller-built pairs (a hint, not validated); ``narrow`` narrows ``permitted``.
        Raises ``DispatchContractViolation`` when a truck or order is not (by
        identity) one of this tick's documents. Draws no RNG.
        """
        sub_trucks = tuple(_unwrap(t) for t in trucks)
        sub_orders = tuple(_unwrap(o) for o in orders)
        truck_ids, order_ids = self._member_ids
        for t in sub_trucks:
            if id(t) not in truck_ids:
                raise DispatchContractViolation(
                    f"restrict(scope={scope!r}): truck {t.get('_id')!r} is not a document of scope {self.scope}")
        for o in sub_orders:
            if id(o) not in order_ids:
                raise DispatchContractViolation(
                    f"restrict(scope={scope!r}): order {o.get('_id')!r} is not a document of scope {self.scope}")
        sub = Tick(shared=self._shared, raw_trucks=sub_trucks, raw_orders=sub_orders, clock=self.clock,
                   seed=self.seed, world=self.world, measure=self.measure, execute=self.execute,
                   permitted=self.permitted if narrow is None else _narrowed(self.permitted, narrow),
                   depth=self.depth + 1, scope=str(scope), sample_rng=sample_rng,
                   candidates=None if candidates is None else tuple(candidates), parent=self)
        log = self._shared.scope_log
        if log is not None:
            log.append({
                "kind": sub.scope.partition(":")[0],
                "key": sub.scope,
                "round": None,
                "trucks": [str(t.get("_id")) for t in sub_trucks],
                "orders": [str(o.get("_id")) for o in sub_orders],
            })
        return sub

    def callee(self, ref: Any) -> Any:
        """The bound child (``bind.BoundNode``: name, version, params, declaration) this node calls through ``ref``."""
        return self._shared.tree.child_for(self._node, ref)

    def call(self, ref: Any, sub: "Tick") -> Result | None:
        """Solve ``sub`` with this node's child ``ref`` under the same contract; ``None`` when discarded.

        ``sub`` must come from THIS tick's :meth:`restrict` and serve one call only.
        The child's params, ``rng``, ``tiebreak_seed`` and ``rebates`` (its instance's)
        are bound onto ``sub``; the solve goes through ``sub.execute`` when the child
        offloads itself. A ``DispatchContractViolation`` raised inside and any
        ``check_result`` problem are reported (message 19) and counted; any other
        exception propagates. A passing result's pairs are remembered, so the top
        check re-checks only their ownership half.
        """
        if not isinstance(sub, Tick) or sub._parent is not self:
            raise DispatchContractViolation(
                f"tick.call in scope {self.scope}: the sub-tick was not made by this tick's restrict()")
        if sub._used:
            raise DispatchContractViolation(
                f"tick.call in scope {self.scope}: sub-tick {sub.scope} was already used by a call; "
                "restrict() a fresh one per call")
        sub._used = True
        child = self.callee(ref)
        instance = child.instance
        sub._node = child
        sub._params = child.params
        sub._rng = getattr(instance, "rng", random)
        sub.tiebreak_seed = getattr(instance, "tiebreak_seed", None)
        sub.rebates = getattr(instance, "rebate_book", None)
        shared = self._shared
        capture = shared.capture
        if capture is not None:
            before = getattr(capture, "before_solve", None)
            if before is not None:
                before(sub)
        try:
            if child.offload == "self":
                result = sub.execute(child.solver.solve_tick, sub, child.params)
            else:
                result = child.solver.solve_tick(sub, child.params)
            problems: list[str] = []
        except DispatchContractViolation as exc:
            result, problems = None, [str(exc)]
        if capture is not None:
            capture(tick=sub, result=result, problems=list(problems))
        if not problems:
            problems = check_result(result, sub)
        if problems:
            report_scope_violation(child.name, child.version, sub, problems, stats=shared.stats)
            return None
        trucks, orders = sub.id_index
        verified = shared.verified
        for tid, oid in result.pairs:
            verified.add((id(trucks[tid]), id(orders[oid])))
        return result

    def docs(self, result: Result) -> list[Pair]:
        """``result``'s pairs as this tick's documents (first document per id), in pair order."""
        trucks, orders = self.id_index
        return [(trucks[tid], orders[oid]) for tid, oid in result.pairs]

    def note_incident(self) -> None:
        """Count one swallowed solver error (the caller logs it): ``stats["crashes"]``."""
        stats = self._shared.stats
        stats["crashes"] = stats.get("crashes", 0) + 1

    # --- framework-private ------------------------------------------------------

    def _top_check(self) -> Callable[[Doc, Doc], bool]:
        """The top tick's check predicate: :meth:`permitted`, minus physical work a sub-tick proved.

        A pair a sub-tick check passed (``call``) re-checks only ownership-or-declared;
        every other pair gets the full :meth:`permitted`. Equal to ``permitted`` for
        every pair, provided the documents are not mutated in between (the read-only
        contract).
        """
        shared = self._shared
        verified = shared.verified
        owns = shared.owns
        permitted = self.permitted

        def check(t: Doc, o: Doc) -> bool:
            if verified and (id(t), id(o)) in verified:
                return owns(t, o)
            return permitted(t, o)

        return check
