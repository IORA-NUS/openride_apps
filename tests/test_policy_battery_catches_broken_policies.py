"""The contract battery fails a broken policy on exactly the check that names its bug.

What is locked: deliberately broken dispatch policies, each registered under
``isolated_registry()``, fail precisely the expected battery checks and nothing
else (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3 tests; round 2 §5 C5, whole
ticks) -- including a policy broken ONLY under a non-default ``choices`` value, which
fails that arm's check while its default arm passes; a policy that draws from the
``random`` module; a ``per_company`` market whose company scopes leak other companies'
trucks (``LeakyMarket``: only ``information.scopes_respect_model``); a central policy
that ignores the cooperation structure (``CrossCompanyCentral``: only
``structure.only_allowed_pairs``); and an arm slower than the budget it inherits
because nobody measured it (``SlowUndeclaredArm``: only that arm's
``performance.scale``, and declaring a measured ``arm_budgets_ms`` entry clears it)
-- and a policy whose ``Params`` defaults break their own bounds cannot be
registered at all.

Why: a battery that passes everything proves nothing. Each broken policy is a
correct baseline (orders and trucks taken in id order, each order to the first free
allowed truck: deterministic and order-independent) with ONE defect added, so a
check failing elsewhere would mean the battery mis-attributes a bug.

Verified to have teeth (each mutation in ``policies/testing/battery.py``, restored
after): mapping the ``no_order_twice`` message kind to ``only_in_scope`` in
``_PLAN_MESSAGE_KINDS`` failed ``test_twice_assigner_fails_only_no_order_twice``;
mapping ``only_allowed_pairs`` to ``returns_plan`` failed
``test_disallowed_pair_policy_fails_only_only_allowed_pairs``; mapping the raised
"appears twice in pairs" kind to ``returns_plan`` failed
``test_double_truck_from_pairs_fails_only_no_truck_twice``; comparing
``best <= spec.perf_budget_ms * 10`` failed ``test_slow_policy_fails_only_performance_scale``;
treating every ``error`` as ok in ``_robustness_check`` failed
``test_crash_on_empty_policy_fails_only_the_empty_robustness_checks``; comparing
only plan lengths in ``_reproducible`` failed
``test_clock_seeded_policy_fails_determinism_reproducible``; returning ``[{}]`` from
``policy_arms`` failed ``test_policy_broken_only_under_a_non_default_choice_fails_that_arm``;
and ``touched = False`` in ``_decide_via_runner`` failed
``test_module_rng_policy_fails_only_no_module_rng``. Round 2 C5 (each mutation in
``battery.py`` restored after, verified by hash): returning ``[]`` from
``_scope_problems`` failed ``test_leaky_market_fails_only_scopes_respect_model``;
making the tick context's feasibility always true (``allowed_raw=lambda t, o: True``
in ``scopes.tick_context``, a scratch copy) failed
``test_cross_company_central_fails_only_only_allowed_pairs``;
``arm_budget`` returning a budget of 10x the default for unmeasured arms failed
``test_slow_undeclared_arm_fails_only_its_performance_scale``.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch import scopes
from apps.container_logistics.policies.dispatch.allowed import CompanyRule
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, param, policy
from apps.container_logistics.policies.framework.errors import PolicyRegistrationError
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.testing.battery import DISPATCH_CHECKS, battery_check_ids, run_battery


def _first_feasible(ctx, trucks=None, allowed=None):
    """Each order (by id) takes the first free allowed truck (by id, unless given)."""
    trucks = sorted(ctx.raw_trucks, key=lambda t: str(t["_id"])) if trucks is None else trucks
    allowed = ctx.allowed_raw if allowed is None else allowed
    used, chains = set(), {}
    for o in sorted(ctx.raw_orders, key=lambda o: str(o["_id"])):
        for t in trucks:
            if t["_id"] not in used and allowed(t, o):
                used.add(t["_id"])
                chains[str(t["_id"])] = (str(o["_id"]),)
                break
    return chains


def _failing(name):
    results = run_battery("planner", name)
    assert [r.id for r in results] == battery_check_ids("planner", name)
    return {r.id for r in results if not r.ok}


def test_twice_assigner_fails_only_no_order_twice():
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="TwiceAssigner", version="1", description="puts one order in two chains")
        class TwiceAssigner(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                chains = _first_feasible(ctx)
                if len(chains) >= 2:
                    (t1, c1), (t2, c2) = list(chains.items())[:2]
                    chains[t2] = c2 + c1  # o1 again, in t2's tail
                return Plan(chains=chains)

        assert _failing("TwiceAssigner") == {"structure.no_order_twice"}


def test_disallowed_pair_policy_fails_only_only_allowed_pairs():
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="DisallowedPairPolicy", version="1", description="assigns infeasible pairs")
        class DisallowedPairPolicy(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                chains = _first_feasible(ctx)
                used_o = {c[0] for c in chains.values()}
                for t in sorted(ctx.raw_trucks, key=lambda t: str(t["_id"])):
                    if str(t["_id"]) in chains:
                        continue
                    for o in sorted(ctx.raw_orders, key=lambda o: str(o["_id"])):
                        if str(o["_id"]) not in used_o and not ctx.allowed_raw(t, o):
                            chains[str(t["_id"])] = (str(o["_id"]),)
                            used_o.add(str(o["_id"]))
                            break
                return Plan(chains=chains)

        assert _failing("DisallowedPairPolicy") == {"structure.only_allowed_pairs"}


def test_slow_policy_fails_only_performance_scale():
    budget_ms = 50

    with isolated_registry():
        @policy(slot="planner", information_model="central", name="SlowPolicy", version="1", description="sleeps past its budget at scale",
                perf_budget_ms=budget_ms)
        class SlowPolicy(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                if len(ctx.raw_trucks) >= 1000:  # only the scale fixture, so the test stays fast
                    time.sleep((budget_ms + 200) / 1000.0)
                return Plan(chains=_first_feasible(ctx))

        assert _failing("SlowPolicy") == {"performance.scale"}


def test_crash_on_empty_policy_fails_only_the_empty_robustness_checks():
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="CrashOnEmptyPolicy", version="1", description="raises on an empty scope")
        class CrashOnEmptyPolicy(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                if not ctx.raw_trucks or not ctx.raw_orders:
                    raise RuntimeError("nothing to dispatch")
                return Plan(chains=_first_feasible(ctx))

        assert _failing("CrashOnEmptyPolicy") == {"robustness.empty_trucks", "robustness.empty_orders"}


def test_clock_seeded_policy_fails_determinism_reproducible():
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="ClockSeededPolicy", version="1", description="seeds its RNG from the clock",
                # Declared, so order_independent is skipped and only the defect under test shows.
                order_sensitive="seeded from the wall clock")
        class ClockSeededPolicy(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                trucks = list(ctx.raw_trucks)
                random.Random(time.time_ns()).shuffle(trucks)  # the bug: never ctx.rng
                return Plan(chains=_first_feasible(ctx, trucks=trucks))

        failing = _failing("ClockSeededPolicy")
        assert "determinism.reproducible" in failing
        assert failing == {"determinism.reproducible"}, f"only determinism should fail, got {sorted(failing)}"


def test_double_truck_from_pairs_fails_only_no_truck_twice():
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="DoubleTruckPolicy", version="1", description="gives one truck two orders")
        class DoubleTruckPolicy(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                trucks = sorted(ctx.raw_trucks, key=lambda t: str(t["_id"]))
                orders = sorted(ctx.raw_orders, key=lambda o: str(o["_id"]))
                pairs = [(t, o) for t in trucks for o in orders if ctx.allowed_raw(t, o)][:2]
                if len(pairs) == 2 and pairs[0][0] is pairs[1][0]:
                    return Plan.from_pairs(pairs)  # raises: the first truck appears twice
                return Plan(chains=_first_feasible(ctx))

        assert _failing("DoubleTruckPolicy") == {"structure.no_truck_twice"}


def test_policy_broken_only_under_a_non_default_choice_fails_that_arm():
    """The reviewer's ``battery_defaults_only.py`` case (review F3): only ``candidates=sampled`` is broken."""
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="BrokenWhenSampled", version="1", description="double-assigns when sampled")
        class BrokenWhenSampled(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                candidates: str = param("spatial", choices=("spatial", "sampled"), help="candidate kind")

            def decide(self, ctx):
                chains = _first_feasible(ctx)
                orders = sorted(ctx.raw_orders, key=lambda o: str(o["_id"]))
                if ctx.params.candidates == "sampled" and orders:  # the bug lives only in this arm
                    first = orders[0]
                    chains = {str(t["_id"]): (str(first["_id"]),) for t in ctx.raw_trucks
                              if ctx.allowed_raw(t, first)}
                return Plan(chains=chains)

        assert _failing("BrokenWhenSampled") == {"structure.no_order_twice[candidates=sampled]"}, (
            "the defect must fail exactly the sampled arm's check; the default arm must pass"
        )


def test_module_rng_policy_fails_only_no_module_rng():
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="ModuleRngPolicy", version="1", description="draws from the random module")
        class ModuleRngPolicy(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                random.random()  # the bug: never ctx.rng
                return Plan(chains=_first_feasible(ctx))

        assert _failing("ModuleRngPolicy") == {"determinism.no_module_rng"}


def test_registering_bad_param_defaults_raises():
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match="defaults fail their own validation"):
            @policy(slot="planner", information_model="central", name="BadDefaults", version="1", description="default below its bound")
            class BadDefaults(DispatchPolicy):
                @dataclass(frozen=True)
                class Params(PolicyParams):
                    bonus_km: float = param(-1.0, ge=0, help="must be >= 0, default is not")

                def decide(self, ctx):
                    return Plan(chains={})


