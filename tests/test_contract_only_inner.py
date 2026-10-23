"""A contract-only leaf (no ``DispatchPolicy`` base) as the ``inner`` of a built-in composite.

Plan ``plan_contract_final.md`` §2.1/§2.2: a registered planner class needs only
``solve_tick``; the ``DispatchPolicy`` helper base (the injection API ``set_rng`` /
``set_tiebreak`` / ``set_rebate_book``) is optional. WS8 litmus (b)
(``tests/test_solver_contract_dropin.py``) found two places that still assumed it:

- ``dispatch/composite.py`` ``CompositePolicy.__init__`` refused a non-``DispatchPolicy``
  inner with ``TypeError``, so ``Partitioned{inner: <contract-only leaf>}`` could not even
  be built. It now takes any inner and forwards each injection only to an inner that
  offers it (every built-in does, so nothing that ran before changes).
- ``PooledMarket.solve_tick`` seeds its inner with ``set_rng`` / ``set_tiebreak`` before
  any draw (pre-v2 order S4). A contract-only inner crashed mid-tick with
  ``AttributeError``; ``PooledMarket.__init__`` now refuses it when the tree is built at
  run start (``bind`` stays pure in ``(params, world)``), naming the API.

Verified to have teeth (WS8, mutate / see it fail / restore): restoring the
``isinstance(inner, DispatchPolicy)`` TypeError in ``CompositePolicy.__init__`` failed
``test_partitioned_runs_a_contract_only_inner``; removing the
``_require_inner_injections(self)`` call from ``PooledMarket.__init__`` failed
``test_pooled_market_refuses_an_inner_without_the_injection_api_at_bind``; making
``CompositePolicy._forward`` skip every call failed
``test_injections_still_reach_an_inner_that_offers_them``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.dispatch.bind import Declaration, bind_profile
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, policy
from apps.container_logistics.policies.framework.registry import isolated_registry


def _toy_class():
    @policy(slot="planner", name="ContractOnlyLeaf", version="1",
            description="A leaf with solve_tick and bind only (no DispatchPolicy base).")
    class ContractOnlyLeaf:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def __init__(self, params=None):
            self.params = params

        def bind(self, params, world, *, top):
            return Declaration()

        def solve_tick(self, tick, params):
            return Result.from_pairs([])

    return ContractOnlyLeaf


def _profile(top: str) -> dict:
    return {"policies_format": 2,
            "policies": {"planner": {"type": top, "params": {"inner": {"type": "ContractOnlyLeaf"}}}}}


def test_partitioned_runs_a_contract_only_inner():
    with isolated_registry():
        _toy_class()
        tree = bind_profile(_profile("Partitioned"))
        assert type(tree.top.instance.inner).__name__ == "ContractOnlyLeaf"
        top = tree.top.instance
        # The injections the app makes on the top must not reach for a missing API.
        top.set_rng(random.Random(1))
        top.set_tiebreak(7)
        top.set_rebate_book(None)


def test_injections_still_reach_an_inner_that_offers_them():
    with isolated_registry():
        tree = bind_profile({"policies_format": 2, "policies": {"planner": {
            "type": "PooledMarket", "params": {"inner": {"type": "GreedyNearest"}}}}})
        top = tree.top.instance
        rng = random.Random(3)
        top.set_rng(rng)
        top.set_tiebreak(11)
        assert top.inner.rng is rng
        assert top.inner.tiebreak_seed == 11


def test_pooled_market_refuses_an_inner_without_the_injection_api_at_run_start():
    with isolated_registry():
        _toy_class()
        with pytest.raises(PolicyError, match=r"PooledMarket: its inner ContractOnlyLeaf has no "
                                              r"set_rng / set_tiebreak\. .*injection API"):
            bind_profile(_profile("PooledMarket"))
