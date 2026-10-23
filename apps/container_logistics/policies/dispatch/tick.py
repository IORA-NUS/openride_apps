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

Integrity against a DISHONEST solver (review H2, L5, L6): the framework checks a
result with state it took when :meth:`Tick.top` was built and that no solver can
rewrite through the tick -- its own predicate (``Tick.permitted`` is a read-only
property handing out that same function), its own id index (no instance ``__dict__``
to pre-seed; ``__slots__`` and read-only properties throughout), the declared pairs,
and a fingerprint of every document. A result naming a document that was modified
during the solve is refused: in a sub-tick for the paired documents, at the top for the
paired documents AND every truck of the tick (the share accounting prices the owner
reserve over all of them after the commit; an unpaired order is read by nothing). So
the verified-pair memo is sound: a sub-tick's physical verdict was reached by the
framework's predicate on unmodified documents. Out of scope, as in any Python: a solver
that deliberately reaches framework-private state through ``object.__setattr__``,
``gc`` or closure cells. That is a review finding, not a contract a test can pin.

Message 19 (a sub-tick result discarded) is logged here; message 18 (the whole tick)
by the runner.

Framework side of the boundary. Not a leaf module: it calls ``assignment.constraints``
(through the module, so tests may patch it) and imports ``views``.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import random
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.framework.errors import DispatchContractViolation

from .bind import value_fingerprint
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
        object.__setattr__(self, "dials", MappingProxyType(dict(dials or {})))

    def __setattr__(self, name: str, value: Any) -> None:
        # The accounting prices the committed pairs with this object after the solve.
        raise DispatchContractViolation(f"Measure.{name} is read-only")

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
        """The dual-cycle predicate ``assignment_cost`` applies, at ``radius_km`` (a measurement, e.g. the bench's).

        A verbatim copy of ``constraints._is_dual_cycle_pair`` over the geometry readers
        (the order's pickup facility is the truck's last drop-off facility, else the last
        drop-off point lies within ``radius_km`` of the pickup);
        ``tests/test_solver_contract_types.py`` pins the two equal.
        """
        t, o = _unwrap(t), _unwrap(o)
        last_fac = assign_constraints._truck_last_dropoff_facility(t)
        order_pickup_fac = (o.get("profile") or {}).get("pickup_facility_name")
        if last_fac is not None and order_pickup_fac is not None and last_fac == str(order_pickup_fac):
            return True
        last_loc = assign_constraints._truck_last_dropoff_loc(t)
        if last_loc is not None:
            d = assign_constraints._haversine_km(last_loc, assign_constraints.order_pickup_loc(o))
            if d is not None and d <= float(radius_km):
                return True
        return False


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


_set = object.__setattr__
_dumps = pickle.dumps


def _fingerprint(doc: Doc, like: bytes) -> bytes:
    """``doc``'s fingerprint in the form ``like`` was taken in (the inlined fast path, or the fallback)."""
    if not like.startswith(b"r"):
        try:
            return _dumps(doc, 5)
        except Exception:  # noqa: BLE001 - became unpicklable: certainly modified
            return b""
    return value_fingerprint(doc)


class _NoInstanceDict(DispatchContractViolation, AttributeError):
    """``tick.__dict__``: a contract violation (message 18/19) that ``hasattr`` still reads as absent."""


