"""The battery's generic ``contract.*`` checks (plan ``plan_contract_final.md`` §7.9, WS4b).

What is locked: ``contract.world_reads_at_bind`` fails a policy whose ``solve_tick`` reads
a run-world key no ``bind`` read (and nothing else); ``contract.bind_pure`` fails a
``bind`` that draws from the ``random`` module (and nothing else);
``contract.declaration_within_ceiling`` reports a top whose declared cross-company pairs
lie outside the scenario's ceiling; ``contract.scope_audit`` is SKIP (with the reason)
for a policy that defines no ``audit_scopes``.

Why: these replace the round-2 information-model audit. A key read only while solving
is not in the runner's re-bind digest, an impure bind differs between the sim main and
the agent, and a declaration outside the ceiling is refused at run start (message 23).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.bind import Declaration
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyParams, policy
from apps.container_logistics.policies.framework.registry import get_policy_spec, isolated_registry
from apps.container_logistics.policies.testing import battery
from apps.container_logistics.policies.testing.battery import battery_check_ids, run_battery


def _results(name):
    results = run_battery("planner", name)
    assert [r.id for r in results] == battery_check_ids("planner", name)
    return {r.id: r for r in results}


def _empty_params():
    @dataclass(frozen=True)
    class Params(PolicyParams):
        pass

    return Params


def test_a_key_read_only_while_solving_fails_only_world_reads_at_bind():
    """Verified to have teeth: ``_world_reads_at_bind`` returning ``(True, "")`` made the
    failing set empty."""
    with isolated_registry():
        @policy(slot="planner", name="LateReader", version="1", description="reads the world only at solve")
        class LateReader(DispatchPolicy):
            Params = _empty_params()

            def solve_tick(self, tick, params):
                tick.world.get("max_travel_time_pickup")  # never read by bind
                return Result(pairs=())

        results = _results("LateReader")
        assert {i for i, r in results.items() if not r.ok} == {"contract.world_reads_at_bind"}
        assert "max_travel_time_pickup" in results["contract.world_reads_at_bind"].detail
        scope = results["contract.scope_audit"]
        assert scope.status == "SKIP" and "defines no audit_scopes" in scope.detail, scope.detail


def test_a_bind_that_draws_from_the_random_module_fails_only_bind_pure():
    """Verified to have teeth: dropping the ``random.getstate()`` comparison in
    ``_bind_pure`` made the failing set empty."""
    with isolated_registry():
        @policy(slot="planner", name="ImpureBind", version="1", description="draws at bind")
        class ImpureBind(DispatchPolicy):
            Params = _empty_params()

            def bind(self, params, world, *, top):
                random.random()
                return Declaration()

            def solve_tick(self, tick, params):
                return Result(pairs=())

        results = _results("ImpureBind")
        assert {i for i, r in results.items() if not r.ok} == {"contract.bind_pure"}
        assert "drew from the random module" in results["contract.bind_pure"].detail


def test_a_declaration_outside_the_ceiling_is_reported():
    """Verified to have teeth: skipping the ceiling comparison and the refused-binding
    branch (``continue`` without a problem) made the check pass."""
    with isolated_registry():
        @policy(slot="planner", name="OverDeclarer", version="1", description="declares a pair no structure has")
        class OverDeclarer(DispatchPolicy):
            Params = _empty_params()

            def bind(self, params, world, *, top):
                world.get("cooperation")
                return Declaration(cross_company={("acme", "zeta")} if world.get("cooperation") else frozenset())

            def solve_tick(self, tick, params):
                return Result(pairs=())

        ok, detail = battery._declaration_within_ceiling(get_policy_spec("planner", "OverDeclarer"), {})
        assert not ok and "('acme', 'zeta')" in detail, detail
