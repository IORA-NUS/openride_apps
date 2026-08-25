from __future__ import annotations

import random as _random_module
from abc import ABC, abstractmethod
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from apps.container_logistics.rebate import RebateBook

Truck = Dict[str, Any]
Order = Dict[str, Any]
PairAllowed = Callable[[Truck, Order], bool]
Cost = Callable[[Truck, Order], float]
Assignment = List[Tuple[Truck, Order]]


class BaseAssignmentSolver(ABC):
    """Plug-and-play contract for container-logistics order/truck matching.

    Solvers are *pure*: they receive the candidate trucks/orders for a single
    haulier bucket plus two SOLVE ARGUMENTS and return a list of
    ``(truck, order)`` pairs. They must not query the DB, import constraints, or
    know how feasibility/cost are computed — the :class:`AssignmentApp` owns that.

    - ``pair_allowed(truck, order) -> bool`` — hard feasibility (haulier, size,
      restricted areas, max reposition time).
    - ``cost(truck, order) -> float`` — soft objective to minimise (lower is
      better; e.g. repositioning/deadhead distance). Solvers that don't optimise
      a cost (random) may ignore it.

    Separate from those two solve arguments, three callables/values may be
    INJECTED before ``solve``/``solve_pairs`` is called: ``set_rng``,
    ``set_tiebreak``, and ``set_rebate_book``. All three are opt-in — a solver
    that never reads the injected value keeps its historical behaviour exactly.

    A solver must never assign the same truck or the same order twice.
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

    @abstractmethod
    def solve(
        self,
        trucks: List[Truck],
        orders: List[Order],
        *,
        pair_allowed: PairAllowed,
        cost: Cost,
    ) -> Assignment:
        raise NotImplementedError
