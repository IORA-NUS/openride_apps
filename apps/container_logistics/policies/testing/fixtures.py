"""Deterministic synthetic WHOLE-TICK dispatch fixtures for the battery, the bench and the goldens.

Single responsibility: build the inputs of one assignment tick from a fixed recipe
(round 2, ``docs/plans/policy_slots_round2_whole_tick.md`` §5 C5). A :class:`Fixture`
is the recipe: raw truck/order documents shaped like ``AssignmentApp.assign`` reads
them, the active cooperation ``structure`` (or ``None``), ``max_travel_time_pickup``,
candidate settings, the clock and the run id the tick seed derives from.
:meth:`Fixture.world` builds the ``DispatchWorld`` exactly like the live app
(``DispatchWorld.from_profile`` on a profile carrying that structure), and
:meth:`Fixture.context` turns the recipe into the whole-tick context ONLY through
``DispatchRunner.tick_context``, so a fixture context is built exactly like a live
tick and any policy, leaf or decomposition, decides it.

Randomness: the context is the only randomness input (``ctx.rng`` is
``Random(tick_seed)``, ``tick_seed = world.tick_seed(run_id, time_step)``); the
``random`` MODULE is seeded with ``seed`` around each decide
(:meth:`Fixture.module_random`), because ``Partitioned`` draws from it by declaration
(``module_rng``). Nothing here reads global state; ``scale`` uses its own
``random.Random(seed)``. Policies receive shared document dicts and must not mutate them.

Testing side: imports the dispatch framework (and the cooperation normaliser of
``datagen.hauliers``, so a fixture structure has the compiled shape), never a policy
module.
"""

from __future__ import annotations

import logging
import random
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from apps.container_logistics.datagen import hauliers as _hauliers
from apps.container_logistics.policies.dispatch.context import DispatchClock, DispatchContext
from apps.container_logistics.policies.dispatch.helpers.candidates import CandidateSettings
from apps.container_logistics.policies.dispatch.runner import DispatchRunner, accounting_params_for
from apps.container_logistics.policies.dispatch.world import DispatchWorld, tick_seed

logger = logging.getLogger(__name__)

__all__ = [
    "Fixture",
    "SIM_CLOCK",
    "cooperation_structure",
    "seeded_module_random",
    "world_profile",
    "empty_trucks",
    "empty_orders",
    "single_truck",
    "no_geometry",
    "one_haulier",
    "all_infeasible",
    "tied_costs",
    "three_company_chain",
    "missing_haulier",
    "scale",
    "SMALL_FIXTURES",
    "ROBUSTNESS_FIXTURES",
    "MULTI_COMPANY_FIXTURES",
    "synthetic_fixtures",
]

SIM_CLOCK = "Wed, 01 Jan 2020 08:00:00 GMT"
_BASE_LON, _BASE_LAT = 103.85, 1.30


@contextmanager
def seeded_module_random(seed: int) -> Iterator[None]:
    """Seed the global ``random`` state for the block, then put the previous state back."""
    saved = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(saved)


def cooperation_structure(edges: list[tuple[str, str]], haulier_ids: list[str], *, sid: str = "coop") -> dict[str, Any]:
    """The compiled (``normalize_cooperation``) structure ``sid`` with ``edges`` over ``haulier_ids``."""
    coop = _hauliers.normalize_cooperation({"active": sid, "structures": [{"id": sid, "edges": edges}]},
                                           haulier_ids)
    return next(s for s in coop["structures"] if s["id"] == coop["active"])


def world_profile(structure: Mapping[str, Any] | None, max_travel_time_pickup: Any) -> dict[str, Any]:
    """The assignment-profile slice ``DispatchWorld.from_profile`` reads for this world."""
    prof: dict[str, Any] = {"max_travel_time_pickup": max_travel_time_pickup}
    if structure is not None:
        prof["cooperation"] = {"active": structure.get("id"), "structures": [dict(structure)]}
    return prof


