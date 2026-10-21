"""Asking a company scope for spatial pairs it never built is a contract violation, not ``[]``.

What is locked: on the pooled topology (``Scope.kind == "company"``) spatial pairs
exist only as the round's prebuilt slice, built only when the resolved candidate
kind is ``spatial``. ``ctx.candidate_pairs("spatial")`` on a company scope of any
other kind raises ``DispatchContractViolation`` naming the fix, so the runner logs
DISPATCH POLICY CONTRACT VIOLATION and discards the plan; a spatial-kind company
scope whose prebuilt slice is empty still returns ``[]``; partition scopes build
spatial pairs on demand and are unaffected.

Why: the old silent ``[]`` let a policy that asks for spatial pairs on a non-spatial
arm assign nothing on every pooled tick while the run reported ``ok: true``.

Verified to have teeth: removing the ``raise`` in ``DispatchContext._build_pairs``
failed ``test_spatial_pairs_on_a_sampled_company_scope_raise_naming_the_fix`` and
``test_the_runner_discards_the_plan_and_logs_the_violation``; dropping the
``self.scope.kind == "company"`` condition failed
``test_partition_scope_builds_spatial_pairs_on_demand``; dropping the
``default_candidate_kind != "spatial"`` condition failed
``test_spatial_company_scope_with_no_prebuilt_pairs_returns_empty``.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.context import DispatchClock
from apps.container_logistics.policies.dispatch.helpers.candidates import CandidateSettings
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy, framework_accounting_cost
from apps.container_logistics.policies.framework import PolicyParams, param, policy
from apps.container_logistics.policies.framework.errors import DispatchContractViolation
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.testing import fixtures as fx


@pytest.fixture
def asks_spatial():
    """A policy whose ``candidates`` defaults to ``sampled`` but whose decide asks for spatial pairs."""
    with isolated_registry():
        @policy(slot="dispatch", name="AsksSpatial", version="1", description="always reads spatial pairs")
        class AsksSpatial(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                candidates: str = param("sampled", choices=("sampled", "spatial"), help="candidate kind")

            def decide(self, ctx):
                pairs, used, chosen = ctx.candidate_pairs("spatial"), set(), []
                for t, o in pairs:
                    if t["_id"] not in used and o["_id"] not in {c[1]["_id"] for c in chosen}:
                        used.add(t["_id"])
                        chosen.append((t, o))
                return Plan.from_pairs(chosen)

        yield "AsksSpatial"


def _company_ctx(runner, prebuilt=None):
    f = fx.one_haulier()
    return runner.company_context(
        trucks=list(f.trucks), orders=list(f.orders), haulier_id="h_a", round=1,
        rule=runner.company_rule("h_a", None), rng=random.Random(1),
        settings=CandidateSettings.from_profile({}),
        accounting_cost=framework_accounting_cost(runner.resolved.params),
        clock=DispatchClock(sim_clock=fx.SIM_CLOCK, time_step=0), prebuilt_pairs=prebuilt,
    )


def test_spatial_pairs_on_a_sampled_company_scope_raise_naming_the_fix(asks_spatial):
    ctx = _company_ctx(build_runner_for_policy(asks_spatial))
    with pytest.raises(DispatchContractViolation, match=r"needs the resolved candidate kind 'spatial'"):
        ctx.candidate_pairs("spatial")
    assert ctx.candidate_pairs("sampled"), "the scope's own kind still works"


def test_the_runner_discards_the_plan_and_logs_the_violation(asks_spatial, caplog):
    runner = build_runner_for_policy(asks_spatial)
    with caplog.at_level(logging.ERROR):
        committed = runner.run(_company_ctx(runner))
    assert committed == [] and runner.stats["violations"] == 1
    assert any("DISPATCH POLICY CONTRACT VIOLATION" in r.getMessage()
               and "candidate_pairs('spatial')" in r.getMessage() for r in caplog.records), (
        "the violation must reach the greppable ERROR line"
    )


def test_spatial_company_scope_with_no_prebuilt_pairs_returns_empty(asks_spatial):
    runner = build_runner_for_policy(asks_spatial, {"candidates": "spatial"})
    ctx = _company_ctx(runner, prebuilt=[])
    assert ctx.candidate_pairs("spatial") == []
    assert runner.run(_company_ctx(runner, prebuilt=[])) == [] and runner.stats["violations"] == 0


def test_partition_scope_builds_spatial_pairs_on_demand(asks_spatial):
    runner = build_runner_for_policy(asks_spatial)  # kind 'sampled', partition scope
    ctx = fx.one_haulier().context(runner)
    assert ctx.candidate_pairs("spatial"), "a partition scope builds spatial pairs whatever its kind"
    assert runner.run(fx.one_haulier().context(runner)) and runner.stats["violations"] == 0
