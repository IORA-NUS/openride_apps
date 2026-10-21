"""``DispatchPolicy``: the abstract contract every dispatch policy implements.

A dispatch policy answers one question per scope per tick: which free truck serves
which unassigned order. It receives a ``DispatchContext`` and returns a ``Plan``
(``decide``). The opt-in injection API (``set_rng``, ``set_tiebreak``,
``set_rebate_book``) is moved here verbatim from ``BaseAssignmentSolver`` so the two
built-ins keep their exact behaviour; ``assignment/solver/base.py`` re-exports
``BaseAssignmentSolver = PairSolver`` and the type aliases below (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P1.2).

Leaf module (plan §2.7): imports stdlib and ``apps.container_logistics.rebate``
(itself stdlib-only) and never ``apps.container_logistics.assignment.*``. Policy side
of the boundary: the framework owns feasibility, scope and commit.
"""

from __future__ import annotations

import random as _random_module
from abc import ABC, abstractmethod
from contextlib import contextmanager
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from apps.container_logistics.rebate import RebateBook

if TYPE_CHECKING:
    from .context import DispatchContext
    from .plan import Plan

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
    """Contract for a dispatch policy: ``decide(ctx) -> Plan``.

    Three callables/values may be INJECTED before a decision: ``set_rng``,
    ``set_tiebreak``, and ``set_rebate_book``. All three are opt-in — a policy
    that never reads the injected value keeps its historical behaviour exactly.

    A policy must never assign the same truck or the same order twice; the runner
    checks this (``invariants.check_plan``) and discards a violating plan.
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

    @contextmanager
    def decision_rng(self, ctx: "DispatchContext") -> Iterator[None]:
        """Bind ``self.rng`` / ``self.tiebreak_seed`` to ``ctx.rng`` / ``ctx.tiebreak_seed`` for one decision.

        The context is the decision's randomness input (round-2 §2.1): a leaf that
        draws through ``self.rng`` and breaks ties through ``self._tiebreak_seed``
        (the pre-v2 solver bodies) then draws from the scope it decides. On a
        partition scope the context carries exactly this policy's own values (the
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

    @abstractmethod
    def decide(self, ctx: "DispatchContext") -> "Plan":
        """Return the plan for one scope. Must not mutate ``ctx`` or its documents.

        Raising :class:`DispatchContractViolation` is counted as a violation and the
        scope commits nothing; any other exception propagates to the caller.
        """
        raise NotImplementedError