@dataclass(frozen=True)
class Fixture:
    """One synthetic whole tick.

    ``structure`` is the ACTIVE cooperation structure (compiled shape) or ``None`` (no
    cooperation: own-haulier only). ``seed`` seeds the ``random`` module around each
    decide; ``run_id`` (default ``fixture:<id>``) and ``time_step`` give the tick seed.
    ``accounting_params`` are the world's accounting dials; empty means "the
    selection's own" (``runner.accounting_params_for``, the F1 one-dial rule).
    """

    id: str
    seed: int
    trucks: tuple[dict[str, Any], ...]
    orders: tuple[dict[str, Any], ...]
    structure: Mapping[str, Any] | None = None
    max_travel_time_pickup: Any = None
    settings: CandidateSettings = field(default_factory=lambda: CandidateSettings.from_profile({}))
    time_step: int = 0
    run_id: str | None = None
    accounting_params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def tick_seed(self) -> int:
        """``world.tick_seed(run_id, time_step)``: the live formula."""
        return tick_seed(self.run_id if self.run_id is not None else f"fixture:{self.id}", self.time_step)

    def world(self, runner: DispatchRunner | None = None) -> DispatchWorld:
        """The fixture's ``DispatchWorld``, built like the live app's (``from_profile``)."""
        accounting = dict(self.accounting_params)
        if not accounting and runner is not None:
            accounting = accounting_params_for(runner.resolved)
        return DispatchWorld.from_profile(world_profile(self.structure, self.max_travel_time_pickup),
                                          accounting_params=accounting)

    @contextmanager
    def module_random(self) -> Iterator[None]:
        """The ``random`` module seeded with ``seed`` for the block (restored afterwards)."""
        with seeded_module_random(self.seed):
            yield

    def context(
        self,
        runner: DispatchRunner,
        *,
        trucks: list[dict[str, Any]] | None = None,
        orders: list[dict[str, Any]] | None = None,
        scope_log: list[dict[str, Any]] | None = None,
    ) -> DispatchContext:
        """A fresh whole-tick context for ``runner`` (``trucks``/``orders`` override the order only)."""
        return runner.tick_context(
            candidates=list(self.trucks if trucks is None else trucks),
            orders=list(self.orders if orders is None else orders),
            world=self.world(runner),
            settings=self.settings,
            clock=DispatchClock(sim_clock=SIM_CLOCK, time_step=self.time_step),
            tick_seed=self.tick_seed,
            scope_log=scope_log,
        )


def _pt(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid: str, haulier: str | None, loc: dict[str, Any] | None, *,
           last_dropoff: dict[str, Any] | None = None, last_facility: str | None = None) -> dict[str, Any]:
    prof: dict[str, Any] = {"truck_size": "40ft", "restricted_areas": []}
    if haulier is not None:
        prof["haulier_id"] = haulier
    if loc is not None:
        prof["current_loc"] = loc
    if last_dropoff is not None:
        prof["last_dropoff_loc"] = last_dropoff
    if last_facility is not None:
        prof["last_dropoff_facility_name"] = last_facility
    doc: dict[str, Any] = {"_id": tid, "state": "online", "profile": prof}
    if haulier is not None:
        doc["haulier_id"] = haulier
    return doc


def _order(oid: str, haulier: str | None, pickup: dict[str, Any] | None, dropoff: dict[str, Any] | None, *,
           pickup_facility_name: str = "fac_p", dropoff_facility_name: str = "fac_d") -> dict[str, Any]:
    prof: dict[str, Any] = {
        "pickup_facility_name": pickup_facility_name,
        "dropoff_facility_name": dropoff_facility_name,
        "order_size": "1x20",
    }
    doc: dict[str, Any] = {"_id": oid, "state": "unassigned", "order_size": "1x20", "profile": prof}
    if haulier is not None:
        prof["haulier_id"] = haulier
        doc["haulier_id"] = haulier
    if pickup is not None:
        doc["pickup_loc"] = pickup
        prof["pickup_loc"] = pickup
    if dropoff is not None:
        doc["dropoff_loc"] = dropoff
        prof["dropoff_loc"] = dropoff
    return doc


def _line_trucks(n: int, haulier: str = "h_a", step: float = 0.01, *, lon0: float = _BASE_LON,
                 prefix: str = "t") -> list[dict[str, Any]]:
    return [_truck(f"{prefix}{i:03d}", haulier, _pt(lon0 + i * step, _BASE_LAT)) for i in range(n)]


