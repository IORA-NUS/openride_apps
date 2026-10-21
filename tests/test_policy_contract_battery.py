"""Every built-in dispatch policy passes every check of the contract battery.

What is locked: ``run_battery("dispatch", name)`` returns exactly one result per id
in ``DISPATCH_CHECKS``, in order, and each is ``ok`` for every registered dispatch
policy (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.2, P3 tests).

Why: the battery is the gate an author runs before selecting a policy
(``openride policy validate``); a built-in that failed it would make the gate
meaningless. The battery runs once per policy (module-scoped cache); the
parametrisation only reads its results, so each check reports separately.
"""

from __future__ import annotations

import functools

import pytest

from apps.container_logistics.policies.framework.registry import list_policies
from apps.container_logistics.policies.testing.battery import DISPATCH_CHECKS, run_battery

_NAMES = [spec.name for spec in list_policies("dispatch")]


@functools.lru_cache(maxsize=None)
def _results(name: str) -> dict[str, object]:
    results = run_battery("dispatch", name)
    assert [r.id for r in results] == list(DISPATCH_CHECKS), (
        f"run_battery({name!r}) must return one result per DISPATCH_CHECKS id, in order"
    )
    return {r.id: r for r in results}


@pytest.mark.parametrize("check", DISPATCH_CHECKS)
@pytest.mark.parametrize("name", _NAMES)
def test_builtin_dispatch_policy_passes(name, check):
    result = _results(name)[check]
    assert result.ok, f"{name} fails {check}: {result.detail}"
