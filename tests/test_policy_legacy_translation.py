"""Legacy dispatch keys -> the round-2 selection tree (plan round 2 §3.6, §5 C2).

What is locked:
- ``legacy.wrap_legacy_leaf``: a partitioned (or absent) ``planner.topology`` wraps the
  leaf in ``Partitioned{inner}``; ``pooled``/``two-stage`` wrap it in
  ``PooledMarket{max_rounds, offer, claim, arbitration, inner}`` read from
  ``planner.market`` (defaults OfferAll / ClaimAllPlanned / LowestCost / 20); a
  ``planner.market`` next to a partitioned topology is inert and never read;
- the profile resolver applies that translation to a bundle profile without
  ``policies`` (``strategy``, ``solver_params``, ``use_spatial_matching``, ``planner``);
- the strictness flips: an unknown topology (message 6), an unknown market component
  name and a ``max_rounds`` that is not an int in 1..100 are LOUD; undeclared
  component params are dropped at WARNING (the one legacy tolerance rule);
- the translation is deterministic (same input -> same JSON bytes), and on valid
  inputs ``legacy_market_params`` agrees with the runtime's
  ``pooled_planner.resolve_market_config``.

Why: every pre-v2 bundle and every legacy env var goes through this translation, so a
silent difference here would change which decomposition a run executes. The resolver
cases run against the stand-in decompositions of ``tests/policy_tree_standins.py``
until work item C3 registers the real ones (the same tests then run against those).
"""

from __future__ import annotations

import itertools
import json
import logging

import pytest

from apps.container_logistics.datagen.preprocess import Preprocessor
from apps.container_logistics.assignment import pooled_planner
from apps.container_logistics.policies.dispatch import legacy
from apps.container_logistics.policies.dispatch.profile import (
    resolve_dispatch_for_profile,
    resolve_profile_policies,
)
from apps.container_logistics.policies.framework import PolicyError, PolicyParamsError, UnknownPolicyError
from apps.container_logistics.policies.framework.components import COMPONENT_KINDS
from tests.policy_tree_standins import decompositions

_LEAF = {"type": "GreedyNearest", "timing": "online", "params": {"dual_cycle_bonus_km": 3.0}}
_INNER = {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 3.0}}
_DEFAULT_MARKET = {
    "max_rounds": 20,
    "offer": {"type": "OfferAll", "params": {}},
    "claim": {"type": "ClaimAllPlanned", "params": {}},
    "arbitration": {"type": "LowestCost", "params": {}},
}
_SPARE = {"type": "OfferSpare", "params": {"keep_below_km": 3.0}}

# (row id, planner, expected wrapped entry) -- round-2 §3.6 rows 1-2.
_SPEC_ROWS = [
    ("no-planner", None, {"type": "Partitioned", "timing": "online", "params": {"inner": _INNER}}),
    ("empty-planner", {}, {"type": "Partitioned", "timing": "online", "params": {"inner": _INNER}}),
    ("partitioned", {"topology": "partitioned"},
     {"type": "Partitioned", "timing": "online", "params": {"inner": _INNER}}),
    ("partitioned-with-inert-bad-market", {"topology": "partitioned", "market": {"offer": {"type": "Nope"}}},
     {"type": "Partitioned", "timing": "online", "params": {"inner": _INNER}}),
    ("pooled-default-market", {"topology": "pooled"},
     {"type": "PooledMarket", "timing": "online", "params": {**_DEFAULT_MARKET, "inner": _INNER}}),
    ("pooled-authored-market", {"topology": "pooled", "market": {"max_rounds": 7, "offer": _SPARE}},
     {"type": "PooledMarket", "timing": "online",
      "params": {**_DEFAULT_MARKET, "max_rounds": 7, "offer": _SPARE, "inner": _INNER}}),
    ("two-stage", {"topology": "two-stage", "market": {"claim": {"type": "ClaimNone"}}},
     {"type": "PooledMarket", "timing": "online",
      "params": {**_DEFAULT_MARKET, "claim": {"type": "ClaimNone", "params": {}}, "inner": _INNER}}),
]


