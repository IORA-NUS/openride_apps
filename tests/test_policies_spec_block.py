"""The ``policies`` block in ``spec.json``: shape, legacy translation, mirrors, bundle.

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.1-P2.3):
- ``policies.dispatch`` is the ONE dispatch dial. Without it the legacy keys
  (``solver`` > ``planner.deployment.type`` > default; ``deployment.params`` then
  ``solverParams``) are translated, and ``profile.strategy`` / ``solver_params`` /
  ``planner.deployment`` are mirrors of whatever won.
- The legacy spellings may not contradict a ``policies`` block; dead
  ``planner.timing`` modes, unknown/planned/legacy slots, unknown entry keys, the
  not-yet-executable ``plan`` timing and unknown params are refused loudly.
- The bundle's assignment profile carries the RESOLVED block (version, defaults);
  the recipe echoes the AUTHORED block.
- Invariant I1: an equivalent ``policies`` block never perturbs generated agents.

Why: a policy selection that is silently coerced, dropped or contradicted runs a
different experiment than the one the author configured.
"""

from __future__ import annotations

import glob
import json
import logging
from copy import deepcopy
from pathlib import Path

import pytest

from apps.container_logistics.datagen import preprocess as P
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError
from apps.container_logistics.policies.framework import resolve as R

DOMAIN = "container-logistics-sim-test"
REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO_ROOT / "scenarios" / "collab_smoke_12t" / "spec.json"


def _spec(**kw):
    spec = {
        "name": "Policies Test", "slug": "policies_test", "simulationDays": 1,
        "orderCountUnit": "total",
        "agents": {"truck": {"count": 6}, "order": {"count": 12}, "facility": {"count": 30}},
        "earlyOrderCount": 0,
    }
    spec.update(deepcopy(kw))
    return spec


def _compile(raw):
    return Preprocessor.compile(deepcopy(raw), domain=DOMAIN)


def _profile(compiled):
    return compiled.spec.assignment_settings["profile"]


def _bundle(tmp_path, monkeypatch, spec, name="b"):
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(tmp_path / "root"))
    out = tmp_path / name
    out.mkdir(exist_ok=True)
    return compile_spec_to_bundle(deepcopy(spec), domain=DOMAIN, scenario_dir=str(out),
                                  slug=spec["slug"])


def _assignment_profile(bundle):
    return next(iter(bundle["agents"]["assignment"].values()))["profile"]


def test_policy_entry_keys_match_the_framework():
    """datagen cannot import the registry, so its shape list is a copy: keep it equal."""
    assert P.POLICY_ENTRY_KEYS == R.ENTRY_KEYS


def test_legacy_solver_translates_to_policies_dispatch():
    compiled = _compile(_spec(solver="GreedyNearest", solverParams={"dual_cycle_bonus_km": 3.0}))
    assert compiled.policies_provenance == {"dispatch": "legacy"}
    assert compiled.recipe["policies"] == {"dispatch": {
        "type": "GreedyNearest", "timing": "online", "params": {"dual_cycle_bonus_km": 3.0}}}
    assert _profile(compiled)["strategy"] == "GreedyNearest"
    assert _profile(compiled)["solver_params"]["dual_cycle_bonus_km"] == 3.0


def test_planner_deployment_translates_when_no_solver():
    compiled = _compile(_spec(planner={"deployment": {
        "type": "GreedyNearest", "params": {"dual_cycle_radius_km": 0.9}}}))
    entry = compiled.recipe["policies"]["dispatch"]
    assert entry["type"] == "GreedyNearest"
    assert entry["params"] == {"dual_cycle_radius_km": 0.9}
    # And with neither, the slot default (the defaults.py strategy).
    assert _compile(_spec()).recipe["policies"]["dispatch"]["type"] == "RandomAssignment"


def test_policies_block_wins_and_is_mirrored_into_strategy_and_deployment():
    """Verified to have teeth: mirroring the legacy ``deployment.type`` instead of
    ``policies.dispatch.type`` into ``strategy`` failed the first assertion."""
    compiled = _compile(_spec(
        planner={"deployment": {"type": "RandomAssignment", "params": {"dual_cycle_bonus_km": 9.0}}},
        policies={"dispatch": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 1.5}}},
    ))
    prof = _profile(compiled)
    assert prof["strategy"] == "GreedyNearest"
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 1.5, "authored params beat deployment"
    assert prof["planner"]["deployment"] == {"type": "GreedyNearest", "params": prof["solver_params"]}
    assert compiled.recipe["solver"] == "GreedyNearest" and compiled.recipe["solverParams"] is None
    assert compiled.policies_provenance == {"dispatch": "authored"}


