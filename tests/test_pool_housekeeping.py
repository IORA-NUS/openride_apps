"""FIX-11 housekeeping (review F11, F15, F16, F17, S1) — one assertion each."""

import logging
import typing

from apps.container_logistics.assignment.pools import Award, pools_from_structure
from apps.container_logistics.policies.dispatch.profile import build_policy_tree, resolve_dispatch_for_profile
from apps.container_logistics.scenario.scenario_overrides import apply_cooperation_override

from tests.test_assignment_cooperation import _coop
from tests.test_assignment_pooled import _app


def test_duplicate_pool_ids_warn_instead_of_vanishing_silently(caplog):
    """F11: a MALFORMED entry warned while a duplicate id vanished quietly — the
    inconsistency was the tell."""
    structure = {"pools": [
        {"id": "p1", "members": ["a", "b"]},
        {"id": "p1", "members": ["c", "d"]},
    ]}
    with caplog.at_level(logging.WARNING):
        pools = pools_from_structure(structure)
    assert [p.id for p in pools] == ["p1"]          # last-wins is unchanged
    assert any("duplicate pool id" in r.message for r in caplog.records), (
        "a pool was silently discarded with no log line"
    )


def test_explicitly_empty_pools_are_honoured_not_treated_as_absent():
    """S1: ``pools: []`` written by hand means "no pools". Falling back to `edges`
    would hand that operator a full edge-derived market."""
    assert pools_from_structure({"pools": [], "edges": [["a", "b"]]}) == ()
    # ...while an ABSENT key still derives from edges (every shipped bundle).
    assert [p.id for p in pools_from_structure({"edges": [["a", "b"]]})] == ["p:a+b"]


def test_award_owner_haulier_id_is_annotated_optional():
    """F15: arbitration fills this from ``owner_of()``, which returns Optional[str],
    so the annotation must not claim otherwise."""
    hints = typing.get_type_hints(Award)
    assert hints["owner_haulier_id"] == typing.Optional[str]
    Award(order_id="o", truck_id="t", carrier_haulier_id="a", owner_haulier_id=None,
          cost_km=1.0, pool_id=None, round=1)  # must construct cleanly


def test_market_components_pick_up_a_behavior_patched_after_construction():
    """F16: the topology was re-read from the profile while the market components
    cached forever — two adjacent resolvers arguing opposite staleness positions.
    Round 2: both are the dispatch runner's selection, keyed on ``planner``; and the
    INSTANCE that runs must follow it, not only the reported selection (review C3 #4),
    so ``_solver`` is the full tree ``__init__`` builds.

    Verified to have teeth: reusing the top instance unconditionally in
    ``profile.bind_instance`` failed the instance and component assertions while the
    ``resolved`` one still passed.
    """
    app = _app([], [], cooperation=_coop([]))
    profile = {"strategy": "GreedyNearest", "planner": {"topology": "pooled", "market": {"max_rounds": 3}}}
    app._solver = build_policy_tree(resolve_dispatch_for_profile(profile))
    first = app._dispatch_runner(profile)
    assert first.resolved.params.max_rounds == 3 and first.policy.params["max_rounds"] == 3
    profile["planner"]["market"]["max_rounds"] = 9
    profile["planner"]["market"]["offer"] = {"type": "OfferNone"}
    runner = app._dispatch_runner(profile)
    assert runner.resolved.params.max_rounds == 9, "stale cache survived a profile patch"
    assert runner.policy.params["max_rounds"] == 9, "the instance that runs kept the stale backstop"
    assert type(runner.policy.components()[0]).__name__ == "OfferNonePolicy", "stale market components"


def test_sharing_override_creates_a_planner_block_like_the_topology_override_does():
    """F17: one module created a missing planner block, the adjacent one warned and
    refused. Aligned on CREATE, because §8 promises old bundles stay runnable under
    a per-run override without recompiling."""
    collection = {"assignment_main": {"profile": {"strategy": "GreedyNearest"}}}
    apply_cooperation_override(collection, sharing="transfer-when-cheaper")
    planner = collection["assignment_main"]["profile"]["planner"]
    assert planner["sharing"]["type"] == "transfer-when-cheaper"