def _line_orders(n: int, haulier: str = "h_a", step: float = 0.012, *, lon0: float = _BASE_LON,
                 prefix: str = "o") -> list[dict[str, Any]]:
    return [
        _order(f"{prefix}{i:03d}", haulier, _pt(lon0 + i * step, _BASE_LAT + 0.002),
               _pt(lon0 + i * step + 0.05, _BASE_LAT))
        for i in range(n)
    ]


def empty_trucks() -> Fixture:
    """Orders but no trucks."""
    return Fixture(id="empty_trucks", seed=101, trucks=(), orders=tuple(_line_orders(5)))


def empty_orders() -> Fixture:
    """Trucks but no orders."""
    return Fixture(id="empty_orders", seed=102, trucks=tuple(_line_trucks(5)), orders=())


def single_truck() -> Fixture:
    """One truck, three orders: at most one head."""
    return Fixture(id="single_truck", seed=103, trucks=tuple(_line_trucks(1)), orders=tuple(_line_orders(3)))


def no_geometry() -> Fixture:
    """No truck anchor and no pickup point anywhere: every cost is ``inf``."""
    trucks = tuple(_truck(f"t{i:03d}", "h_a", None) for i in range(4))
    orders = tuple(_order(f"o{i:03d}", "h_a", None, None) for i in range(3))
    return Fixture(id="no_geometry", seed=104, trucks=trucks, orders=orders)


def one_haulier() -> Fixture:
    """Eight trucks and six orders of one haulier at distinct distances."""
    return Fixture(id="one_haulier", seed=105, trucks=tuple(_line_trucks(8)), orders=tuple(_line_orders(6)))


def all_infeasible() -> Fixture:
    """Every pair breaks ``max_travel_time_pickup=1`` (seconds): nothing may be assigned."""
    trucks = tuple(_truck(f"t{i:03d}", "h_a", _pt(_BASE_LON + 0.03 + i * 0.01, _BASE_LAT)) for i in range(4))
    return Fixture(id="all_infeasible", seed=106, trucks=trucks, orders=tuple(_line_orders(3)),
                   max_travel_time_pickup=1)


def tied_costs() -> Fixture:
    """Two partnered hauliers, every truck and pickup co-located: every cost is exactly 0.0.

    The trucks just dropped off at the shared point, so ``assignment_cost`` clamps to
    zero and the decision is decided by tie-breaking alone. Fewer trucks than the
    spatial per-order cap, so the candidate set does not depend on input order.
    """
    here = _pt(_BASE_LON, _BASE_LAT)
    trucks = tuple(
        _truck(f"t_{h}_{i}", h, here, last_dropoff=here)
        for h in ("h_a", "h_b") for i in range(3)
    )
    orders = tuple(
        _order(f"o_{h}_{i}", h, here, _pt(_BASE_LON + 0.05, _BASE_LAT))
        for h in ("h_a", "h_b") for i in range(3)
    )
    return Fixture(id="tied_costs", seed=107, trucks=trucks, orders=orders,
                   structure=cooperation_structure([("h_a", "h_b")], ["h_a", "h_b"]))


def three_company_chain() -> Fixture:
    """A chain ``h_a - h_b - h_c`` plus a solo ``h_d``: cross-company sharing, and its limits.

    ``h_a`` and ``h_c`` are both partners of ``h_b`` but NOT of each other (the
    adjacency is edge-direct), and ``h_d`` has no partner. Each company's orders sit
    nearer a NEIGHBOUR's trucks than its own, so a cost-aware solver shares across
    every edge, and a policy that ignores the structure would pair ``h_a`` with
    ``h_c`` (or anyone with ``h_d``). Group-wise: one component ``{h_a, h_b, h_c}``
    and the singleton ``h_d``.
    """
    hids = ["h_a", "h_b", "h_c", "h_d"]
    trucks, orders = [], []
    for k, h in enumerate(hids):
        lon = _BASE_LON + 0.04 * k
        trucks += _line_trucks(3, h, step=0.004, lon0=lon, prefix=f"t_{h}_")
        # Orders placed half-way towards the next company's trucks.
        orders += _line_orders(2, h, step=0.003, lon0=lon + 0.03, prefix=f"o_{h}_")
    return Fixture(id="three_company_chain", seed=108, trucks=tuple(trucks), orders=tuple(orders),
                   structure=cooperation_structure([("h_a", "h_b"), ("h_b", "h_c")], hids), time_step=3)