def test_solver_disagreeing_with_policies_dispatch_is_rejected():
    """Verified to have teeth: deleting the solver-vs-type check let this compile."""
    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(solver="RandomAssignment", policies={"dispatch": {"type": "GreedyNearest"}}))
    assert "solver='RandomAssignment' but policies.dispatch.type='GreedyNearest'" in str(exc.value)
    assert "legacy spelling" in str(exc.value)
    # An AGREEING solver is fine (a carried spec holds both spellings).
    _compile(_spec(solver="GreedyNearest", policies={"dispatch": {"type": "GreedyNearest"}}))


def test_solverparams_with_policies_block_is_rejected():
    """Verified to have teeth: deleting the solverParams check let this compile."""
    with pytest.raises(SpecValidationError, match="solverParams is the legacy spelling"):
        _compile(_spec(solverParams={"dual_cycle_bonus_km": 2.0},
                       policies={"dispatch": {"type": "GreedyNearest"}}))
    # Deviation (agreeing values tolerated, symmetric with `solver`): see preprocess.
    _compile(_spec(solverParams={"dual_cycle_bonus_km": 2.0},
                   policies={"dispatch": {"type": "GreedyNearest",
                                          "params": {"dual_cycle_bonus_km": 2.0}}}))


def _resolve_error(tmp_path, monkeypatch, policies):
    with pytest.raises(SpecValidationError) as exc:
        _bundle(tmp_path, monkeypatch, _spec(policies=policies))
    return str(exc.value)


def test_unknown_slot_rejected(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch, {"dispach": {"type": "GreedyNearest"}})
    assert msg.startswith("Unknown policy slot 'dispach' in 'policies'. Known slots: dispatch, market.offer")


def test_planned_slot_rejected_with_phase(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch, {"idle": {"type": "Stay"}})
    assert msg == ("Policy slot 'idle' is declared but not implemented yet (lands in P5). "
                   "Remove 'policies.idle'.")


def test_legacy_market_slot_points_to_planner_market(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch, {"market.offer": {"type": "OfferAll"}})
    assert msg == ("'policies.market.offer' is not selectable here yet; configure it via "
                   "planner.market.offer (lands in P5).")


def test_unknown_entry_key_rejected():
    with pytest.raises(SpecValidationError, match=r"policies.dispatch: unknown key\(s\) 'parms'"):
        _compile(_spec(policies={"dispatch": {"type": "GreedyNearest", "parms": {}}}))


def test_plan_timing_rejected_with_p4_message(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch,
                         {"dispatch": {"type": "GreedyNearest", "timing": "plan"}})
    assert msg == ("policies.dispatch.timing='plan' is defined but not executable yet: the "
                   "held-plan dispatcher lands with P4. Use 'online'.")
    msg = _resolve_error(tmp_path, monkeypatch,
                         {"dispatch": {"type": "GreedyNearest", "timing": "weekly"}})
    assert msg == "policies.dispatch.timing='weekly' is not a dispatch timing. Allowed: online, plan."


def test_planner_timing_static_rejected_with_translation():
    """Verified to have teeth: removing the mode check let 'static' compile silently."""
    for mode in ("static", "hybrid"):
        with pytest.raises(SpecValidationError) as exc:
            _compile(_spec(planner={"timing": {"mode": mode}}))
        assert str(exc.value) == (
            f"planner.timing.mode={mode!r} has never had a runtime reader; dispatch timing "
            "is now policies.dispatch.timing ('online'; 'plan' lands with P4).")
    _compile(_spec(planner={"timing": {"mode": "online"}}))


def test_unknown_param_rejected_in_policies_block(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch,
                         {"dispatch": {"type": "GreedyNearest", "params": {"bonus_km": 1}}})
    assert "unknown key 'bonus_km'" in msg and "Declared params: candidates" in msg


def test_undeclared_param_dropped_with_warning_in_legacy_translation(tmp_path, monkeypatch, caplog):
    """Random declares no dual-cycle params; legacy solver_params were never validated.

    Verified to have teeth: passing ``legacy_slots=()`` from spec_compile made this
    compile fail with "unknown key 'dual_cycle_bonus_km'".
    """
    with caplog.at_level(logging.WARNING):
        bundle = _bundle(tmp_path, monkeypatch, _spec(
            solver="RandomAssignment", solverParams={"dual_cycle_bonus_km": 7.0}))
    assert any("not declared by dispatch RandomAssignment" in r.getMessage() for r in caplog.records)
    assert bundle["recipe"]["policies"]["dispatch"]["params"] == {}
    prof = _assignment_profile(bundle)
    assert prof["policies"]["dispatch"]["params"] == {"candidates": "sampled"}
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 7.0, "accounting keeps the legacy value"


