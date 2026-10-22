"""Candidate generation for the dispatch slot: which (truck, order) pairs to consider.

Three kinds (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P1.3):

- ``spatial`` -- the spatial index's nearby feasible pairs (:func:`spatial_pairs` over
  a tick; the pooled market prebuilds its pairs once per round in
  ``pooled_planner.build_round_candidates`` and hands them down as ``tick.candidates``).
- ``sampled`` -- every scope truck, randomly capped at ``max_trucks_per_scope``
  (:func:`tick_sampled_trucks`), crossed with every order by the solver itself.
- ``full`` -- the uncapped feasible cross product (:func:`tick_full_pairs`).

The tick-level helpers (:func:`spatial_pairs`, :func:`tick_sampled_trucks`,
:func:`tick_full_pairs`) take a ``dispatch.tick.Tick`` and read only its public
surface: eligibility from ``tick.serves``, feasibility from ``tick.permitted``, the
cap sample from ``tick.rng`` (whole tick, per haulier: :data:`TICK_SAMPLE_SCOPE`) or
``tick.sample_rng`` (a sub-tick, the whole slice). The knobs come from the run world
(:meth:`CandidateSettings.from_world`). The list-level functions below them are
verbatim moves of code that lived inline in ``assignment/app.py``; the dispatch golden
test holds them byte-identical, including how many draws they take from the RNG they
are handed. ``spatial.iter_candidate_pairs`` is called THROUGH ITS MODULE because
tests monkeypatch it (plan §2.8).

Solver helper (``policies/solver_lib``; the framework never imports it). Not a leaf module: it
imports ``assignment.spatial`` / ``assignment.constraints`` at top, which is safe
while ``assignment/app.py`` imports ``policies`` only lazily (plan §2.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment import spatial

logger = logging.getLogger(__name__)

__all__ = [
    "CandidateKind",
    "CANDIDATE_KINDS",
    "DEFAULT_MAX_TRUCKS_PER_SCOPE",
    "CandidateSettings",
    "TICK_SAMPLE_SCOPE",
    "spatial_pairs",
    "tick_sampled_trucks",
    "tick_full_pairs",
    "spatial_pairs_for_partition",
    "sampled_trucks",
    "sampled_trucks_per_haulier",
    "full_pairs",
]

CandidateKind = Literal["spatial", "sampled", "full"]
CANDIDATE_KINDS: tuple[str, ...] = ("spatial", "sampled", "full")

# Profile key ``max_trucks_per_haulier`` (moved here from assignment/app.py). The
# sampled path scores every truck x order pair, O(trucks x orders); past ~500 trucks
# per scope a single assign() can no longer finish inside the scheduler step barrier
# (observed: a 5000-truck fleet produced 0 haul trips while 500 produced ~464). So
# the trucks fed to a policy are a random sample per scope each tick: every truck
# rotates in across ticks and the per-tick cost no longer grows with fleet size. 500
# keeps the worst case at 500 trucks x 500 orders = 250K pairs, the proven-fast
# envelope. Raise it to widen the candidate pool at the cost of solve time.
DEFAULT_MAX_TRUCKS_PER_SCOPE = 500

#: How :func:`tick_sampled_trucks` applies the cap on the WHOLE tick (``depth == 0``):
#: per haulier (round-2 C3 deviation). A sub-tick samples its whole slice at once.
TICK_SAMPLE_SCOPE = "per_haulier"
_SAMPLED_MEMO = "solver_lib.candidates.sampled"


@dataclass(frozen=True)
class CandidateSettings:
    """The spatial-index and sampling knobs of one assignment profile."""

    cell_deg: float
    per_order: int
    max_rings: int
    max_trucks_per_scope: int

    @classmethod
    def from_profile(cls, prof: Mapping[str, Any]) -> "CandidateSettings":
        """Parse exactly as ``AssignmentApp.assign`` does (clamps included).

        Raises ``TypeError``/``ValueError`` on an unparseable value, as today.
        """
        return cls(
            cell_deg=float(prof.get("spatial_cell_deg", spatial.DEFAULT_CELL_DEG)),
            per_order=max(1, int(prof.get(
                "spatial_candidates_per_order", spatial.DEFAULT_PER_ORDER_CANDIDATES
            ))),
            max_rings=max(0, int(prof.get("spatial_max_rings", spatial.DEFAULT_MAX_RINGS))),
            max_trucks_per_scope=max(
                1, int(prof.get("max_trucks_per_haulier", DEFAULT_MAX_TRUCKS_PER_SCOPE))
            ),
        )

    @classmethod
    def from_world(cls, world: Mapping[str, Any]) -> "CandidateSettings":
        """The same parse over a run world (``dispatch.bind.RunWorld``): reads the four knob keys."""
        return cls.from_profile(world)

    def spatial_params(self) -> dict[str, Any]:
        """The three-key dict ``PooledPlannerContext.spatial_params`` carries today."""
        return {"cell_deg": self.cell_deg, "per_order": self.per_order, "max_rings": self.max_rings}

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for capture and the run stamp."""
        return {**self.spatial_params(), "max_trucks_per_scope": self.max_trucks_per_scope}


