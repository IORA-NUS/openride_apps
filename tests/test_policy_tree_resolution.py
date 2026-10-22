"""Nested selection resolution, merge and stamp (plan round 2 §3.2, §3.5, §3.7; §5 C2).

What is locked:
- a ``policies.planner`` TREE resolves through refs with the spec's exact messages
  1 (unknown inner), 2 (a composite where a leaf is required), 4 (a ref that is not an
  object) and 5 (an inner's param written on the composite);
- ``merge_policy_overrides`` merges nested params ref by ref: same ref type merges,
  a changed ref type (or a bare string, the CLI's ``planner.inner=X``) resets that
  ref's params;
- the compiled form (``to_profile``) carries each ref's ``version``, reads back
  unchanged (the ``version`` echo is ignored), and the stamp carries ``label``,
  ``leaf``, ``information_model``, ``share_tag_format`` and the nested params;
- legacy-provenance params are filtered recursively (undeclared keys of the inner and
  the components dropped at WARNING);
- TEMPORARY (C2 -> C3): a block that names no dispatch resolves to the slot default
  ``Partitioned`` when it is registered, else to the round-1 leaf default.

Why: the tree is now the only dial (spec, CLI, stamp, schema, form); a resolver that
silently flattened or mis-merged it would run a different arm than the one recorded.
The decompositions are the real ``Partitioned``/``PooledMarket`` (registered since C3;
``tests/policy_tree_standins.decompositions()`` leaves them in place).
"""

from __future__ import annotations

import json
import logging

import pytest

from apps.container_logistics.policies.dispatch.profile import resolve_profile_policies
from apps.container_logistics.policies.framework import (
    PolicyParamsError,
    UnknownPolicyError,
    merge_policy_overrides,
    policies_stamp,
    resolve_policies_block,
)
from tests.policy_registry_names import dispatch_names, leaf_names
from tests.policy_tree_standins import decompositions

_GREEDY = {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 2.0}}


def _block(top, **params):
    return {"planner": {"type": top, "params": params}}


def test_unknown_inner_is_message_1():
    """Verified to have teeth: disabling the unknown-name check of
    ``components._dispatch_target`` (falling through to the registry's generic message)
    failed this test."""
    with decompositions():
        with pytest.raises(UnknownPolicyError) as exc:
            resolve_policies_block(_block("Partitioned", inner={"type": "GreedyNeerest"}))
        # The list is the registry's (MINOR-7): a new leaf changes it on both sides.
        assert str(exc.value) == (
            "policies.planner.params.inner: unknown planner policy 'GreedyNeerest'. Registered planner "
            f"policies: {dispatch_names()}. (List them: openride "
            "policy list planner)")


def test_composite_inner_is_message_2():
    """Verified to have teeth: declaring the stand-in's ``inner`` with ``leaf_only=False``
    let ``PooledMarket`` nest."""
    with decompositions():
        with pytest.raises(PolicyParamsError) as exc:
            resolve_policies_block(_block("Partitioned", inner={"type": "PooledMarket"}))
        assert str(exc.value) == (
            "policies.planner.params.inner: 'PooledMarket' decomposes the tick itself (it has an "
            f"'inner' of its own); an inner must be a per-scope solver: {leaf_names()}.")


def test_string_ref_is_message_4():
    """Verified to have teeth: coercing a bare string into ``{type}`` in
    ``params._ref_entry`` let the spec form through."""
    with decompositions(), pytest.raises(PolicyParamsError) as exc:
        resolve_policies_block(_block("Partitioned", inner="GreedyNearest"))
    assert str(exc.value) == (
        "policies.planner.params.inner must be an object {type, params?} (got 'GreedyNearest'). "
        'In spec.json write {"type": "GreedyNearest"}; on the CLI --policy-param '
        "planner.inner=GreedyNearest is accepted.")


def test_inner_param_on_the_composite_is_message_5():
    """Verified to have teeth: removing the ``_inner_key_hint`` decoration failed the exact
    message; the hint is absent when the inner does not declare the key."""
    with decompositions():
        current = resolve_policies_block(_block("Partitioned", inner=_GREEDY))
        with pytest.raises(PolicyParamsError) as exc:
            merge_policy_overrides(current, {"planner": {"params": {"dual_cycle_bonus_km": 0}}})
        assert str(exc.value) == (
            "policy overrides.planner.params for Partitioned: unknown key 'dual_cycle_bonus_km'. "
            "Declared params: inner. 'dual_cycle_bonus_km' is a param of the inner GreedyNearest: "
            "use planner.inner.dual_cycle_bonus_km.")
        random_inner = resolve_policies_block(_block("Partitioned"))
        with pytest.raises(PolicyParamsError) as exc:
            merge_policy_overrides(random_inner, {"planner": {"params": {"dual_cycle_bonus_km": 0}}})
        assert "is a param of the inner" not in str(exc.value)


