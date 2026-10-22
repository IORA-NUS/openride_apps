"""``DispatchPolicy``: the OPTIONAL helper base of a planner policy.

A planner policy answers one question per tick (or per sub-tick a decomposition hands
it): which free truck serves which unassigned order. The contract is the wrapper
interface ``bind(params, world, *, top) -> Declaration`` and
``solve_tick(tick, params) -> Result`` (``dispatch/bind.py``, ``dispatch/tick.py``,
``dispatch/result.py``; plan ``plan_contract_final.md`` §2.2); a class may implement it
directly. This base adds: a default ``bind`` (no cross-company pairs; the candidate
hint of a ``candidates`` param), a default ``solve_tick`` that runs a LEGACY
``decide(ctx) -> Plan`` through ``compat.legacy_context``, and the opt-in injection API (``set_rng``, ``set_tiebreak``,
``set_rebate_book``) is moved here verbatim from ``BaseAssignmentSolver`` so the two
built-ins keep their exact behaviour; ``assignment/solver/base.py`` re-exports
``BaseAssignmentSolver = PairSolver`` and the type aliases below (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P1.2).

Leaf module (plan §2.7): imports stdlib, ``apps.container_logistics.rebate`` (itself
stdlib-only), ``policies.framework.errors`` and the leaf ``dispatch.bind``; never
``apps.container_logistics.assignment.*`` (``compat.legacy_context`` is imported
inside ``solve_tick``). Policy side of the boundary: the framework owns feasibility,
ownership and commit.
"""

from __future__ import annotations

import random as _random_module
from abc import ABC
from contextlib import contextmanager
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from apps.container_logistics.policies.framework.errors import DispatchContractViolation
from apps.container_logistics.rebate import RebateBook

from .bind import Declaration

if TYPE_CHECKING:
    from .result import Result
    from .tick import Tick

__all__ = [
    "DispatchPolicy",
    "Truck",
    "Order",
    "PairAllowed",
    "Cost",
    "Assignment",
]

# Kept in the typing.* spelling of the module they were moved from, so the aliases
# re-exported by assignment/solver/base.py are the identical objects' equivalents.
Truck = Dict[str, Any]
Order = Dict[str, Any]
PairAllowed = Callable[[Truck, Order], bool]
Cost = Callable[[Truck, Order], float]
Assignment = List[Tuple[Truck, Order]]