class _TickShared:
    """What every tick and sub-tick of one decision shares (framework-private, sealed).

    Built once, by :meth:`Tick.top`, BEFORE the solver runs, and never re-derived from
    anything a solver can rewrite (review H2):

    - ``index`` -- ``(trucks_by_id, orders_by_id)`` over the whole tick's documents,
      first document per id, read-only: the top check and the commit
      (:meth:`Tick.docs`) look pairs up here, so only the tick's own documents are
      ever committed;
    - fingerprints of every document (``bind.value_fingerprint``, keyed by ``id()``):
      :meth:`intact` / :meth:`changed` tell whether a document was modified since the
      tick was built, so a pair (or the accounting that follows) can never be judged on
      a document the solver rewrote;
    - the verified-pair memo, held in closures, written only by :meth:`Tick.call` after
      the FRAMEWORK's check of a sub-tick passed against intact documents.

    Every attribute is read-only after construction (assignment raises
    ``DispatchContractViolation``); ``stats`` and ``scope_log`` are the caller's
    mutable records.
    """

    __slots__ = ("tree", "stats", "scope_log", "capture", "cross_company", "owns", "index",
                 "_fingerprints", "_docs", "remember", "proved", "_sealed")

    def __init__(self, *, tree: Any, stats: dict[str, Any], scope_log: list | None, capture: Any,
                 cross_company: frozenset[tuple[str, str]], trucks: tuple[Doc, ...],
                 orders: tuple[Doc, ...]) -> None:
        _set(self, "tree", tree)
        _set(self, "stats", stats)
        _set(self, "scope_log", scope_log)
        _set(self, "capture", capture)
        _set(self, "cross_company", cross_company)
        _set(self, "owns", _ownership(cross_company))
        _set(self, "index", _index(trucks, orders))
        docs = trucks + orders
        try:  # the fast path of value_fingerprint, inlined (~10% of a tick's framework cost)
            fingerprints = {id(doc): _dumps(doc, 5) for doc in docs}
        except Exception:  # noqa: BLE001 - an unpicklable document: the per-document fallback
            fingerprints = {id(doc): value_fingerprint(doc) for doc in docs}
        _set(self, "_fingerprints", MappingProxyType(fingerprints))
        _set(self, "_docs", (trucks, orders))  # keeps every fingerprinted id alive
        verified: set[tuple[int, int]] = set()

        def remember(t: Doc, o: Doc) -> None:
            verified.add((id(t), id(o)))

        def proved(t: Doc, o: Doc) -> bool:
            return bool(verified) and (id(t), id(o)) in verified

        _set(self, "remember", remember)
        _set(self, "proved", proved)
        _set(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        raise DispatchContractViolation(f"the tick's framework state is read-only (attempted to set {name!r})")

    __delattr__ = __setattr__  # type: ignore[assignment]

    def __getattr__(self, name: str) -> Any:
        # Only reached for a name it does not have (e.g. the memo set, which lives in a
        # closure): a contract violation, still an AttributeError for hasattr/getattr.
        raise _NoInstanceDict(f"the tick's framework state has no attribute {name!r} (it is read-only)")

    def intact(self, doc: Doc) -> bool:
        """Whether ``doc`` is one of this tick's documents, unmodified since the tick was built."""
        before = self._fingerprints.get(id(doc))
        return before is not None and _fingerprint(doc, before) == before

    def changed(self) -> list[str]:
        """The ids of this tick's TRUCKS modified since it was built (``[]``: none).

        Trucks only: after the commit the share accounting reads every truck (the owner
        reserve) but only the committed orders, which the check has already found intact.
        """
        out = []
        fingerprints = self._fingerprints
        for doc in self._docs[0]:
            before = fingerprints[id(doc)]
            if _fingerprint(doc, before) != before:
                out.append(str(doc.get("_id")) if isinstance(doc, dict) else repr(doc))
        return out


def _index(trucks: Iterable[Doc], orders: Iterable[Doc]) -> tuple[Mapping[str, Doc], Mapping[str, Doc]]:
    """``(trucks_by_id, orders_by_id)``, the FIRST document per ``str(_id)``, as read-only mappings."""
    by_truck: dict[str, Doc] = {}
    for t in trucks:
        by_truck.setdefault(str(t.get("_id")), t)
    by_order: dict[str, Doc] = {}
    for o in orders:
        by_order.setdefault(str(o.get("_id")), o)
    return MappingProxyType(by_truck), MappingProxyType(by_order)


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
    """One decision's inputs.

    Built by :meth:`top` (the runner) and :meth:`restrict` (a narrowed sub-tick) only.
    Solvers read it and never mutate it or its documents (the read-only contract of
    ``DispatchPolicy``), and the framework holds them to it (review H2): every
    attribute is read-only (assigning one raises ``DispatchContractViolation``, i.e.
    message 18 at the top, 19 in a sub-tick), there is no instance ``__dict__`` to
    pre-seed, the predicate the framework checks with is its own (:attr:`permitted`
    hands out that same function, but replacing it replaces nothing), and a document
    modified during the solve fails the check (``_TickShared.intact`` / ``changed``).
    ``scratch`` is the one mutable member: a dict the framework never reads.
    """

    __slots__ = ("_shared", "_raw_trucks", "_raw_orders", "_clock", "_seed", "_world", "_measure",
                 "_execute", "_permitted", "_depth", "_scope", "_candidates", "_scratch", "_sample_rng",
                 "_parent", "_used", "_node", "_params", "_rng", "_tiebreak_seed", "_rebates",
                 "_cross_company", "_index", "_trucks", "_orders", "_members")

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
        cross_company: frozenset[tuple[str, str]] | None = None,
        index: tuple[Mapping[str, Doc], Mapping[str, Doc]] | None = None,
    ) -> None:
        _set(self, "_shared", shared)
        _set(self, "_raw_trucks", raw_trucks)
        _set(self, "_raw_orders", raw_orders)
        _set(self, "_clock", clock)
        _set(self, "_seed", seed)
        _set(self, "_world", world)
        _set(self, "_measure", measure)
        _set(self, "_execute", execute)
        _set(self, "_permitted", permitted)
        _set(self, "_depth", depth)
        _set(self, "_scope", scope)
        _set(self, "_candidates", candidates)
        _set(self, "_scratch", {})
        _set(self, "_sample_rng", sample_rng)
        _set(self, "_parent", parent)
        _set(self, "_used", False)
        _set(self, "_node", None)
        _set(self, "_params", None)
        _set(self, "_rng", None)
        _set(self, "_tiebreak_seed", None)
        _set(self, "_rebates", None)
        _set(self, "_cross_company", shared.cross_company if cross_company is None else cross_company)
        _set(self, "_index", _index(raw_trucks, raw_orders) if index is None else index)
        _set(self, "_trucks", None)
        _set(self, "_orders", None)
        _set(self, "_members", None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise DispatchContractViolation(
            f"Tick.{name} is read-only: a solver reads the tick and never rewrites it (scope "
            f"{self._scope}); keep per-tick memos in tick.scratch.")

    def __delattr__(self, name: str) -> None:
        raise DispatchContractViolation(f"Tick.{name} is read-only (scope {self._scope}).")

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup failed. There is no instance __dict__ to
        # pre-seed (e.g. a forged ``id_index``); say so as a contract violation, which is
        # also an AttributeError so ``hasattr`` / ``getattr(..., default)`` still work.
        if name == "__dict__":
            raise _NoInstanceDict(
                f"Tick has no instance __dict__ (scope {object.__getattribute__(self, '_scope')}): its state is "
                "read-only; keep per-tick memos in tick.scratch.")
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

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
        """The whole tick for bound selection ``tree`` (``DispatchRunner.tick``). Draws and builds no RNG.

        Takes the framework's snapshot (index, document fingerprints) here, before any
        solver code runs.
        """
        cross = frozenset(tree.declaration.cross_company)
        raw_trucks, raw_orders = tuple(trucks), tuple(orders)
        shared = _TickShared(tree=tree, stats=stats if stats is not None else {}, scope_log=scope_log,
                             capture=capture, cross_company=cross, trucks=raw_trucks, orders=raw_orders)
        tick = cls(shared=shared, raw_trucks=raw_trucks, raw_orders=raw_orders, clock=clock, seed=seed,
                   world=world, measure=measure, execute=execute,
                   permitted=_top_permitted(cross, max_travel_time_pickup), depth=0, scope=TOP_SCOPE,
                   sample_rng=None, candidates=None, parent=None, index=shared.index)
        top = tree.top
        _set(tick, "_node", top)
        _set(tick, "_params", top.params)
        _set(tick, "_tiebreak_seed", seed)
        _set(tick, "_rebates", getattr(top.instance, "rebate_book", None))
        return tick

    # --- what a solver reads --------------------------------------------------

    @property
    def raw_trucks(self) -> tuple[Doc, ...]:
        """This (sub-)tick's truck documents."""
        return self._raw_trucks

    @property
    def raw_orders(self) -> tuple[Doc, ...]:
        """This (sub-)tick's order documents."""
        return self._raw_orders

    @property
    def permitted(self) -> Callable[[Any, Any], bool]:
        """``permitted(truck, order)``: the predicate the framework checks this tick's result with.

        At the top: own (both hauliers present and equal) or a declared ``(owner,
        carrier)``, AND physically feasible. In a sub-tick: the parent's, narrowed by the
        caller's ``narrow`` and by the callee's own non-empty ``cross_company``. Views or
        documents.
        """
        return self._permitted

    @property
    def clock(self) -> DispatchClock:
        return self._clock

    @property
    def seed(self) -> int:
        """The per-tick seed (``tick_seed``), inherited by sub-ticks."""
        return self._seed

    @property
    def world(self) -> Any:
        """The read-tracking run world (``bind.RunWorld``); its values are copies."""
        return self._world

    @property
    def measure(self) -> Measure:
        return self._measure

    @property
    def execute(self) -> Callable[..., Any]:
        """``execute(fn, *args)``: a plain call, or ``solve_in_thread``'s thread-pool call."""
        return self._execute

    @property
    def depth(self) -> int:
        """0 at the top, +1 per ``restrict``."""
        return self._depth

    @property
    def scope(self) -> str:
        """``"tick"`` at the top, the caller's key below."""
        return self._scope

    @property
    def candidates(self) -> tuple[Pair, ...] | None:
        """Caller-built pairs handed to ``restrict`` (a hint, not validated), or ``None``."""
        return self._candidates

    @property
    def scratch(self) -> dict[str, Any]:
        """A per-tick-object dict for the solver's memos; the framework never reads it."""
        return self._scratch

    @property
    def tiebreak_seed(self) -> int | None:
        """Top: the tick seed. Sub: the callee instance's injected tie-break (``None`` if none)."""
        return self._tiebreak_seed

    @property
    def rebates(self) -> Any:
        """The node instance's injected rebate book (``None`` unless ``planner.rebate_aware``)."""
        return self._rebates

    @property
    def params(self) -> Any:
        """This node's validated params (what ``solve_tick`` is handed; for the legacy ``decide`` shim)."""
        return self._params

    @property
    def rng(self) -> Any:
        """The decision RNG. Top: ``random.Random(seed)``, built on first read. Sub: the callee's own."""
        if self._rng is None and self._depth == 0:
            _set(self, "_rng", random.Random(self._seed))
        return self._rng

    @property
    def sample_rng(self) -> Any:
        """What a cap sample draws from: the caller's choice at ``restrict`` (``None`` = :attr:`rng`)."""
        return self.rng if self._sample_rng is None else self._sample_rng

    @property
    def trucks(self) -> tuple[TruckView, ...]:
        """Typed views of ``raw_trucks`` (``haulier_id`` = ``constraints.haulier_of``), built on first access."""
        if self._trucks is None:
            _set(self, "_trucks", tuple(truck_view(t) for t in self._raw_trucks))
        return self._trucks

    @property
    def orders(self) -> tuple[OrderView, ...]:
        """Typed views of ``raw_orders``, built on first access."""
        if self._orders is None:
            _set(self, "_orders", tuple(order_view(o) for o in self._raw_orders))
        return self._orders

    @property
    def id_index(self) -> tuple[Mapping[str, Doc], Mapping[str, Doc]]:
        """``(trucks_by_id, orders_by_id)``, the FIRST document per id, read-only (``check_result`` and commit share it)."""
        return self._index

    def _member_ids(self) -> tuple[frozenset[int], frozenset[int]]:
        if self._members is None:
            _set(self, "_members", (frozenset(id(t) for t in self._raw_trucks),
                                    frozenset(id(o) for o in self._raw_orders)))
        return self._members

    def serves(self, carrier_id: Any, owner_id: Any) -> bool:
        """Whether ``carrier_id``'s trucks may serve ``owner_id``'s orders: own, or a declared pair.

        ``False`` when either id is missing (``None`` or empty), exactly like
        :attr:`permitted`'s ownership half (review L6). In a sub-tick whose callee
        declared its own ``cross_company``, only those pairs count.
        """
        if carrier_id is None or owner_id is None or carrier_id == "" or owner_id == "":
            return False
        return carrier_id == owner_id or (owner_id, carrier_id) in self._cross_company

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
        truck_ids, order_ids = self._member_ids()
        for t in sub_trucks:
            if id(t) not in truck_ids:
                raise DispatchContractViolation(
                    f"restrict(scope={scope!r}): truck {t.get('_id')!r} is not a document of scope {self._scope}")
        for o in sub_orders:
            if id(o) not in order_ids:
                raise DispatchContractViolation(
                    f"restrict(scope={scope!r}): order {o.get('_id')!r} is not a document of scope {self._scope}")
        sub = Tick(shared=self._shared, raw_trucks=sub_trucks, raw_orders=sub_orders, clock=self._clock,
                   seed=self._seed, world=self._world, measure=self._measure, execute=self._execute,
                   permitted=self._permitted if narrow is None else _narrowed(self._permitted, narrow),
                   depth=self._depth + 1, scope=str(scope), sample_rng=sample_rng,
                   candidates=None if candidates is None else tuple(candidates), parent=self,
                   cross_company=self._cross_company)
        log = self._shared.scope_log
        if log is not None:
            log.append({
                "kind": sub._scope.partition(":")[0],
                "key": sub._scope,
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
        are bound onto ``sub``; a child that declared a non-empty ``cross_company`` (a
        subset of the top's, message 24) has ``sub`` narrowed to own-or-those pairs
        (review L5; an empty declaration, every built-in inner's, inherits the caller's).
        The solve goes through ``sub.execute`` when the child offloads itself. A
        ``DispatchContractViolation`` raised inside and any ``check_result`` problem
        (checked with the FRAMEWORK's predicate for ``sub``, on intact documents) are
        reported (message 19) and counted; any other exception propagates. A passing
        result's pairs are remembered, so the top check re-checks only their ownership
        half.
        """
        if not isinstance(sub, Tick) or sub._parent is not self:
            raise DispatchContractViolation(
                f"tick.call in scope {self._scope}: the sub-tick was not made by this tick's restrict()")
        if sub._used:
            raise DispatchContractViolation(
                f"tick.call in scope {self._scope}: sub-tick {sub._scope} was already used by a call; "
                "restrict() a fresh one per call")
        _set(sub, "_used", True)
        child = self.callee(ref)
        instance = child.instance
        _set(sub, "_node", child)
        _set(sub, "_params", child.params)
        _set(sub, "_rng", getattr(instance, "rng", random))
        _set(sub, "_tiebreak_seed", getattr(instance, "tiebreak_seed", None))
        _set(sub, "_rebates", getattr(instance, "rebate_book", None))
        own_cross = child.declaration.cross_company
        if own_cross:
            narrowed = frozenset(own_cross) & sub._cross_company
            _set(sub, "_cross_company", narrowed)
            _set(sub, "_permitted", _narrowed(sub._permitted, _ownership(narrowed)))
        shared = self._shared
        capture = shared.capture
        if capture is not None:
            before = getattr(capture, "before_solve", None)
            if before is not None:
                before(sub)
        try:
            if child.offload == "self":
                result = sub._execute(child.solver.solve_tick, sub, child.params)
            else:
                result = child.solver.solve_tick(sub, child.params)
            problems: list[str] = []
        except DispatchContractViolation as exc:
            result, problems = None, [str(exc)]
        if capture is not None:
            capture(tick=sub, result=result, problems=list(problems))
        if not problems:
            problems = check_result(result, sub, permitted=sub._permitted, index=sub._index,
                                    intact=shared.intact)
        if problems:
            report_scope_violation(child.name, child.version, sub, problems, stats=shared.stats)
            return None
        trucks, orders = sub._index
        remember = shared.remember
        for tid, oid in result.pairs:
            remember(trucks[tid], orders[oid])
        return result

    def docs(self, result: Result) -> list[Pair]:
        """``result``'s pairs as this tick's documents (first document per id), in pair order.

        Looked up in the index the framework built with the tick, never in anything the
        solver handed back, so only the tick's own documents are committed.
        """
        trucks, orders = self._index
        return [(trucks[tid], orders[oid]) for tid, oid in result.pairs]

    def note_incident(self) -> None:
        """Count one swallowed solver error (the caller logs it): ``stats["crashes"]``."""
        stats = self._shared.stats
        stats["crashes"] = stats.get("crashes", 0) + 1

    # --- framework-private ------------------------------------------------------

    def _top_check(self) -> Callable[[Doc, Doc], bool]:
        """The top tick's check predicate: the framework's :attr:`permitted`, minus physical work a sub-tick proved.

        A pair a sub-tick check passed (``call``: the framework's predicate for that
        sub-tick, on intact documents) re-checks only ownership-or-declared; every other
        pair gets the full predicate. Equal to ``permitted`` for every pair, because the
        top check also refuses any document modified since the tick was built
        (``_TickShared.intact``).
        """
        shared = self._shared
        proved = shared.proved
        owns = shared.owns
        permitted = self._permitted

        def check(t: Doc, o: Doc) -> bool:
            if proved(t, o):
                return owns(t, o)
            return permitted(t, o)

        return check
