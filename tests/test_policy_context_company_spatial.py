"""A sub-tick asked for spatial pairs the caller did not supply builds its own; supplied ones are served.

What is locked (the generic contract, plan_contract_final.md G1): a caller (the pooled
market) hands a sub-tick its round's prebuilt spatial pairs through
``restrict(candidates=...)`` ONLY when its callee hints ``"spatial"``; a legacy
``ctx.candidate_pairs("spatial")`` (``compat.legacy_context``) serves exactly those,
an empty prebuilt slice included (``[]``, not a rebuild), and on a sub-tick whose
caller supplied none it BUILDS the tick's own spatial pairs instead of raising (the
old company-scope ``DispatchContractViolation`` is gone). End to end, a sampled-kind
inner of ``PooledMarket`` that asks for spatial pairs therefore commits pairs and no
contract violation is logged; the whole tick builds spatial pairs on demand.

Why: the old raise existed only because a company scope had no way to build pairs
itself. A sub-tick now has the documents, ``serves`` and ``permitted`` to build them,
so a policy asking for spatial pairs on a non-spatial arm still gets a real candidate
set, never a silent ``[]``.

Verified to have teeth: making ``LegacyDecideContext.candidate_pairs`` return ``[]``
when ``tick.candidates is None`` failed
``test_a_sub_tick_with_no_supplied_pairs_builds_its_own_spatial_pairs`` and
``test_a_sampled_inner_asking_for_spatial_pairs_commits_under_the_market``; treating an
empty supplied slice as "none supplied" (``if tick.candidates``) failed
``test_supplied_pairs_are_served_even_when_empty``.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.compat.legacy_context import LegacyDecideContext
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.framework import PolicyParams, param, policy
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.solver_lib import candidates as C
from apps.container_logistics.policies.testing import fixtures as fx


@pytest.fixture
def asks_spatial():
    """A policy whose ``candidates`` defaults to ``sampled`` but whose decide asks for spatial pairs."""
    with isolated_registry():
        @policy(slot="planner", information_model="central", name="AsksSpatial", version="1", description="always reads spatial pairs")
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


def _ids(pairs):
    return [(t["_id"], o["_id"]) for t, o in pairs]


def _company_sub(runner, prebuilt=None):
    """A company sub-tick of the one-haulier fixture, as the market builds it (``company:<id>``)."""
    tick = fx.one_haulier().context(runner)
    return tick, tick.restrict(trucks=tick.raw_trucks, orders=tick.raw_orders, scope="company:h_a",
                               sample_rng=random.Random(1), candidates=prebuilt)


def test_a_sub_tick_with_no_supplied_pairs_builds_its_own_spatial_pairs(asks_spatial):
    tick, sub = _company_sub(build_runner_for_policy(asks_spatial))
    ctx = LegacyDecideContext(sub)
    got = ctx.candidate_pairs("spatial")
    assert got, "a sub-tick builds spatial pairs whatever its kind"
    assert _ids(got) == _ids(C.spatial_pairs(sub, C.CandidateSettings.from_world(sub.world)))
    assert ctx.candidate_pairs("sampled"), "the sub-tick's own kind still works"


def test_supplied_pairs_are_served_even_when_empty(asks_spatial):
    runner = build_runner_for_policy(asks_spatial, {"candidates": "spatial"})
    _tick, sub = _company_sub(runner, prebuilt=[])
    assert LegacyDecideContext(sub).candidate_pairs("spatial") == []
    tick, _ = _company_sub(runner)
    one = [(tick.raw_trucks[0], tick.raw_orders[0])]
    _tick2, sub2 = _company_sub(runner, prebuilt=one)
    assert _ids(LegacyDecideContext(sub2).candidate_pairs("spatial")) == _ids(one)


def test_a_sampled_inner_asking_for_spatial_pairs_commits_under_the_market(asks_spatial, caplog):
    """``PooledMarket[inner=AsksSpatial]`` (hint ``None``: no prebuild) commits, with no violation."""
    runner = build_runner_for_policy("PooledMarket", {"inner": {"type": asks_spatial}})
    assert runner.tree.top.inner.declaration.candidate_hint is None
    with caplog.at_level(logging.ERROR):
        committed = runner.run(fx.one_haulier().context(runner)).pairs
    assert committed and runner.stats["violations"] == 0
    assert not any("PLANNER POLICY CONTRACT VIOLATION" in r.getMessage() for r in caplog.records)


def test_the_whole_tick_builds_spatial_pairs_on_demand(asks_spatial):
    runner = build_runner_for_policy(asks_spatial)  # kind 'sampled', whole tick
    ctx = LegacyDecideContext(fx.one_haulier().context(runner))
    assert ctx.candidate_pairs("spatial"), "the whole tick builds spatial pairs whatever its kind"
    assert runner.run(fx.one_haulier().context(runner)).pairs and runner.stats["violations"] == 0