@pytest.mark.parametrize("row,planner,expected", _SPEC_ROWS, ids=[r[0] for r in _SPEC_ROWS])
def test_legacy_spec_translates(row, planner, expected):
    """Verified to have teeth: wrapping every topology in Partitioned failed the pooled
    rows; building the market in the authored key order failed the two-stage row."""
    got = legacy.wrap_legacy_leaf(_LEAF, planner)
    assert got == expected, f"[{row}] wrap_legacy_leaf gave {got}"
    assert list(got) == list(expected), f"[{row}] key order must be type, timing, params"
    assert list(got["params"]) == list(expected["params"]), f"[{row}] params order differs"


_PROFILE_ROWS = [
    ("no-strategy", {}, "Partitioned/RandomAssignment", {"inner": {"type": "RandomAssignment",
                                                                    "params": {"candidates": "sampled"}}}),
    ("greedy-sampled", {"strategy": "GreedyNearest", "use_spatial_matching": False,
                        "solver_params": {"dual_cycle_bonus_km": 2.5, "legacy_knob": 1}},
     "Partitioned/GreedyNearest",
     {"inner": {"type": "GreedyNearest", "params": {"candidates": "sampled", "dual_cycle_bonus_km": 2.5,
                                                    "dual_cycle_radius_km": 0.5}}}),
    ("greedy-pooled", {"strategy": "GreedyNearest", "planner": {"topology": "pooled",
                                                                 "market": {"max_rounds": 4}}},
     "PooledMarket/GreedyNearest",
     {"max_rounds": 4, "offer": {"type": "OfferAll", "params": {}},
      "claim": {"type": "ClaimAllPlanned", "params": {}}, "arbitration": {"type": "LowestCost", "params": {}},
      "inner": {"type": "GreedyNearest", "params": {"candidates": "spatial", "dual_cycle_bonus_km": 5.0,
                                                    "dual_cycle_radius_km": 0.5}}}),
]


@pytest.mark.parametrize("row,profile,label,params", _PROFILE_ROWS, ids=[r[0] for r in _PROFILE_ROWS])
def test_legacy_profile_translates(row, profile, label, params):
    """Round-2 §3.6 row 3: a bundle profile without ``policies``.

    Verified to have teeth: resolving the flat leaf (skipping ``wrap_legacy_leaf`` in
    ``resolve_profile_policies``) failed every row.
    """
    with decompositions():
        node = resolve_dispatch_for_profile(profile)
        resolved, source = resolve_profile_policies(profile)
    assert (node.label, node.source, source) == (label, "legacy-profile", "legacy-profile"), node
    assert node.params.to_dict() == params, f"[{row}] {node.params.to_dict()}"
    assert resolved["planner"].params == params and resolved["planner"].label == label
    assert node.inner is not None and node.inner.name == label.split("/")[1]


def test_unknown_topology_is_loud():
    """Message 6 (the pre-v2 runtime fell back to partitioned).

    Verified to have teeth: returning ``"partitioned"`` for an unknown value failed this test.
    """
    with pytest.raises(PolicyError) as exc:
        legacy.legacy_topology({"topology": "nonsense"})
    assert str(exc.value) == (
        "assignment profile planner.topology='nonsense' is not a legacy topology (partitioned, "
        "pooled, two-stage). The decomposition is now the planner policy: policies.planner.type "
        "= Partitioned or PooledMarket."
    )
    for bad in ("", "Pooled", 3, ["pooled"]):
        with pytest.raises(PolicyError, match="is not a legacy topology"):
            legacy.legacy_topology({"topology": bad})
    with decompositions(), pytest.raises(PolicyError, match="planner.topology='nonsense'"):
        resolve_dispatch_for_profile({"planner": {"topology": "nonsense"}})
    for planner in (None, {}, {"topology": None}, {"topology": "partitioned"}):
        assert legacy.legacy_topology(planner) == "partitioned", planner
    # C3 (C2 review finding 6): a non-object planner is loud, never a silent "partitioned".
    for planner in ("not-a-mapping", [], 3):
        with pytest.raises(PolicyParamsError, match=r"^assignment profile planner must be an object"):
            legacy.legacy_topology(planner)


def test_two_stage_is_pooled_market():
    """Verified to have teeth: mapping ``two-stage`` to itself failed this test."""
    assert legacy.legacy_topology({"topology": "two-stage"}) == "pooled"
    assert legacy.legacy_topology({"topology": "pooled"}) == "pooled"
    with decompositions():
        node = resolve_dispatch_for_profile({"strategy": "GreedyNearest", "planner": {"topology": "two-stage"}})
    assert node.label == "PooledMarket/GreedyNearest"


