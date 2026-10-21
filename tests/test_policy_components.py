"""Locks the market component kinds as policy-reference targets (round-2 plan §3.2, §5 C1).

``market.offer``/``market.claim``/``market.arbitration`` are no longer slots: they are
components the ``PooledMarket`` dispatch policy references. What is locked:

- a ref resolves against the LIVE legacy registries, so a name a test injects there
  resolves (round-2 S11), and a class without ``Params`` declares no params;
- every built-in component declares a typed ``Params`` whose defaults validate;
- the typed defaults reproduce the legacy ``params={}`` behaviour exactly, so moving
  the market onto typed params cannot change a pooled run;
- ``OfferSpare.keep_below_km=None`` means infinity (the class's own default).
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass

import pytest

from apps.container_logistics.assignment.arbitration import ARBITRATION_REGISTRY, DEFAULT_ARBITRATION_RULE
from apps.container_logistics.assignment.policy.claim import (
    CLAIM_REGISTRY,
    DEFAULT_CLAIM_POLICY,
    BaseClaimPolicy,
)
from apps.container_logistics.assignment.policy.offer import DEFAULT_OFFER_POLICY, OfferSparePolicy
from apps.container_logistics.assignment.pools import Bid, Pool, PoolMarket
from apps.container_logistics.policies.framework import (
    SUPPORTED_SCHEMA_KEYWORDS,
    PolicyParams,
    PolicyParamsError,
    PolicyRef,
    UnknownPolicyError,
    param,
    ref,
)
from apps.container_logistics.policies.framework.catalog import component_catalog
from apps.container_logistics.policies.framework.components import (
    COMPONENT_KINDS,
    NoParams,
    instantiate_component,
    live_registry,
    ref_names,
    ref_target,
)
from apps.container_logistics.policies.framework.params import check_defaults

TICK_SEED = 424242


@dataclass(frozen=True)
class _MarketParams(PolicyParams):
    offer: PolicyRef = param(ref("OfferAll"), kind="market.offer", help="Offer.")
    claim: PolicyRef = param(ref("ClaimAllPlanned"), kind="market.claim", help="Claim.")
    arbitration: PolicyRef = param(ref("LowestCost"), kind="market.arbitration", help="Arbitration.")


def _typed(kind: str, name: str, params=None) -> PolicyRef:
    """The resolved ref a PooledMarket would hold for ``kind``/``name``."""
    field = kind.rpartition(".")[2]
    parsed = _MarketParams.from_mapping({field: {"type": name, "params": params or {}}},
                                        where="policies.dispatch.params for PooledMarket")
    return getattr(parsed, field)


# --- a fixed market fixture ------------------------------------------------------------


def _truck(tid, hid):
    return {"_id": tid, "profile": {"haulier_id": hid}}


def _order(oid, hid):
    return {"_id": oid, "profile": {"haulier_id": hid}}


_COSTS = {
    ("a1", "a-o1"): 2.0, ("a1", "a-o2"): 9.0, ("a2", "a-o2"): 4.0, ("a2", "a-o3"): 12.0,
    ("a1", "b-o1"): 1.0, ("a2", "b-o1"): 3.0, ("a2", "b-o2"): 6.0, ("b1", "b-o1"): 1.5,
    ("b1", "a-o3"): 5.0,
}


def _cost(truck, order):
    return _COSTS.get((truck["_id"], order["_id"]), math.inf)


def _market() -> PoolMarket:
    market = PoolMarket([Pool(id="p:acme+borax", members=frozenset({"acme", "borax"}))])
    market.contribute("b-o1", "borax", ["p:acme+borax"])
    market.contribute("b-o2", "borax", ["p:acme+borax"])
    market.contribute("a-o3", "acme", ["p:acme+borax"])
    return market


ACME_TRUCKS = [_truck("a1", "acme"), _truck("a2", "acme")]
ACME_ORDERS = [_order("a-o1", "acme"), _order("a-o2", "acme"), _order("a-o3", "acme"),
               _order("a-o4", "acme")]
OWNER = {"a-o1": "acme", "a-o2": "acme", "a-o3": "acme", "b-o1": "borax", "b-o2": "borax"}


def _offer_out(policy):
    market = _market()
    return policy.offer(haulier_id="acme", orders=list(ACME_ORDERS), trucks=list(ACME_TRUCKS),
                        pools=market.pools_for("acme"), rng=random.Random(7), cost=_cost)


def _claim_out(policy):
    pooled = [(ACME_TRUCKS[0], _order("b-o1", "borax")), (ACME_TRUCKS[1], _order("b-o2", "borax"))]
    own = [(ACME_TRUCKS[0], ACME_ORDERS[0]), (ACME_TRUCKS[1], ACME_ORDERS[1])]
    return policy.bids(haulier_id="acme", pooled_pairs=pooled, own_pairs=own, cost=_cost,
                       market=_market(), rng=random.Random(7), round=1)


def _arbitration_out(policy):
    bids = [Bid("acme", "b-o1", "a1", 1.0, "p:acme+borax", 1), Bid("borax", "b-o1", "b1", 1.5, "p:acme+borax", 1),
            Bid("acme", "b-o2", "a2", 6.0, "p:acme+borax", 1), Bid("borax", "a-o3", "b1", 5.0, "p:acme+borax", 1),
            Bid("acme", "a-o3", "a2", 12.0, "p:acme+borax", 1), Bid("acme", "b-o2", "a1", math.inf, "p:acme+borax", 1)]
    return policy.resolve(bids, market=_market(), tick_seed=TICK_SEED, owner_of=OWNER.get,
                          rng=random.Random(7))


_RUN = {"market.offer": _offer_out, "market.claim": _claim_out, "market.arbitration": _arbitration_out}


# --- tests -----------------------------------------------------------------------------


def test_component_ref_resolves_a_name_injected_into_the_live_registry():
    """A name added to ``CLAIM_REGISTRY`` at runtime is a valid ref target; a class
    without ``Params`` declares none; removing the name makes it unknown again.

    Verified to have teeth: caching a ``dict(...)`` snapshot of the registry in
    ``components.live_registry`` failed the injected-name resolution; dropping the
    ``NoParams`` fallback (``getattr(cls, "Params")``) raised AttributeError.
    """
    class _TestInjectedClaim(BaseClaimPolicy):
        """An injected claim policy with no Params."""

        def bids(self, **kwargs):
            return []

    CLAIM_REGISTRY["_TestInjectedClaim"] = _TestInjectedClaim
    try:
        resolved = _typed("market.claim", "_TestInjectedClaim")
        assert resolved.type == "_TestInjectedClaim" and resolved.version is None
        assert isinstance(resolved.params, NoParams)
        assert "_TestInjectedClaim" in ref_names("market.claim")
        built = instantiate_component("market.claim", resolved)
        assert type(built) is _TestInjectedClaim and built.params == {}
        names = [p["name"] for p in component_catalog()["market.claim"]["policies"]]
        assert "_TestInjectedClaim" in names
        with pytest.raises(PolicyParamsError, match=r"unknown key 'x'\. Declared params: \(none\)"):
            _typed("market.claim", "_TestInjectedClaim", {"x": 1})
    finally:
        del CLAIM_REGISTRY["_TestInjectedClaim"]
    with pytest.raises(UnknownPolicyError, match="unknown market.claim component '_TestInjectedClaim'"):
        _typed("market.claim", "_TestInjectedClaim")


def test_every_builtin_component_declares_params_and_defaults_validate():
    """Each built-in offer/claim/arbitration class carries its own typed ``Params`` and
    ``version="1"``; defaults validate; schemas use only supported keywords; each kind's
    default is the legacy module default.

    Verified to have teeth: deleting ``OwnerFirstRule.Params`` failed the
    own-``Params`` assertion; setting ``RandomAwardRule.version = "2"`` failed the version one.
    """
    assert list(COMPONENT_KINDS) == ["market.offer", "market.claim", "market.arbitration"]
    legacy_defaults = {"market.offer": DEFAULT_OFFER_POLICY, "market.claim": DEFAULT_CLAIM_POLICY,
                       "market.arbitration": DEFAULT_ARBITRATION_RULE}
    for kind, spec in COMPONENT_KINDS.items():
        registry = live_registry(kind)
        assert spec.default == legacy_defaults[kind] and spec.default in registry, kind
        assert len(registry) >= 3, kind
        for name, cls in registry.items():
            assert "Params" in vars(cls), f"{kind} {name} must declare its own nested Params"
            assert vars(cls).get("version") == "1", f"{kind} {name} must declare version='1'"
            check_defaults(cls.Params, where=f"{kind} {name}")
            target = ref_target(kind, name, where="t")
            assert target.params_cls is cls.Params and not target.is_composite
            schema = cls.Params.json_schema()
            used = set(schema) | {k for prop in schema["properties"].values() for k in prop}
            assert used <= SUPPORTED_SCHEMA_KEYWORDS, (kind, name, used - SUPPORTED_SCHEMA_KEYWORDS)
    assert ARBITRATION_REGISTRY is live_registry("market.arbitration"), "the LIVE dict, not a copy"


@pytest.mark.parametrize("kind", list(COMPONENT_KINDS))
def test_typed_defaults_reproduce_legacy_empty_params(kind):
    """For every class of ``kind``: the typed-default instance and ``cls(params={})``
    produce identical output on a fixed market fixture.

    Verified to have teeth: changing the ``ClaimIfGainExceeds`` typed default to 1.5
    failed the output equality; making ``instantiate_component`` keep ``None`` values
    failed the no-``None`` assertion (OfferSpare's own warning fallback to ``inf``
    would otherwise hide it in the output).
    """
    for name, cls in live_registry(kind).items():
        typed = instantiate_component(kind, _typed(kind, name))
        legacy = cls(params={})
        assert type(typed) is cls
        assert all(v is not None for v in typed.params.values()), (name, typed.params)
        assert _RUN[kind](typed) == _RUN[kind](legacy), f"{kind} {name}: typed defaults changed behaviour"


def test_offer_spare_keep_below_none_means_infinity(caplog):
    """``keep_below_km=None`` is dropped before instantiation, so OfferSpare uses ``inf``
    with no "bad keep_below_km" warning; a number really is the threshold.

    Verified to have teeth: removing the ``if v is not None`` filter in
    ``instantiate_component`` logged the "bad keep_below_km" warning and left
    ``keep_below_km`` in the params.
    """
    caplog.set_level(logging.WARNING)
    unset = instantiate_component("market.offer", _typed("market.offer", "OfferSpare",
                                                         {"keep_below_km": None, "only_when_short": False}))
    assert unset.params == {"only_when_short": False}
    infinite = OfferSparePolicy({"keep_below_km": math.inf, "only_when_short": False})
    assert _offer_out(unset) == _offer_out(infinite)
    assert set(_offer_out(unset)) == {"a-o4"}, "with inf only the order no truck can serve is offered"
    assert not [r for r in caplog.records if "keep_below_km" in r.getMessage()]
    three = instantiate_component("market.offer", _typed("market.offer", "OfferSpare",
                                                         {"keep_below_km": 3, "only_when_short": False}))
    assert set(_offer_out(three)) == {"a-o2", "a-o3", "a-o4"}, "orders whose best own cost exceeds 3 km"
