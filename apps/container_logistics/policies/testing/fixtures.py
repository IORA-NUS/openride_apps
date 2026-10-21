"""Deterministic synthetic dispatch fixtures for the battery, the bench and the goldens.

Single responsibility: build the inputs of one dispatch scope from a fixed recipe
(plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.1). A :class:`Fixture` is the
recipe (raw truck/order documents shaped like ``AssignmentApp.assign`` reads them,
the feasibility rule, candidate settings, clock); :meth:`Fixture.context` turns it
into a ``DispatchContext`` ONLY through ``DispatchRunner.partition_context``, so a
fixture context is built exactly like a live one.

Every fixture has an ``id`` and a ``seed``; nothing here reads global state or
draws from the ``random`` module (``scale`` uses its own ``random.Random(seed)``).
Policies receive shared document dicts and must not mutate them.

Testing side: imports the dispatch framework, never a policy module.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from apps.container_logistics.policies.dispatch.allowed import PartitionRule
from apps.container_logistics.policies.dispatch.context import DispatchClock, DispatchContext
from apps.container_logistics.policies.dispatch.helpers.candidates import CandidateSettings
from apps.container_logistics.policies.dispatch.runner import DispatchRunner, framework_accounting_cost

logger = logging.getLogger(__name__)

__all__ = [
    "Fixture",
    "empty_trucks",
    "empty_orders",
    "single_truck",
    "no_geometry",
    "one_haulier",
    "all_infeasible",
    "tied_costs",
    "scale",
    "SMALL_FIXTURES",
    "ROBUSTNESS_FIXTURES",
    "synthetic_fixtures",
]

SIM_CLOCK = "Wed, 01 Jan 2020 08:00:00 GMT"
_BASE_LON, _BASE_LAT = 103.85, 1.30


@dataclass(frozen=True)
class Fixture:
    """One synthetic dispatch scope (partition topology).

    ``tiebreak_seed`` is the seed a caller should inject (``None`` = the legacy
    partitioned path); ``seed`` seeds the RNGs a caller injects (``Random(seed)``).
    """

    id: str
    seed: int
    trucks: tuple[dict[str, Any], ...]
    orders: tuple[dict[str, Any], ...]
    rule: PartitionRule
    partition_key: tuple[str, Any]
    settings: CandidateSettings = field(default_factory=lambda: CandidateSettings.from_profile({}))
    time_step: int = 0
    tiebreak_seed: int | None = None

    def context(
        self,
        runner: DispatchRunner,
        *,
        trucks: list[dict[str, Any]] | None = None,
        orders: list[dict[str, Any]] | None = None,
    ) -> DispatchContext:
        """A fresh context for ``runner``'s policy; ``trucks``/``orders`` override the order only."""
        return runner.partition_context(
            trucks=list(self.trucks if trucks is None else trucks),
            orders=list(self.orders if orders is None else orders),
            partition_key=self.partition_key,
            rule=self.rule,
            settings=self.settings,
            accounting_cost=framework_accounting_cost(runner.resolved.params),
            clock=DispatchClock(sim_clock=SIM_CLOCK, time_step=self.time_step),
        )


def _pt(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid: str, haulier: str, loc: dict[str, Any] | None, *,
           last_dropoff: dict[str, Any] | None = None, last_facility: str | None = None) -> dict[str, Any]:
    prof: dict[str, Any] = {"haulier_id": haulier, "truck_size": "40ft", "restricted_areas": []}
    if loc is not None:
        prof["current_loc"] = loc
    if last_dropoff is not None:
        prof["last_dropoff_loc"] = last_dropoff
    if last_facility is not None:
        prof["last_dropoff_facility_name"] = last_facility
    return {"_id": tid, "state": "online", "haulier_id": haulier, "profile": prof}


def _order(oid: str, haulier: str, pickup: dict[str, Any] | None, dropoff: dict[str, Any] | None, *,
           pickup_facility_name: str = "fac_p", dropoff_facility_name: str = "fac_d") -> dict[str, Any]:
    prof: dict[str, Any] = {
        "haulier_id": haulier,
        "pickup_facility_name": pickup_facility_name,
        "dropoff_facility_name": dropoff_facility_name,
        "order_size": "1x20",
    }
    doc: dict[str, Any] = {"_id": oid, "state": "unassigned", "haulier_id": haulier,
                           "order_size": "1x20", "profile": prof}
    if pickup is not None:
        doc["pickup_loc"] = pickup
        prof["pickup_loc"] = pickup
    if dropoff is not None:
        doc["dropoff_loc"] = dropoff
        prof["dropoff_loc"] = dropoff
    return doc


def _own_rule(max_travel_time_pickup: Any = None) -> PartitionRule:
    return PartitionRule(max_travel_time_pickup=max_travel_time_pickup, adjacency={})