def missing_haulier() -> Fixture:
    """Some trucks and orders carry no ``haulier_id``: they can never match (fail-closed)."""
    trucks = _line_trucks(4) + [_truck(f"t_none_{i}", None, _pt(_BASE_LON + 0.001 * i, _BASE_LAT)) for i in range(2)]
    orders = _line_orders(3) + [_order(f"o_none_{i}", None, _pt(_BASE_LON + 0.001 * i, _BASE_LAT + 0.002),
                                       _pt(_BASE_LON + 0.05, _BASE_LAT)) for i in range(2)]
    return Fixture(id="missing_haulier", seed=109, trucks=tuple(trucks), orders=tuple(orders))


def scale(n_trucks: int = 1000, n_orders: int = 500, hauliers: int = 1, seed: int = 0) -> Fixture:
    """A dense synthetic tick: ``n_trucks`` x ``n_orders`` over 60 facilities.

    Orders pick up and drop off at facilities; 30% of trucks have just dropped off at
    some order's pickup (cost 0, the tie case), the rest sit at a random facility or
    a random point. More than one haulier forms one fully-partnered structure. The
    id is ``scale`` at the default sizes, else ``scale_<trucks>x<orders>x<hauliers>_s<seed>``.
    """
    rng = random.Random(seed)
    facilities = [
        (f"fac_{k:02d}", _pt(103.62 + rng.random() * 0.38, 1.26 + rng.random() * 0.18))
        for k in range(60)
    ]
    hids = [f"h_{k}" for k in range(hauliers)]
    orders = []
    for i in range(n_orders):
        (pf, pp), (df, dp) = rng.sample(facilities, 2)
        orders.append(_order(f"o{i:04d}", hids[i % hauliers], pp, dp, pickup_facility_name=pf, dropoff_facility_name=df))
    trucks = []
    for i in range(n_trucks):
        hid = hids[i % hauliers]
        roll = rng.random()
        if roll < 0.3:
            src = rng.choice(orders)
            pick = src["pickup_loc"]
            trucks.append(_truck(f"t{i:04d}", hid, pick, last_dropoff=pick,
                                 last_facility=src["profile"]["pickup_facility_name"]))
        elif roll < 0.7:
            trucks.append(_truck(f"t{i:04d}", hid, rng.choice(facilities)[1]))
        else:
            trucks.append(_truck(f"t{i:04d}", hid, _pt(103.62 + rng.random() * 0.38, 1.26 + rng.random() * 0.18)))
    structure = None
    if hauliers > 1:
        structure = cooperation_structure([(a, b) for i, a in enumerate(hids) for b in hids[i + 1:]], hids)
    default = (n_trucks, n_orders, hauliers, seed) == (1000, 500, 1, 0)
    fid = "scale" if default else f"scale_{n_trucks}x{n_orders}x{hauliers}_s{seed}"
    return Fixture(id=fid, seed=seed, trucks=tuple(trucks), orders=tuple(orders), structure=structure,
                   time_step=7)


# Non-empty, small: decided by the structure group.
SMALL_FIXTURES: tuple[Callable[[], Fixture], ...] = (
    single_truck, no_geometry, one_haulier, all_infeasible, tied_costs, three_company_chain, missing_haulier,
)
# One robustness check per fixture, in this order.
ROBUSTNESS_FIXTURES: tuple[Callable[[], Fixture], ...] = (
    empty_trucks, empty_orders, single_truck, no_geometry, one_haulier, all_infeasible, tied_costs,
    three_company_chain, missing_haulier, scale,
)
# The fixtures with more than one company: where an information model can be broken.
MULTI_COMPANY_FIXTURES: tuple[Callable[[], Fixture], ...] = (tied_costs, three_company_chain)


def synthetic_fixtures() -> list[Fixture]:
    """Every synthetic fixture once, in ``ROBUSTNESS_FIXTURES`` order."""
    return [make() for make in ROBUSTNESS_FIXTURES]