def test_nested_merge_same_ref_type_merges_changed_type_resets():
    """Verified to have teeth: replacing a ref value wholesale in ``merge_params`` (no
    recursion) dropped ``dual_cycle_bonus_km=2.0`` in the same-type case; never
    resetting kept GreedyNearest's params on the type change; dropping
    ``_carried_inner`` (C3) reset the top-change's inner to RandomAssignment."""
    with decompositions():
        current = resolve_policies_block(_block("PooledMarket", inner=_GREEDY, max_rounds=5))
        same = merge_policy_overrides(current, {"planner": {"params": {
            "inner": {"params": {"candidates": "sampled"}}}}})
        assert same["planner"].params["inner"] == {"type": "GreedyNearest", "params": {
            "candidates": "sampled", "dual_cycle_bonus_km": 2.0, "dual_cycle_radius_km": 0.5}}
        assert same["planner"].params["max_rounds"] == 5
        assert same["planner"].params["offer"] == {"type": "OfferAll", "params": {}}
        changed = merge_policy_overrides(current, {"planner": {"params": {"inner": "RandomAssignment",
                                                                           "offer": {"type": "OfferSpare"}}}})
        assert changed["planner"].params["inner"] == {"type": "RandomAssignment", "params": {"candidates": "sampled"}}
        assert changed["planner"].params["offer"] == {"type": "OfferSpare", "params": {
            "keep_below_km": None, "only_when_short": True}}
        assert changed["planner"].label == "PooledMarket/RandomAssignment"
        # C3 (C2 review finding 2): a changed decomposition keeps the current solver.
        top = merge_policy_overrides(current, {"planner": {"type": "Partitioned"}})
        assert top["planner"].params == {"inner": current["planner"].params["inner"]}
        assert top["planner"].label == "Partitioned/GreedyNearest"
        with pytest.raises(UnknownPolicyError, match=r"^policy overrides.planner.params.inner: unknown planner"):
            merge_policy_overrides(current, {"planner": {"params": {"inner": "Nope"}}})


def test_offer_param_of_a_component_merges_into_its_current_type():
    """A component ref merges like a dispatch ref (same type: params merge).

    Verified to have teeth: replacing a ref value wholesale in ``merge_params`` failed this test.
    """
    with decompositions():
        current = resolve_policies_block(_block("PooledMarket", offer={"type": "OfferSpare",
                                                                       "params": {"only_when_short": False}}))
        merged = merge_policy_overrides(current, {"planner": {"params": {"offer": {"params": {"keep_below_km": 3}}}}})
    assert merged["planner"].params["offer"] == {"type": "OfferSpare", "params": {
        "keep_below_km": 3.0, "only_when_short": False}}


def test_compiled_form_carries_versions_and_reads_back():
    """Verified to have teeth: emitting ``dict(self.params)`` in ``to_profile`` dropped the
    nested versions."""
    with decompositions():
        resolved = resolve_policies_block(_block("PooledMarket", inner=_GREEDY))
        compiled = resolved["planner"].to_profile()
        assert compiled["params"]["inner"] == {"type": "GreedyNearest", "version": "1", "params": {
            "candidates": "spatial", "dual_cycle_bonus_km": 2.0, "dual_cycle_radius_km": 0.5}}
        assert compiled["params"]["offer"] == {"type": "OfferAll", "version": "1", "params": {}}
        again, source = resolve_profile_policies({"policies": {"planner": compiled}})
    assert source == "bundle" and again == resolved, "the version echo must be ignored on read"
    assert again["planner"].label == "PooledMarket/GreedyNearest"


def test_stamp_is_nested_and_labelled():
    """Verified to have teeth: stamping ``params=dict(r.params)`` dropped the ref versions."""
    with decompositions():
        resolved = resolve_policies_block(_block("Partitioned", inner=_GREEDY))
        stamp = policies_stamp(resolved, candidate_settings={}, accounting_params={"dual_cycle_bonus_km": 2.0},
                               source="override")["planner"]
    assert (stamp["name"], stamp["label"], stamp["leaf"], stamp["information_model"],
            stamp["share_tag_format"]) == ("Partitioned", "Partitioned/GreedyNearest", "GreedyNearest",
                                           "per_group", "v1")
    assert stamp["params"] == {"inner": {"type": "GreedyNearest", "version": "1", "params": {
        "candidates": "spatial", "dual_cycle_bonus_km": 2.0, "dual_cycle_radius_km": 0.5}}}
    json.dumps(stamp)


def test_legacy_params_are_filtered_recursively(caplog):
    """Verified to have teeth: filtering the top level only left ``bogus`` in the inner
    and the request was refused as an unknown key."""
    block = _block("PooledMarket", inner={"type": "RandomAssignment", "params": {"dual_cycle_bonus_km": 5.0}},
                   offer={"type": "OfferAll", "params": {"bogus": 1}})
    with decompositions(), caplog.at_level(logging.WARNING, logger="apps.container_logistics.policies.framework.resolve"):
        resolved = resolve_policies_block(block, legacy_slots=("planner",))
    assert resolved["planner"].params["inner"] == {"type": "RandomAssignment", "params": {"candidates": "sampled"}}
    assert resolved["planner"].params["offer"] == {"type": "OfferAll", "params": {}}
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "dual_cycle_bonus_km" in messages and "bogus" in messages


def test_unnamed_dispatch_is_the_slot_default():
    """A block that does not name dispatch resolves to the slot default ``Partitioned``
    with its default inner (C3 removed the C2 round-1 fallback; ``Partitioned`` is
    registered).

    Verified to have teeth: making ``merge_policy_overrides``/``resolve_policies_block``
    fall back to ``legacy.LEGACY_LEAF_DEFAULT`` (the deleted C2 branch) failed this test.
    """
    assert resolve_policies_block({})["planner"].label == "Partitioned/RandomAssignment"