def _line_trucks(n: int, haulier: str = "h_a", step: float = 0.01) -> list[dict[str, Any]]:
    return [_truck(f"t{i:03d}", haulier, _pt(_BASE_LON + i * step, _BASE_LAT)) for i in range(n)]


def _line_orders(n: int, haulier: str = "h_a", step: float = 0.012) -> list[dict[str, Any]]:
    return [
        _order(f"o{i:03d}", haulier, _pt(_BASE_LON + i * step, _BASE_LAT + 0.002),
               _pt(_BASE_LON + i * step + 0.05, _BASE_LAT))
        for i in range(n)
    ]


def empty_trucks() -> Fixture:
    """Orders but no trucks."""
    return Fixture(id="empty_trucks", seed=101, trucks=(), orders=tuple(_line_orders(5)),
                   rule=_own_rule(), partition_key=("haulier", "h_a"))


def empty_orders() -> Fixture:
    """Trucks but no orders."""
    return Fixture(id="empty_orders", seed=102, trucks=tuple(_line_trucks(5)), orders=(),
                   rule=_own_rule(), partition_key=("haulier", "h_a"))


def single_truck() -> Fixture:
    """One truck, three orders: at most one head."""
    return Fixture(id="single_truck", seed=103, trucks=tuple(_line_trucks(1)),
                   orders=tuple(_line_orders(3)), rule=_own_rule(), partition_key=("haulier", "h_a"))


def no_geometry() -> Fixture:
    """No truck anchor and no pickup point anywhere: every cost is ``inf``."""
    trucks = tuple(_truck(f"t{i:03d}", "h_a", None) for i in range(4))
    orders = tuple(_order(f"o{i:03d}", "h_a", None, None) for i in range(3))
    return Fixture(id="no_geometry", seed=104, trucks=trucks, orders=orders,
                   rule=_own_rule(), partition_key=("haulier", "h_a"))


def one_haulier() -> Fixture:
    """Eight trucks and six orders of one haulier at distinct distances."""
    return Fixture(id="one_haulier", seed=105, trucks=tuple(_line_trucks(8)),
                   orders=tuple(_line_orders(6)), rule=_own_rule(), partition_key=("haulier", "h_a"))


def all_infeasible() -> Fixture:
    """Every pair breaks ``max_travel_time_pickup=1`` (seconds): nothing may be assigned."""
    trucks = tuple(_truck(f"t{i:03d}", "h_a", _pt(_BASE_LON + 0.03 + i * 0.01, _BASE_LAT)) for i in range(4))
    return Fixture(id="all_infeasible", seed=106, trucks=trucks, orders=tuple(_line_orders(3)),
                   rule=_own_rule(max_travel_time_pickup=1), partition_key=("haulier", "h_a"))


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
    rule = PartitionRule(max_travel_time_pickup=None, adjacency={"h_a": {"h_b"}, "h_b": {"h_a"}})
    return Fixture(id="tied_costs", seed=107, trucks=trucks, orders=orders, rule=rule,
                   partition_key=("component", 0))


def scale(n_trucks: int = 1000, n_orders: int = 500, hauliers: int = 1, seed: int = 0) -> Fixture:
    """A dense synthetic tick: ``n_trucks`` x ``n_orders`` over 60 facilities.

    Orders pick up and drop off at facilities; 30% of trucks have just dropped off at
    some order's pickup (cost 0, the tie case), the rest sit at a random facility or
    a random point. More than one haulier forms one fully-partnered component. The
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
    if hauliers == 1:
        rule, key = _own_rule(), ("haulier", hids[0])
    else:
        rule = PartitionRule(max_travel_time_pickup=None,
                             adjacency={h: {p for p in hids if p != h} for h in hids})
        key = ("component", 0)
    default = (n_trucks, n_orders, hauliers, seed) == (1000, 500, 1, 0)
    fid = "scale" if default else f"scale_{n_trucks}x{n_orders}x{hauliers}_s{seed}"
    return Fixture(id=fid, seed=seed, trucks=tuple(trucks), orders=tuple(orders), rule=rule,
                   partition_key=key, time_step=7)


# Non-empty, small: the structure group decides on each of these.
SMALL_FIXTURES: tuple[Callable[[], Fixture], ...] = (
    single_truck, no_geometry, one_haulier, all_infeasible, tied_costs,
)
# One robustness check per fixture, in this order.
ROBUSTNESS_FIXTURES: tuple[Callable[[], Fixture], ...] = (
    empty_trucks, empty_orders, single_truck, no_geometry, one_haulier, all_infeasible, tied_costs, scale,
)


def synthetic_fixtures() -> list[Fixture]:
    """Every synthetic fixture once, in ``ROBUSTNESS_FIXTURES`` order."""
    return [make() for make in ROBUSTNESS_FIXTURES]