def test_unknown_market_component_is_loud():
    """The pre-v2 market registries fell back to the default with a log line.

    Verified to have teeth: falling back to the kind's default on an unknown name (the
    pre-v2 fail-soft, a ``try``/``except`` around ``ref_target``) failed this test.
    """
    with pytest.raises(UnknownPolicyError) as exc:
        legacy.legacy_market_params({"market": {"offer": {"type": "OfferAl"}}})
    assert str(exc.value) == (
        "planner.market.offer: unknown market.offer component 'OfferAl'. Registered: OfferAll, "
        "OfferNone, OfferSpare. (List them: openride policy list market.offer)"
    )
    with pytest.raises(UnknownPolicyError, match="NopeRule"):
        legacy.legacy_market_params({"market": {"arbitration": {"type": "NopeRule"}}})
    for bad_market, match in (
        ("OfferAll", "planner.market must be an object"),
        ({"offer": "OfferAll"}, r"planner.market.offer must be an object \{type, params\?\}"),
        ({"offer": {"type": "OfferAll", "parms": {}}}, "unknown key"),
        ({"offer": {"type": "OfferAll", "params": [1]}}, "planner.market.offer.params must be an object"),
        ({"offer": {"type": 7}}, "planner.market.offer.type must name a market.offer component"),
    ):
        with pytest.raises(PolicyParamsError, match=match):
            legacy.legacy_market_params({"market": bad_market})


@pytest.mark.parametrize("value", [0, 101, -1, True, "5", 2.0])
def test_max_rounds_must_be_an_int_in_range(value):
    """Verified to have teeth: removing the ``isinstance(value, bool)`` guard let ``True``
    through as 1 and failed the ``True`` case."""
    with pytest.raises(PolicyParamsError, match=r"planner.market.max_rounds must be an integer in 1..100"):
        legacy.legacy_market_params({"market": {"max_rounds": value}})


def test_max_rounds_null_is_the_default_like_preprocess():
    """C3 (C2 review NIT): ``null`` means the default (20) here as in ``Preprocessor._normalize_market``.

    Verified to have teeth: passing ``None`` to ``_max_rounds`` (the C2 behaviour) failed this test.
    """
    assert legacy.legacy_market_params({"market": {"max_rounds": None}})["max_rounds"] == 20
    assert Preprocessor._normalize_market({"max_rounds": None})["max_rounds"] == 20


def test_padded_component_names_are_refused():
    """C3 (C2 review NIT): a padded name is loud, not silently stripped (component names match exactly).

    Verified to have teeth: removing the ``name != name.strip()`` check let ``' OfferSpare '`` resolve.
    """
    with pytest.raises(PolicyParamsError, match=r"planner.market.offer.type ' OfferSpare ' has leading/trailing"):
        legacy.legacy_market_params({"market": {"offer": {"type": " OfferSpare "}}})


def test_undeclared_component_params_dropped_with_warning(caplog):
    """Verified to have teeth: passing component params through unfiltered failed the
    equality, and removing the ``logger.warning`` failed the log assertion."""
    with caplog.at_level(logging.WARNING, logger="apps.container_logistics.policies.dispatch.legacy"):
        got = legacy.legacy_market_params({"market": {
            "offer": {"type": "OfferSpare", "params": {"keep_below_km": 2, "bogus_knob": 1}},
            "claim": {"type": "ClaimAllPlanned", "params": {"min_gain_km": 1.0}},
        }})
    assert got["offer"] == {"type": "OfferSpare", "params": {"keep_below_km": 2}}
    assert got["claim"] == {"type": "ClaimAllPlanned", "params": {}}
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("bogus_knob" in m and "OfferSpare" in m for m in messages), messages
    assert any("min_gain_km" in m and "ClaimAllPlanned" in m for m in messages), messages


def test_translation_is_deterministic():
    """Same input -> same JSON bytes, whatever the authored key order.

    Verified to have teeth: building ``legacy_market_params`` by iterating the authored
    ``market`` dict (instead of the fixed role order) failed the reordered case.
    """
    market = {"max_rounds": 9, "arbitration": {"type": "LowestCost"}, "offer": _SPARE,
              "claim": {"type": "ClaimNone"}}
    reordered = dict(reversed(list(market.items())))
    a = json.dumps(legacy.wrap_legacy_leaf(_LEAF, {"topology": "pooled", "market": market}))
    b = json.dumps(legacy.wrap_legacy_leaf(_LEAF, {"topology": "pooled", "market": reordered}))
    c = json.dumps(legacy.wrap_legacy_leaf(dict(_LEAF), {"market": reordered, "topology": "pooled"}))
    assert a == b == c


