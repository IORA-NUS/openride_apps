"""Every built-in dispatch policy passes every check of the contract battery, on every arm.

What is locked: ``run_battery("planner", name)`` returns exactly the ids of
``battery_check_ids("planner", name)``, in order -- ``DISPATCH_CHECKS`` for the
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

from apps.container_logistics.policies.framework.components import ref_names
from apps.container_logistics.policies.framework.params import declared_params
from apps.container_logistics.policies.framework.registry import get_policy_spec, list_policies
from apps.container_logistics.policies.testing.battery import (
    ARM_CHECKS,
    DISPATCH_CHECKS,
    PLANNING_ARM_CHECKS,
    battery_check_ids,
    run_battery,
)

BUILTINS = ("GreedyNearest", "Partitioned", "PooledMarket", "RandomAssignment")
_RUNTIME_BUDGET_S = 120.0
_NAMES = [spec.name for spec in list_policies("planner")]
_CASES = [(name, check) for name in _NAMES for check in battery_check_ids("planner", name)]
_ELAPSED: dict[str, float] = {}


@functools.lru_cache(maxsize=None)
def _results(name: str) -> dict[str, object]:
    start = time.perf_counter()
    out = {r.id: r for r in run_battery("planner", name)}
    _ELAPSED[name] = time.perf_counter() - start
    return out


def test_the_four_builtins_are_registered():
    assert set(BUILTINS) <= set(_NAMES)


@pytest.mark.parametrize("name", _NAMES)
def test_battery_returns_exactly_the_listed_ids_in_order(name):
    got = list(_results(name))
    want = battery_check_ids("planner", name)
    assert got == want, f"run_battery({name!r}) must return one result per battery_check_ids id, in order"
    # A planning top adds PLANNING_ARM_CHECKS to its default arm; the dispatch ids
    # themselves keep their stable order around them.
    stable = [c for c in want if c not in PLANNING_ARM_CHECKS]
    assert stable[: len(DISPATCH_CHECKS)] == list(DISPATCH_CHECKS), "the default arm keeps the stable ids"


# What a built-in's arms must be. Intent (final review MINOR-7): a NEW ARM OF A BUILT-IN
# MUST BE ACKNOWLEDGED HERE -- a new str choice, a new ref param, a reordered param. So
# each built-in's own str-choice arms and its ref params (in declared order) are
# written out by hand. The TARGETS of a ref param are NOT: they are every other
# available target in the live registry (``_ref_targets``), so registering a new leaf
# (a NetworkFlow solver) or a new component adds its ``inner=X`` / ``offer=X`` arm
# without editing this table. ``_BUILTIN_TARGETS`` still pins that every shipped
# target IS an arm, so dropping one is caught.
_WANT_ARMS = {
    "GreedyNearest": ["candidates=sampled"],
    "RandomAssignment": ["candidates=spatial"],
    "Partitioned": [("inner", "planner")],
    "PooledMarket": [("offer", "market.offer"), ("claim", "market.claim"),
                     ("arbitration", "market.arbitration"), ("inner", "planner")],
}
_BUILTIN_TARGETS = {
    "Partitioned": ["inner=GreedyNearest"],
    "PooledMarket": ["offer=OfferNone", "offer=OfferSpare", "claim=ClaimIfGainExceeds", "claim=ClaimNone",
                     "arbitration=HighestBenefit", "arbitration=OwnerFirst", "arbitration=RandomAward",
                     "inner=GreedyNearest"],
}


def _ref_targets(name: str, param: str, kind: str) -> list[str]:
    """Every other available target of ``name``'s ref ``param``, sorted, read from the registry."""
    info = next(i for i in declared_params(get_policy_spec("planner", name).params_cls) if i.name == param)
    if kind == "planner":
        names = [s.name for s in list_policies("planner")
                 if not s.missing_requirements() and not s.top_only
                 and not (info.meta.leaf_only and s.is_composite)]
    else:
        names = ref_names(kind)
    return [f"{param}={t}" for t in sorted(names) if t != info.default.type]


def expected_arms(name: str) -> list[str]:
    """``_WANT_ARMS[name]`` with each ``(ref param, kind)`` expanded from the live registry."""
    out: list[str] = []
    for entry in _WANT_ARMS[name]:
        out += _ref_targets(name, *entry) if isinstance(entry, tuple) else [entry]
    return out


@pytest.mark.parametrize("name", BUILTINS)
def test_builtins_have_their_arms_in_the_battery(name):
    want_arms = expected_arms(name)
    want = [f"{cid}[{arm}]" for arm in want_arms for cid in ARM_CHECKS]
    assert battery_check_ids("planner", name)[len(DISPATCH_CHECKS):] == want, (
        f"{name} must be battery-checked on {want_arms}, each arm with every ARM_CHECKS id"
    )
    assert set(_BUILTIN_TARGETS.get(name, [])) <= set(want_arms), "every shipped target is an arm"


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
