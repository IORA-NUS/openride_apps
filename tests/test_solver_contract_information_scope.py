"""``contract.information_scope`` FAILS when the measured containment contradicts the badge.

Final review round 2, G4: the check only ever reported the measurement, so a policy
whose badge claims a decomposition passed while its sub-ticks showed an inner the whole
fleet. ``battery._unsplit_fleets`` now makes that a FAIL, measured and keyed on no
badge text: on a tick whose truck carriers the fixture's ceiling separates into two or
more groups, a sub-tick holding EVERY truck of the tick contradicts a decomposition
badge. The reviewer's two probes (``r2_fb/probe5``) are pinned here, next to the honest
shapes that must keep their status:

- ``BadgeLiar`` -- badge ``per company``, ONE ``restrict`` over the whole tick, an
  ``audit_scopes`` that returns ``[]``: was PASS ("saw 4 of 4 hauliers"), now FAIL;
- ``LeakPerCompany`` -- badge ``per company``, one sub-tick per order owner, each with
  ALL the trucks (the all-trucks-per-subtick leak): was PASS, now FAIL;
- ``HonestZone`` -- badge ``per zone``, the tick split by a key that is not the
  company (each zone holds trucks of every carrier, but never the whole fleet): PASS;
- ``HonestCentral`` -- badge ``central``, no sub-tick: SKIP.

The built-ins keep their rows (``Partitioned``: a ceiling-joined pair may be ONE
sub-tick; ``PooledMarket``: one carrier per sub-tick): the valtable is unchanged, and
:func:`test_the_built_in_decompositions_still_pass` pins it on the default arms.

Verified to have teeth (R3B; mutate / see it fail / restore): making
``_unsplit_fleets`` return ``[]`` failed the two liar tests; dropping its
``n_trucks < len(...)`` guard (every multi-carrier sub-tick a contradiction) failed
``HonestZone`` and the built-in test (``Partitioned`` on ``three_company_chain``).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyParams, policy
from apps.container_logistics.policies.framework.registry import discover_builtin_policies, isolated_registry
from apps.container_logistics.policies.testing.battery import run_battery
from tests.test_policy_battery_catches_broken_policies import _first_feasible


def _params():
    @dataclass(frozen=True)
    class Params(PolicyParams):
        pass

    return Params


def _scope_rows(name: str) -> dict[str, tuple[str, str]]:
    return {r.id: (r.status, r.detail) for r in run_battery("planner", name) if r.id.startswith("contract.")}


def _zone(doc) -> int:
    return sum(map(ord, str(doc["_id"]))) % 2


def _register(name: str, badge: str, solve, audit: bool = True):
    attrs = {"Params": _params(), "solve_tick": lambda self, tick, params: solve(tick)}
    if audit:
        attrs["audit_scopes"] = lambda self, scope_log, tick: []
    cls = type(name, (DispatchPolicy,), attrs)
    policy(slot="planner", name=name, version="1", description="information-scope probe", badge=badge)(cls)


def _whole_tick_liar(tick):
    tick.restrict(trucks=list(tick.raw_trucks), orders=list(tick.raw_orders), scope="company:all",
                  sample_rng=tick.rng)
    return Result(pairs=tuple((t, c[0]) for t, c in _first_feasible(tick).items()))


def _all_trucks_leak(tick):
    owners = sorted({assign_constraints.haulier_of(o) for o in tick.raw_orders} - {None})
    for h in owners:
        tick.restrict(trucks=list(tick.raw_trucks),
                      orders=[o for o in tick.raw_orders if assign_constraints.haulier_of(o) == h],
                      scope=f"company:{h}", sample_rng=tick.rng)
    return Result(pairs=tuple((t, c[0]) for t, c in _first_feasible(tick).items()))


def _honest_zone(tick):
    pairs = []
    for z in (0, 1):
        sub = tick.restrict(trucks=[t for t in tick.raw_trucks if _zone(t) == z],
                            orders=[o for o in tick.raw_orders if _zone(o) == z],
                            scope=f"zone:{z}", sample_rng=tick.rng)
        pairs += [(t, c[0]) for t, c in _first_feasible(sub, allowed=tick.permitted).items()]
    return Result(pairs=tuple(pairs))


def _central(tick):
    return Result(pairs=tuple((t, c[0]) for t, c in _first_feasible(tick).items()))


@pytest.mark.parametrize(("name", "solve"), [("BadgeLiar", _whole_tick_liar), ("LeakPerCompany", _all_trucks_leak)])
def test_a_decomposition_badge_over_an_unsplit_fleet_fails(name, solve):
    with isolated_registry():
        _register(name, "per company", solve)
        rows = _scope_rows(name)
    status, detail = rows["contract.information_scope"]
    assert status == "FAIL", rows["contract.information_scope"]
    assert "three_company_chain" in detail and "held all 12 trucks of 4 carriers" in detail, detail
    # Its own audit says nothing is wrong: only the framework's measurement catches it.
    assert rows["contract.scope_audit"][0] == "PASS", rows["contract.scope_audit"]


def test_an_honest_split_by_another_key_passes():
    with isolated_registry():
        _register("HonestZone", "per zone", _honest_zone)
        rows = _scope_rows("HonestZone")
    assert rows["contract.information_scope"][0] == "PASS", rows["contract.information_scope"]
    assert rows["contract.scope_audit"][0] == "PASS", rows["contract.scope_audit"]


def test_a_central_policy_is_still_skipped():
    with isolated_registry():
        _register("HonestCentral", "central", _central, audit=False)
        rows = _scope_rows("HonestCentral")
    assert rows["contract.information_scope"][0] == "SKIP", rows["contract.information_scope"]


@pytest.mark.parametrize("name", ["Partitioned", "PooledMarket"])
def test_the_built_in_decompositions_still_pass(name):
    discover_builtin_policies()
    rows = {r.id: r.status for r in run_battery("planner", name) if r.id.startswith("contract.information_scope")}
    assert rows and set(rows.values()) == {"PASS"}, rows