def test_legacy_market_params_agree_with_resolve_market_config_on_valid_inputs():
    """On every valid input, the strict reader and the runtime's fail-soft reader agree.

    The grid crosses every registered component name of each role (with declared params
    only) with absent/present roles and several ``max_rounds``.

    Verified to have teeth: defaulting ``max_rounds`` to 10 in ``legacy_market_params``
    failed the absent-``max_rounds`` cells.
    """
    choices = {
        "offer": [None, {"type": "OfferAll"}, {"type": "OfferNone", "params": {}}, _SPARE,
                  {"type": "OfferSpare", "params": {"only_when_short": False}}],
        "claim": [None, {"type": "ClaimAllPlanned"}, {"type": "ClaimNone"},
                  {"type": "ClaimIfGainExceeds", "params": {"min_gain_km": 1.5}}],
        "arbitration": [None, {"type": "LowestCost"}, {"type": "OwnerFirst"}],
        "max_rounds": [None, 1, 20, 100],
    }
    cells = 0
    for offer, claim, arbitration, max_rounds in itertools.product(*choices.values()):
        market = {k: v for k, v in (("offer", offer), ("claim", claim), ("arbitration", arbitration),
                                    ("max_rounds", max_rounds)) if v is not None}
        planner = {"topology": "pooled", "market": market}
        strict = legacy.legacy_market_params(planner)
        soft = pooled_planner.resolve_market_config(planner)
        assert strict == soft, f"market {market}: strict {strict} != runtime {soft}"
        cells += 1
    assert cells == 5 * 4 * 3 * 4


def test_constants_match_the_runtime_and_component_defaults():
    """The leaf restates three runtime facts it cannot import; pin them.

    Verified to have teeth: setting ``LEGACY_MAX_ROUNDS_DEFAULT = 2`` failed this test.
    """
    assert legacy.LEGACY_MAX_ROUNDS_DEFAULT == pooled_planner.DEFAULT_MAX_ROUNDS
    assert legacy.LEGACY_MAX_ROUNDS_RANGE == (1, pooled_planner.MAX_ROUNDS_CEILING)
    assert dict(legacy.MARKET_ROLES) == {"offer": "market.offer", "claim": "market.claim",
                                         "arbitration": "market.arbitration"}
    assert set(legacy.MARKET_ROLES.values()) == set(COMPONENT_KINDS)
    defaults = legacy.legacy_market_params({})
    assert {r: defaults[r]["type"] for r in legacy.MARKET_ROLES} == {
        "offer": pooled_planner.DEFAULT_OFFER_POLICY, "claim": pooled_planner.DEFAULT_CLAIM_POLICY,
        "arbitration": pooled_planner.DEFAULT_ARBITRATION_RULE}


def test_echoes_invert_the_translation():
    """``topology_echo``/``market_echo`` are what compile writes back (round-2 §3.6).

    Verified to have teeth: keeping ``None`` params in ``market_echo`` failed the
    ``keep_below_km`` assertion.
    """
    assert legacy.topology_echo("Partitioned") == "partitioned"
    assert legacy.topology_echo("PooledMarket") == "pooled"
    assert legacy.topology_echo("GreedyNearest") is None
    with decompositions():
        node = resolve_dispatch_for_profile({"planner": {"topology": "pooled", "market": {
            "max_rounds": 5, "offer": {"type": "OfferSpare", "params": {"only_when_short": False}}}}})
    echo = legacy.market_echo(node.params)
    assert echo == {
        "offer": {"type": "OfferSpare", "params": {"only_when_short": False}},  # keep_below_km None dropped
        "claim": {"type": "ClaimAllPlanned", "params": {}},
        "arbitration": {"type": "LowestCost", "params": {}},
        "max_rounds": 5,
    }
    assert list(echo) == ["offer", "claim", "arbitration", "max_rounds"]
    assert legacy.market_echo(node.params.to_dict()) == echo
    assert pooled_planner.resolve_market_config({"market": echo}) == echo, "the echo must read back unchanged"