class DispatchPolicy(ABC):
    """Helper base of a planner policy: ``bind`` + ``solve_tick`` defaults and the injection API.

    Three callables/values may be INJECTED before a decision: ``set_rng``,
    ``set_tiebreak``, and ``set_rebate_book``. All three are opt-in — a policy
    that never reads the injected value keeps its historical behaviour exactly.

    A policy must never assign the same truck or the same order twice; the runner
    checks this (``invariants.check_result``) and discards a violating result.

    **The documents are READ-ONLY.** ``tick.raw_trucks`` / ``tick.raw_orders`` (and every
    candidate pair built from them) are the tick's own dicts, shared by every sub-tick
    of the tick. A policy must not mutate them, not even temporarily ("move" a truck for a
    sub-solve and restore it afterwards): the top check trusts a sub-tick's physical
    verdict for the pairs it already proved (``Tick.call`` records them, keyed on
    document identity) and re-checks only ownership-or-declared, so a pair proved against
    a mutated document is committed as proved. Build a modified COPY instead and map
    its ids back. The cost of verifying this per head (a fingerprint at record and at
    check, ~0.9 ms per 400-head tick) exceeds what the shortcut saves, so it is a
    contract, not a check (round-2 Deviations, "Verification review fixes").
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self._params = params or {}
        self._rng: Optional[_random_module.Random] = None
        self._tiebreak_seed: Optional[int] = None
        self._rebate_book: Optional[RebateBook] = None

    @property
    def params(self) -> Dict[str, Any]:
        return self._params

    def set_rng(self, rng: Optional[_random_module.Random]) -> None:
        """Inject a seeded RNG so a tick's solve is reproducible (plan §6.7, I-P5).

        Optional by design: when never called (or called with ``None``) the
        :attr:`rng` property yields the ``random`` MODULE, so every existing
        caller — the whole legacy ``partitioned`` path — keeps the exact
        module-global behaviour it has today. Byte-identical, not merely
        equivalent.
        """
        self._rng = rng

    @property
    def rng(self) -> Union[_random_module.Random, ModuleType]:
        """The injected RNG, else the ``random`` module (today's behaviour)."""
        return self._rng if self._rng is not None else _random_module

    def set_tiebreak(self, tick_seed: Optional[int]) -> None:
        """Break equal-cost ties by a stable hash instead of a shuffle (I-P5).

        Opt-in, scoped exactly like :meth:`set_rng`: when this is never called
        (the whole legacy ``partitioned`` path) the solver keeps its historical
        shuffle and is byte-identical to today.

        Why it is needed (plan §13.4 FIX-3): seeding the shuffle makes a solve
        *reproducible for a fixed input order*, but ``scored``'s pre-shuffle order
        is a function of the caller's truck/order list order, and the subsequent
        sort is stable — so equal-cost pairs were broken differently for different
        permutations of the SAME input at the SAME seed. That is precisely the
        order-independence I-P5 claims, and it did not hold.
        """
        self._tiebreak_seed = tick_seed

    @property
    def tiebreak_seed(self) -> Optional[int]:
        return self._tiebreak_seed

    def set_rebate_book(self, book: Optional[RebateBook]) -> None:
        """Inject a :class:`RebateBook` so a solver MAY price a decision (plan §3.2).

        Opt-in, scoped exactly like :meth:`set_rng` and :meth:`set_tiebreak`: when
        this is never called — the whole shipped ``partitioned`` path AND the whole
        shipped ``pooled`` path, today — :attr:`rebate_book` stays ``None`` and the
        solver keeps its historical behaviour, byte-identical to today, not merely
        equivalent. Nothing shipped calls this. ``AssignmentApp`` constructs a book
        and calls it only when the compiled profile carries
        ``planner.rebate_aware: true``, which defaults ``false`` and is set by no
        shipped scenario — so the injection itself can never be the thing that
        breaks the allocation-inertness invariant (plan §7, R-I1b).

        A solver that opts in reads ``self.rebate_book.price_at(facility_id, when)``
        for a *decision-time estimate*; the framework does not supply a predicted
        arrival time, because "when do I think I will arrive" is a company's
        belief — SOLVER territory by plan §2, not this seam's problem.
        """
        self._rebate_book = book

    @property
    def rebate_book(self) -> Optional[RebateBook]:
        """The injected :class:`RebateBook`, or ``None`` when never injected."""
        return self._rebate_book

    def bind(self, params: Any, world: Any, *, top: bool) -> Declaration:
        """Default run-start declaration: no cross-company pairs; the candidate hint of a ``candidates`` param.

        Pure in ``(params, world)``. A top-level policy that commits cross-company pairs
        must declare them (``Declaration.cross_company``) or the framework refuses them.
        """
        spatial = getattr(params, "candidates", None) == "spatial"
        return Declaration(candidate_hint="spatial" if spatial else None)

    def solve_tick(self, tick: "Tick", params: Any) -> "Result":
        """Default decision: the LEGACY ``decide(ctx)`` over ``compat.legacy_context.LegacyDecideContext(tick)``.

        ``params`` must be ``tick.params`` (the framework passes exactly that). A
        returned ``Plan`` is converted to a ``Result`` (a chain longer than one is a
        ``DispatchContractViolation``); a ``Result`` is passed through.
        """
        from apps.container_logistics.policies.compat.legacy_context import LegacyDecideContext  # local: compat reaches solver_lib

        if params is not tick.params:
            raise DispatchContractViolation(
                f"{type(self).__name__}.solve_tick: params must be the tick's own (tick.params)")
        return LegacyDecideContext(tick).run(self.decide)

    @contextmanager
    def decision_rng(self, ctx: Any) -> Iterator[None]:
        """Bind ``self.rng`` / ``self.tiebreak_seed`` to ``ctx.rng`` / ``ctx.tiebreak_seed`` for one decision.

        The tick (or a legacy context over it) is the decision's randomness input: a
        leaf that draws through ``self.rng`` and breaks ties through
        ``self._tiebreak_seed`` (the pre-v2 solver bodies) then draws from the tick it
        decides. On a sub-tick the tick carries exactly this policy's own values (the
        ``random`` module and ``None`` on the partitioned default), so the bind is an
        identity there. The previous values are restored in ``finally``, also when
        ``decide`` raises.
        """
        saved = (self._rng, self._tiebreak_seed)
        self._rng, self._tiebreak_seed = ctx.rng, ctx.tiebreak_seed
        try:
            yield
        finally:
            self._rng, self._tiebreak_seed = saved

    def decide(self, ctx: Any) -> Any:
        """LEGACY entry point: return the ``Plan`` for one scope. Must not mutate ``ctx`` or its documents.

        Only :meth:`solve_tick`'s default calls it; a policy that implements
        ``solve_tick`` itself never needs it. Raising :class:`DispatchContractViolation`
        is counted as a violation and the scope commits nothing; any other exception
        propagates to the caller.
        """
        raise NotImplementedError(f"{type(self).__name__} implements neither solve_tick nor decide")