def test_leaky_market_fails_only_scopes_respect_model():
    """A ``per_company`` policy whose company scopes hold EVERY company's trucks.

    Its plan is a correct one (decided on the tick), so only the information audit
    of the scopes it built can see the leak.
    """
    with isolated_registry():
        @policy(slot="planner", information_model="per_company", name="LeakyMarket", version="1",
                description="company scopes that leak other companies' trucks")
        class LeakyMarket(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                hids = sorted({assign_constraints.haulier_of(o) for o in ctx.raw_orders} - {None})
                for hid in hids:
                    scopes.company_context(  # the leak: all trucks, not just hid's
                        policy=self, params=ctx.params, candidate_kind=ctx.default_candidate_kind,
                        trucks=list(ctx.raw_trucks),
                        orders=[o for o in ctx.raw_orders if assign_constraints.haulier_of(o) == hid],
                        haulier_id=hid, round=1,
                        rule=CompanyRule(haulier_id=hid, max_travel_time_pickup=ctx.world.max_travel_time_pickup),
                        rng=ctx.rng, settings=ctx.candidate_settings, accounting_cost=ctx.accounting_cost,
                        clock=ctx.clock, parent=ctx,
                    )
                return Plan(chains=_first_feasible(ctx))

        assert _failing("LeakyMarket") == {"information.scopes_respect_model"}


def test_cross_company_central_fails_only_only_allowed_pairs():
    """A central policy that lets any company serve any other's orders, ignoring the structure.

    Only ``three_company_chain`` exposes it (``h_d`` is solo, ``h_a``/``h_c`` are not
    partners); the framework's world rule on the tick scope is what refuses it.
    """
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="CrossCompanyCentral", version="1",
                description="serves across every company pair")
        class CrossCompanyCentral(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                def anyone(t, o):
                    th, oh = assign_constraints.haulier_of(t), assign_constraints.haulier_of(o)
                    return ctx.allowed_raw(t, o) or (th is not None and oh is not None and th != oh)

                return Plan(chains=_first_feasible(ctx, allowed=anyone))

        assert _failing("CrossCompanyCentral") == {"structure.only_allowed_pairs"}


def _slow_undeclared_arm(budgets=None):
    budget_ms = 500

    @policy(slot="planner", information_model="central", name="SlowUndeclaredArm", version="1",
            description="fast by default, slow on one arm", perf_budget_ms=budget_ms, arm_budgets_ms=budgets)
    class SlowUndeclaredArm(DispatchPolicy):
        @dataclass(frozen=True)
        class Params(PolicyParams):
            variant: str = param("a", choices=("a", "x"), help="x is slow at scale")

        def decide(self, ctx):
            if ctx.params.variant == "x" and len(ctx.raw_trucks) >= 1000:  # only the scale fixture
                time.sleep((budget_ms + 200) / 1000.0)
            return Plan(chains=_first_feasible(ctx))

    return SlowUndeclaredArm


def test_slow_undeclared_arm_fails_only_its_performance_scale():
    """An arm nobody measured inherits the default arm's budget, so being slower fails it loudly.

    (The spec names the arm ``candidates=x``; a ``candidates`` value must be a real
    candidate kind, so the arm here is ``variant=x``.) Declaring the arm's measured
    budget clears exactly that failure.
    """
    with isolated_registry():
        _slow_undeclared_arm()
        failing = _failing("SlowUndeclaredArm")
        assert failing == {"performance.scale[variant=x]"}, sorted(failing)
        detail = next(r.detail for r in run_battery("planner", "SlowUndeclaredArm")
                      if r.id == "performance.scale[variant=x]")
        assert "arm not measured" in detail and "budget 500 ms" in detail
    with isolated_registry():
        _slow_undeclared_arm({"variant=x": 2100})
        assert _failing("SlowUndeclaredArm") == set()
