"""Every built-in dispatch policy passes every check of the contract battery, on every arm.

What is locked: ``run_battery("dispatch", name)`` returns exactly the ids of
``battery_check_ids("dispatch", name)``, in order -- ``DISPATCH_CHECKS`` for the
default params, then ``ARM_CHECKS`` once per non-default ``choices`` value as
``<check>[<param>=<value>]`` -- and each is ``ok`` for every registered dispatch
policy (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.2, P3 tests). So
GreedyNearest ``candidates=sampled`` and RandomAssignment ``candidates=spatial`` are
certified, not just the defaults.

Why: the battery is the gate an author runs before selecting a policy
(``openride policy validate``); a built-in that failed it -- on any arm a scenario
can select -- would make the gate meaningless. The battery runs once per policy
(module-scoped cache); the parametrisation only reads its results, so each check
reports separately under a stable id.
"""

from __future__ import annotations

import functools

import pytest

from apps.container_logistics.policies.framework.registry import list_policies
from apps.container_logistics.policies.testing.battery import (
    ARM_CHECKS,
    DISPATCH_CHECKS,
    battery_check_ids,
    run_battery,
)

_NAMES = [spec.name for spec in list_policies("dispatch")]
_CASES = [(name, check) for name in _NAMES for check in battery_check_ids("dispatch", name)]


@functools.lru_cache(maxsize=None)
def _results(name: str) -> dict[str, object]:
    return {r.id: r for r in run_battery("dispatch", name)}


@pytest.mark.parametrize("name", _NAMES)
def test_battery_returns_exactly_the_listed_ids_in_order(name):
    got = list(_results(name))
    want = battery_check_ids("dispatch", name)
    assert got == want, f"run_battery({name!r}) must return one result per battery_check_ids id, in order"
    assert want[: len(DISPATCH_CHECKS)] == list(DISPATCH_CHECKS), "the default arm keeps the stable ids"


def test_both_builtins_have_their_non_default_candidates_arm_in_the_battery():
    ids = {name: battery_check_ids("dispatch", name) for name in ("GreedyNearest", "RandomAssignment")}
    for name, arm in (("GreedyNearest", "candidates=sampled"), ("RandomAssignment", "candidates=spatial")):
        want = [f"{cid}[{arm}]" for cid in ARM_CHECKS]
        assert ids[name][len(DISPATCH_CHECKS):] == want, f"{name} must be battery-checked on {arm}"


@pytest.mark.parametrize(("name", "check"), _CASES, ids=[f"{n}-{c}" for n, c in _CASES])
def test_builtin_dispatch_policy_passes(name, check):
    result = _results(name)[check]
    assert result.ok, f"{name} fails {check}: {result.detail}"