def spatial_pairs(tick: Any, settings: CandidateSettings) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The tick's spatial candidate pairs: :func:`spatial_pairs_for_partition` over its documents.

    Eligibility is ``tick.serves`` (own haulier or a declared partner), feasibility
    ``tick.permitted``. Draws no RNG.
    """
    return spatial_pairs_for_partition(list(tick.raw_trucks), list(tick.raw_orders), serves=tick.serves,
                                       allowed=tick.permitted, settings=settings)


def tick_sampled_trucks(tick: Any, settings: CandidateSettings) -> list[dict[str, Any]]:
    """The tick's trucks, randomly capped at ``settings.max_trucks_per_scope``; drawn ONCE per tick object.

    The whole tick (``depth == 0``) caps per haulier from ``tick.rng``
    (:data:`TICK_SAMPLE_SCOPE`); a sub-tick caps its whole slice from
    ``tick.sample_rng``. Memoised in ``tick.scratch`` so a second call draws nothing;
    a fresh list is returned each call.
    """
    sampled = tick.scratch.get(_SAMPLED_MEMO)
    if sampled is None:
        cap = settings.max_trucks_per_scope
        if tick.depth == 0:
            sampled = sampled_trucks_per_haulier(list(tick.raw_trucks), cap=cap, rng=tick.rng)
        else:
            sampled = sampled_trucks(list(tick.raw_trucks), cap=cap, rng=tick.sample_rng)
        tick.scratch[_SAMPLED_MEMO] = sampled
    return list(sampled)


def tick_full_pairs(tick: Any, trucks: Iterable[dict[str, Any]] | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every ``tick.permitted`` pair over ``trucks`` (default: all the tick's) and its orders, truck-major."""
    return full_pairs(tick.raw_trucks if trucks is None else trucks, tick.raw_orders, allowed=tick.permitted)


def spatial_pairs_for_partition(
    trucks: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    *,
    serves: Callable[[str, str], bool],
    allowed: Callable[[dict[str, Any], dict[str, Any]], bool],
    settings: CandidateSettings,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Spatial candidate pairs for one set of trucks and orders, grouped by ORDER haulier.

    Verbatim body of the spatial branch of ``AssignmentApp.assign._solve_bucket``
    (``rule.serves`` / ``rule.allowed`` are now the ``serves`` / ``allowed`` arguments:
    ``serves(truck_haulier, order_haulier)``).
    Candidates are generated per order-haulier (first-seen order) over only the
    trucks eligible to serve that haulier (own + edge partners): a single
    component-wide index let share-INELIGIBLE trucks consume the per-order cap by
    pure proximity and starve orders while their own haulier had free trucks in
    range. The caller still runs ONE solve over the union, so nothing is
    double-assigned across groups.
    """
    orders_by_haulier: dict[str, list[dict[str, Any]]] = {}
    for o in orders:
        oh = assign_constraints.haulier_of(o)
        if oh:
            orders_by_haulier.setdefault(oh, []).append(o)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for oh, o_group in orders_by_haulier.items():
        eligible = [
            t for t in trucks
            if (assign_constraints.haulier_of(t) or "") and serves(
                assign_constraints.haulier_of(t), oh
            )
        ]
        if not eligible:
            continue
        pairs.extend(
            spatial.iter_candidate_pairs(
                eligible,
                o_group,
                pair_allowed=allowed,
                cell_deg=settings.cell_deg,
                per_order=settings.per_order,
                max_rings=settings.max_rings,
            )
        )
    return pairs


def sampled_trucks(trucks: list[dict[str, Any]], *, cap: int, rng: Any) -> list[dict[str, Any]]:
    """``trucks`` itself when ``len <= cap``, else ``rng.sample(trucks, cap)``.

    Returns the SAME object when not sampling (as the inline code did); pair solvers
    copy their input, so callers need not. ``rng`` is the ``random`` module on the
    partitioned path and the tick ``Random`` on the pooled path; exactly one
    ``sample`` call is made, and only when over the cap (plan §7 risk 1).
    """
    bt = trucks
    if len(bt) > cap:
        bt = rng.sample(bt, cap)
    return bt


def sampled_trucks_per_haulier(trucks: list[dict[str, Any]], *, cap: int, rng: Any) -> list[dict[str, Any]]:
    """The tick-scope sample: :func:`sampled_trucks` applied to each haulier's trucks.

    Trucks are grouped by ``haulier_of`` in first-seen order (a haulier-less truck
    forms its own group; it can never match anyway), each group is capped
    independently, and the groups are concatenated in that order. ``rng`` draws once
    per group that exceeds ``cap`` and never otherwise, so a tick under the cap
    consumes nothing. Why per haulier: a whole-tick cap would give a central solver
    far less candidate coverage than the same solver per group under ``Partitioned``
    (round-2 C3 deviation).
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for truck in trucks:
        groups.setdefault(assign_constraints.haulier_of(truck), []).append(truck)
    out: list[dict[str, Any]] = []
    for group in groups.values():
        out.extend(sampled_trucks(group, cap=cap, rng=rng))
    return out


def full_pairs(
    trucks: Iterable[dict[str, Any]],
    orders: Iterable[dict[str, Any]],
    *,
    allowed: Callable[[dict[str, Any], dict[str, Any]], bool],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every allowed pair, truck-major (the order ``GreedyNearest.solve`` builds)."""
    orders = list(orders)
    return [(t, o) for t in trucks for o in orders if allowed(t, o)]