def test_bundle_profile_carries_resolved_policies_with_version_and_defaults(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, monkeypatch, _spec(
        policies={"dispatch": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}))
    prof = _assignment_profile(bundle)
    assert prof["policies"] == {"dispatch": {
        "type": "GreedyNearest", "version": "1", "kind": "operator", "timing": "online",
        "params": {"candidates": "spatial", "dual_cycle_bonus_km": 0.0, "dual_cycle_radius_km": 0.5}}}
    assert prof["strategy"] == "GreedyNearest"
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 0.0, "accounting mirrors the policy"
    from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile

    resolved = resolve_dispatch_for_profile(prof)
    assert resolved.source == "policies" and resolved.params.dual_cycle_bonus_km == 0.0


def test_stale_deployment_mirror_never_outlives_an_authored_param(tmp_path, monkeypatch):
    """A removed authored param falls back to the POLICY default in solver_params too.

    The recipe's ``planner.deployment.params`` mirror carries the previous compile's
    values; without the re-mirror, accounting kept bonus 0 while the policy ran 5.0.
    Verified to have teeth: removing the re-mirror loop in
    ``spec_compile._resolve_compiled_policies`` left solver_params at 0.0.
    """
    first = _bundle(tmp_path, monkeypatch, _spec(
        policies={"dispatch": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}), "a")
    recipe = deepcopy(first["recipe"])
    recipe["policies"] = {"dispatch": {"type": "GreedyNearest"}}
    second = _bundle(tmp_path, monkeypatch, recipe, "b")
    prof = _assignment_profile(second)
    assert prof["policies"]["dispatch"]["params"]["dual_cycle_bonus_km"] == 5.0
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 5.0


def test_legacy_use_spatial_false_in_a_profile_is_folded_and_removed(tmp_path, monkeypatch):
    """A profile key arriving via a scenario_gen.py override must not reach the bundle
    next to a ``policies`` block (the runtime resolver refuses that pair).

    Verified to have teeth: replacing ``prof.pop`` with ``prof.get`` in
    ``_profile_policies`` left the key on the profile next to the ``policies`` block.
    """
    from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile
    from apps.container_logistics.scenario import spec_compile as SC

    resolved = R.resolve_policies_block({"dispatch": {"type": "GreedyNearest"}})
    prof = {"strategy": "GreedyNearest", "use_spatial_matching": False}
    SC._patch_assignment_profiles({"a0": {"profile": prof}}, resolved, "legacy")
    assert "use_spatial_matching" not in prof
    assert prof["policies"]["dispatch"]["params"]["candidates"] == "sampled"
    assert resolve_dispatch_for_profile(prof).candidate_kind == "sampled"
    authored = {"strategy": "GreedyNearest", "use_spatial_matching": False}
    with pytest.raises(SpecValidationError, match="candidates='sampled' instead"):
        SC._patch_assignment_profiles({"a0": {"profile": authored}}, resolved, "authored")


def test_recipe_echoes_authored_not_resolved(tmp_path, monkeypatch):
    authored = {"dispatch": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}
    bundle = _bundle(tmp_path, monkeypatch, _spec(policies=authored))
    assert bundle["recipe"]["policies"] == authored, "defaults/version must not leak into the recipe"
    assert bundle["settings"]["GENERATION_SPEC"]["policies"] == authored


def test_policies_block_never_perturbs_generated_agents(tmp_path, monkeypatch):
    """Invariant I1: trucks, orders and facilities are byte-equal with and without an
    equivalent ``policies`` block (the selection is carried data, not a sampling input).

    Verified to have teeth: adding ``+ (1 if raw.get("policies") else 0)`` to the
    master seed in ``Preprocessor.compile`` failed the agent comparison.
    """
    spec = json.loads(SMOKE_SPEC.read_text())
    legacy = _bundle(tmp_path, monkeypatch, spec, "legacy")
    with_block = deepcopy(spec)
    with_block["policies"] = deepcopy(legacy["recipe"]["policies"])
    with_block["solver"] = None
    authored = _bundle(tmp_path, monkeypatch, with_block, "authored")
    for role in ("truck", "order", "facility"):
        a = json.dumps(legacy["agents"][role], sort_keys=True)
        b = json.dumps(authored["agents"][role], sort_keys=True)
        assert a == b, f"{role} agents changed when the policies block was added"
    assert _assignment_profile(legacy)["policies"] == _assignment_profile(authored)["policies"]


@pytest.mark.parametrize("path", sorted(glob.glob(str(REPO_ROOT / "scenarios" / "*" / "spec.json"))))
def test_every_shipped_spec_translates_to_its_current_solver(path):
    """Shape-only: each shipped spec's dispatch translation names its old solver."""
    raw = json.loads(Path(path).read_text())
    planner_cfg = Preprocessor._normalize_planner(raw.get("planner"))
    expected = raw.get("solver") or (planner_cfg.get("deployment") or {}).get("type") or "RandomAssignment"
    authored, provenance = Preprocessor._normalize_policies(raw, planner_cfg)
    assert provenance["dispatch"] == ("authored" if "policies" in raw else "legacy")
    assert authored["dispatch"]["type"] == expected
