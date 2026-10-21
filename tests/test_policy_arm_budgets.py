"""Per-arm performance budgets name real arms, and the measured arms declare one.

What is locked (round 2, ``docs/plans/policy_slots_round2_whole_tick.md`` §5 C5): every
``arm_budgets_ms`` key of every registered dispatch policy names an arm the battery
actually runs (``battery.policy_arms``) -- registration validates only str-choice
keys (message 17), so a ref-arm key such as ``inner=GreedyNeerest`` is caught here;
GreedyNearest ``candidates=sampled`` and every non-default Partitioned and
PooledMarket arm carry a measured budget; and the battery gates an arm by its measured
``arm_budgets_ms`` key when there is one (``battery.arm_budget``; an unmeasured composite
``inner=X`` arm is derived, see ``test_policy_final_review_battery.py``).

Why: a budget keyed on a typo is silently never applied, so the arm it was measured
for falls back to the default arm's budget and either fails for no visible reason or,
worse, a slow arm keeps passing under a stale number.

Verified to have teeth (each mutation restored after, verified by hash): renaming
PooledMarket's ``"inner=GreedyNearest"`` key to ``"inner=GreedyNeerest"`` failed
``test_arm_budget_keys_name_real_arms`` and
``test_measured_arms_declare_a_budget[PooledMarket]`` (and the gating test); making ``battery.arm_budget``
ignore ``arm_budgets_ms`` failed ``test_the_battery_gates_an_arm_by_its_own_budget``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.registry import get_policy_spec, isolated_registry, list_policies
from apps.container_logistics.policies.testing import battery


def _unknown_budget_keys(spec):
    arms = {battery.arm_label(arm) for arm in battery.policy_arms(spec)[1:]}
    return sorted(set(spec.arm_budgets_ms) - arms)


def test_arm_budget_keys_name_real_arms():
    bad = {spec.name: _unknown_budget_keys(spec) for spec in list_policies("dispatch") if _unknown_budget_keys(spec)}
    assert not bad, f"arm_budgets_ms keys that name no battery arm: {bad}"


def test_a_ref_budget_key_naming_no_target_is_caught():
    """Registration accepts a ref-param key unchecked (discovery order); this check catches it."""
    with isolated_registry():
        @policy(slot="dispatch", information_model="central", name="TypoBudget", version="1",
                description="a ref-arm budget with a typo", arm_budgets_ms={"offer=OfferNoen": 900})
        class TypoBudget(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                offer: PolicyRef = param(ref("OfferAll"), kind="market.offer", help="unused")

            def decide(self, ctx):
                return Plan(chains={})

        assert _unknown_budget_keys(get_policy_spec("dispatch", "TypoBudget")) == ["offer=OfferNoen"]


_MEASURED = {
    "GreedyNearest": {"candidates=sampled"},
    "Partitioned": {"inner=GreedyNearest"},
    "PooledMarket": {"offer=OfferNone", "offer=OfferSpare", "claim=ClaimIfGainExceeds", "claim=ClaimNone",
                     "arbitration=HighestBenefit", "arbitration=OwnerFirst", "arbitration=RandomAward",
                     "inner=GreedyNearest"},
}


@pytest.mark.parametrize("name", sorted(_MEASURED))
def test_measured_arms_declare_a_budget(name):
    spec = get_policy_spec("dispatch", name)
    assert set(spec.arm_budgets_ms) == _MEASURED[name]
    assert all(ms >= 500 for ms in spec.arm_budgets_ms.values()), "the rule's floor is 500 ms"


def test_the_battery_gates_an_arm_by_its_own_budget():
    spec = get_policy_spec("dispatch", "GreedyNearest")
    budget, source = battery.arm_budget(spec, {"candidates": "sampled"})
    assert (budget, source) == (spec.arm_budgets_ms["candidates=sampled"], "arm_budgets_ms['candidates=sampled']")
    assert battery.arm_budget(spec, {})[0] == spec.perf_budget_ms
    pooled = get_policy_spec("dispatch", "PooledMarket")
    assert battery.arm_budget(pooled, {"inner": {"type": "GreedyNearest"}})[0] == pooled.arm_budgets_ms["inner=GreedyNearest"]
