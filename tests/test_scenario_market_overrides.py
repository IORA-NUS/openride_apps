"""Per-run planner-topology / pooled-market overrides (shared-pool plan §6.11, Phase 6).

These mirror the existing solver/cooperation override contract: pure data patched
onto the LOADED assignment behaviors before agents spawn, fail-soft on bad input,
and the on-disk bundle never touched.
"""

import copy
import json

import pytest

from apps.container_logistics.datagen.preprocess import PLANNER_TOPOLOGIES
from apps.container_logistics.scenario.scenario_overrides import (
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

def test_topology_override_applies_and_is_fail_soft():
    coll = _collection({"topology": "partitioned", "timing": {"mode": "online"}})

    # applies
    assert apply_planner_topology_override(coll, "pooled") == "pooled"
    assert _planner(coll)["topology"] == "pooled"
    # untouched sibling keys survive
    assert _planner(coll)["timing"] == {"mode": "online"}

    # fail-soft: unknown topology is IGNORED (not raised) and leaves the value alone
    assert apply_planner_topology_override(coll, "nonsense") is None
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
    # existing params of the patched role are PRESERVED (only 'type' is replaced)
    assert market["offer"]["params"] == {"keep_below_km": 3.0}
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
    """max_rounds is a FRAMEWORK constant -> hard-bounded, unlike algorithm names."""
    coll = _collection({"topology": "pooled", "market": {"max_rounds": 2}})
    assert apply_market_override(coll, max_rounds=0) is None
    assert apply_market_override(coll, max_rounds=11) is None
    assert apply_market_override(coll, max_rounds="not-a-number") is None
    assert _planner(coll)["market"]["max_rounds"] == 2


def test_market_override_accepts_unknown_algorithm_names_fail_soft_at_runtime():
    """Algorithm NAMES are deliberately not validated here — the registries fall
    back at runtime, exactly like deployment.type. A typo must degrade a run, not
    abort the launch."""
    coll = _collection({"topology": "pooled"})
    applied = apply_market_override(coll, arbitration="TypoRule")
    assert applied == {"arbitration": "TypoRule"}
    assert _planner(coll)["market"]["arbitration"] == {"type": "TypoRule", "params": {}}


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
