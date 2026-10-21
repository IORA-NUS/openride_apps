"""Every built-in dispatch policy passes every check of the contract battery, on every arm.

What is locked: ``run_battery("dispatch", name)`` returns exactly the ids of
``battery_check_ids("dispatch", name)``, in order -- ``DISPATCH_CHECKS`` for the
default params, then ``ARM_CHECKS`` once per arm as ``<check>[<arm>]`` -- and each is
``ok`` for all four built-ins (GreedyNearest, RandomAssignment, Partitioned,
PooledMarket) on whole ticks (round 2, ``docs/plans/policy_slots_round2_whole_tick.md``
§5 C5). The arms are the non-default choices of each policy's own ``str`` params and,
for each policy reference, every other available target (so ``Partitioned`` is
certified with ``inner=GreedyNearest`` and ``PooledMarket`` with every other offer,
claim and arbitration component and ``inner=GreedyNearest``), each against its own
measured budget. The whole battery over the four built-ins runs in under 120 s.

Why: the battery is the gate an author runs before selecting a policy
(``openride policy validate``); a built-in that failed it -- on any arm a scenario
can select -- would make the gate meaningless, and a battery too slow to run would
not be run. Each policy's battery runs once (module cache); the parametrisation
only reads its results, so each check reports separately under a stable id.
"""

from __future__ import annotations

import functools
import time

import pytest

from apps.container_logistics.policies.framework.registry import list_policies
from apps.container_logistics.policies.testing.battery import (
    ARM_CHECKS,
    DISPATCH_CHECKS,
    battery_check_ids,
    run_battery,
)

BUILTINS = ("GreedyNearest", "Partitioned", "PooledMarket", "RandomAssignment")
_RUNTIME_BUDGET_S = 120.0
_NAMES = [spec.name for spec in list_policies("dispatch")]
_CASES = [(name, check) for name in _NAMES for check in battery_check_ids("dispatch", name)]
_ELAPSED: dict[str, float] = {}


@functools.lru_cache(maxsize=None)
def _results(name: str) -> dict[str, object]:
    start = time.perf_counter()
    out = {r.id: r for r in run_battery("dispatch", name)}
    _ELAPSED[name] = time.perf_counter() - start
    return out


def test_the_four_builtins_are_registered():
    assert set(BUILTINS) <= set(_NAMES)


@pytest.mark.parametrize("name", _NAMES)
def test_battery_returns_exactly_the_listed_ids_in_order(name):
    got = list(_results(name))
    want = battery_check_ids("dispatch", name)
    assert got == want, f"run_battery({name!r}) must return one result per battery_check_ids id, in order"
    assert want[: len(DISPATCH_CHECKS)] == list(DISPATCH_CHECKS), "the default arm keeps the stable ids"


_WANT_ARMS = {
    "GreedyNearest": ["candidates=sampled"],
    "RandomAssignment": ["candidates=spatial"],
    "Partitioned": ["inner=GreedyNearest"],
    "PooledMarket": ["offer=OfferNone", "offer=OfferSpare", "claim=ClaimIfGainExceeds", "claim=ClaimNone",
                     "arbitration=HighestBenefit", "arbitration=OwnerFirst", "arbitration=RandomAward",
                     "inner=GreedyNearest"],
}


@pytest.mark.parametrize("name", BUILTINS)
def test_builtins_have_their_arms_in_the_battery(name):
    want = [f"{cid}[{arm}]" for arm in _WANT_ARMS[name] for cid in ARM_CHECKS]
    assert battery_check_ids("dispatch", name)[len(DISPATCH_CHECKS):] == want, (
        f"{name} must be battery-checked on {_WANT_ARMS[name]}, each arm with every ARM_CHECKS id"
    )


@pytest.mark.parametrize(("name", "check"), _CASES, ids=[f"{n}-{c}" for n, c in _CASES])
def test_builtin_dispatch_policy_passes(name, check):
    result = _results(name)[check]
    assert result.ok, f"{name} fails {check}: {result.detail}"


def test_battery_over_the_builtins_runs_in_under_two_minutes():
    for name in BUILTINS:
        _results(name)
    total = sum(_ELAPSED[name] for name in BUILTINS)
    assert total < _RUNTIME_BUDGET_S, (
        f"the battery over {BUILTINS} took {total:.1f} s ({ {n: round(_ELAPSED[n], 1) for n in BUILTINS} }); "
        f"budget {_RUNTIME_BUDGET_S:.0f} s"
    )
