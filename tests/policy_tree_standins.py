"""Test stand-ins for the two decomposition solvers, registered only inside a test.

Responsibility: let round-2 C2 tests exercise the selection TREE (legacy translation
through the resolver, nested params, binding, mirrors) before work item C3 registered
the real ``Partitioned`` and ``PooledMarket``. :func:`decompositions` registers a
stand-in under the real name ONLY when that name is not registered, inside
``isolated_registry()``; since C3 the real classes are always registered, so the
tests run against them (the stand-ins only matter in an isolated registry that lost
the built-ins). The params mirror the round-2 spec exactly (§3.1, §5 C3): ``Partitioned``
declares only ``inner``; ``PooledMarket`` declares ``max_rounds, offer, claim,
arbitration, inner`` in that order.

The stand-ins cannot decide (``decide`` raises): they exist for resolution and
binding only.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from apps.container_logistics.policies.dispatch.composite import CompositePolicy
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.registry import isolated_registry, list_policies

__all__ = ["StandInPartitioned", "StandInPooledMarket", "decompositions"]

_INNER_HELP = "The per-scope solver each scope is decided by (a leaf)."


class StandInPartitioned(CompositePolicy):
    """Test stand-in for ``Partitioned`` (per cooperation group)."""

    @dataclass(frozen=True)
    class Params(PolicyParams):
        inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True, help=_INNER_HELP)

    def decide(self, ctx):  # pragma: no cover - resolution/binding only
        raise NotImplementedError("test stand-in")


class StandInPooledMarket(CompositePolicy):
    """Test stand-in for ``PooledMarket`` (per company, through shared pools)."""

    @dataclass(frozen=True)
    class Params(PolicyParams):
        max_rounds: int = param(20, ge=1, le=100, help="Safety backstop on auction rounds.")
        offer: PolicyRef = param(ref("OfferAll"), kind="market.offer", help="Offer component.")
        claim: PolicyRef = param(ref("ClaimAllPlanned"), kind="market.claim", help="Claim component.")
        arbitration: PolicyRef = param(ref("LowestCost"), kind="market.arbitration",
                                       help="Arbitration component.")
        inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True, help=_INNER_HELP)

    def decide(self, ctx):  # pragma: no cover - resolution/binding only
        raise NotImplementedError("test stand-in")


_STANDINS = (
    (StandInPartitioned, dict(name="Partitioned", information_model="per_group", share_tag_format="v1")),
    (StandInPooledMarket, dict(name="PooledMarket", information_model="per_company")),
)


@contextmanager
def decompositions() -> Iterator[None]:
    """Inside ``isolated_registry()``, make ``Partitioned``/``PooledMarket`` resolvable.

    A real registration (C3 on) is left alone; a missing one gets the stand-in.
    """
    with isolated_registry():
        names = {spec.name for spec in list_policies("planner")}
        for cls, meta in _STANDINS:
            if meta["name"] not in names:
                policy(slot="planner", version="1", description=cls.__doc__, **meta)(cls)
        yield
