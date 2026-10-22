"""Per-run planner-topology / pooled-market overrides (shared-pool plan §6.11, Phase 6).

These mirror the existing solver/cooperation override contract: pure data patched
onto the LOADED assignment behaviors before agents spawn, and the on-disk bundle never
touched. Round 2 (``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §5 C4): both
are translated into the dispatch selection (``PooledMarket`` / ``Partitioned`` and its
params) and are LOUD on bad input; the ``planner`` keys are rewritten as mirrors.
"""

import copy
import json

import pytest

from apps.container_logistics.datagen.preprocess import PLANNER_TOPOLOGIES
from apps.container_logistics.policies.framework import PolicyError, UnknownPolicyError
from apps.container_logistics.policies.compat.legacy_env import (
    apply_market_override,
    apply_planner_topology_override,
    known_planner_topologies,
)


def _collection(planner=None):
    """A minimal {agent_id: behavior} assignment collection, as ScenarioManager loads."""
    profile = {"strategy": "GreedyNearest"}
    if planner is not None:
        profile["planner"] = copy.deepcopy(planner)
    return {"assignment_main": {"profile": profile}}


def _planner(coll):
    return coll["assignment_main"]["profile"]["planner"]


# --- topology override -------------------------------------------------------

def test_topology_override_applies_and_is_fail_loud():
    coll = _collection({"topology": "partitioned", "timing": {"mode": "online"}})

    # applies
    assert apply_planner_topology_override(coll, "pooled") == "pooled"
    assert _planner(coll)["topology"] == "pooled"
    # untouched sibling keys survive
    assert _planner(coll)["timing"] == {"mode": "online"}

    # fail-loud (round 2): an unknown topology raises and leaves the value alone
    with pytest.raises(PolicyError, match="'nonsense' is not a legacy topology"):
        apply_planner_topology_override(coll, "nonsense")
    assert _planner(coll)["topology"] == "pooled"

    # falsy input is a no-op
    assert apply_planner_topology_override(coll, None) is None
    assert apply_planner_topology_override(coll, "") is None
    assert _planner(coll)["topology"] == "pooled"


def test_two_stage_topology_override_resolves_to_pooled():
    """'two-stage' is a deprecated alias — the runtime must never see it."""
    coll = _collection({"topology": "partitioned"})
    assert apply_planner_topology_override(coll, "two-stage") == "pooled"
    assert _planner(coll)["topology"] == "pooled"


def test_topology_override_creates_planner_block_on_legacy_bundle():
    """A bundle compiled before the planner block existed is still runnable both ways."""
    coll = _collection(planner=None)
    assert apply_planner_topology_override(coll, "pooled") == "pooled"
    assert _planner(coll)["topology"] == "pooled"


def test_topology_override_default_is_never_flipped_implicitly():
    """No override => the scenario's own topology stands, untouched (plan §D5)."""
    coll = _collection({"topology": "partitioned"})
    before = json.dumps(coll, sort_keys=True)
    assert apply_planner_topology_override(coll, None) is None
    assert json.dumps(coll, sort_keys=True) == before


# --- market override ---------------------------------------------------------

def test_market_override_patches_only_loaded_behaviors():
    """The on-disk bundle must be untouched: we mutate the loaded dict only."""
    on_disk = {
        "topology": "pooled",
        "market": {
            "offer": {"type": "OfferAll", "params": {"keep_below_km": 3.0}},
            "claim": {"type": "ClaimAllPlanned", "params": {}},
            "arbitration": {"type": "LowestCost", "params": {}},
            "max_rounds": 2,
        },
    }
    pristine = copy.deepcopy(on_disk)
    coll = _collection(on_disk)  # _collection deep-copies, standing in for a load

    applied = apply_market_override(coll, offer="OfferSpare", max_rounds=1)
    assert applied == {"offer": "OfferSpare", "max_rounds": 1}

    market = _planner(coll)["market"]
    assert market["offer"]["type"] == "OfferSpare"
    assert market["max_rounds"] == 1
    # round 2: a changed component type resets its params (to OfferSpare's defaults; the
    # unset keep_below_km is omitted), and the mirror has the legacy shape
    assert market["offer"]["params"] == {"only_when_short": True}
    assert set(market) == {"offer", "claim", "arbitration", "max_rounds"}
    # roles that were not requested are untouched
    assert market["claim"]["type"] == "ClaimAllPlanned"
    assert market["arbitration"]["type"] == "LowestCost"

    # the source dict we were handed is not the one that got mutated
    assert on_disk == pristine


def test_market_override_is_noop_when_nothing_requested():
    coll = _collection({"topology": "pooled", "market": {"max_rounds": 2}})
    before = json.dumps(coll, sort_keys=True)
    assert apply_market_override(coll) is None
    assert json.dumps(coll, sort_keys=True) == before


def test_market_override_rejects_out_of_range_max_rounds():
    """max_rounds is a FRAMEWORK constant -> hard-bounded 1..100 (round 2: loud)."""
    coll = _collection({"topology": "pooled", "market": {"max_rounds": 2}})
    for bad in (0, 101, "x"):
        with pytest.raises(PolicyError, match="must be an integer in 1..100"):
            apply_market_override(coll, max_rounds=bad)
    assert _planner(coll).get("market", {}).get("max_rounds") == 2
    assert apply_market_override(coll, max_rounds=11) == {"max_rounds": 11}
    assert _planner(coll)["market"]["max_rounds"] == 11


def test_market_override_rejects_unknown_component_names():
    """Round 2: component NAMES are validated (the runtime is fail-loud too), naming the
    registered components; nothing is mutated."""
    coll = _collection({"topology": "pooled"})
    before = json.dumps(coll, sort_keys=True)
    with pytest.raises(UnknownPolicyError, match="'TypoRule'.*Registered: HighestBenefit, LowestCost"):
        apply_market_override(coll, arbitration="TypoRule")
    assert json.dumps(coll, sort_keys=True) == before


def test_market_override_creates_market_block_on_legacy_bundle():
    coll = _collection({"topology": "pooled"})  # no market key at all
    assert apply_market_override(coll, claim="ClaimNone") == {"claim": "ClaimNone"}
    assert _planner(coll)["market"]["claim"] == {"type": "ClaimNone", "params": {}}


# --- single-source guard -----------------------------------------------------

def test_known_planner_topologies_matches_preprocess_constant():
    """The override validator and the compiler must never drift (plan §7)."""
    assert known_planner_topologies() == tuple(PLANNER_TOPOLOGIES)
    assert "partitioned" in known_planner_topologies()
    assert "pooled" in known_planner_topologies()
