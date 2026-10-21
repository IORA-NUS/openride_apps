"""Per-run policy overrides: CLI parsing, merge rules, profile patching, the run stamp.

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.2, P2.5, P2.6):
- ``--policy-param`` values are JSON when they parse, else the raw string; slot
  names may contain dots.
- A changed type resets params to the new policy's defaults; the same type merges.
  Every merge is re-validated (no silent acceptance of a bad override).
- ``ORSIM_SOLVER`` still works and is ``dispatch.type`` sugar; disagreeing with
  ``ORSIM_POLICY_OVERRIDES`` is refused.
- The override patches ``profile.policies`` and the ``strategy`` mirror, never
  ``solver_params``, and a legacy profile's ``use_spatial_matching`` is folded away
  (the runtime resolver refuses it next to ``policies``).
- The stamp shape, its ``run_config.meta`` mirror, and ``run-simulation`` refusing a
  bad override WITHOUT spawning the sim.

Why: an override that is silently dropped or coerced runs a different arm than the
one the run name and stamp claim.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile
from apps.container_logistics.policies.framework import (
    PolicyError,
    PolicyParamsError,
    UnknownPolicyError,
    merge_policy_overrides,
    parse_policy_cli_args,
    policies_stamp,
    resolve_policies_block,
)
from apps.container_logistics.scenario.scenario_overrides import (
    apply_policy_overrides,
    apply_solver_override,
)


def _collection(profile):
    return {"assignment_0": {"profile": deepcopy(profile)}}


def _greedy(**params):
    return resolve_policies_block({"dispatch": {"type": "GreedyNearest", "params": params}})


def test_parse_policy_cli_args_json_coercion():
    """Round 2 (§3.3): the slot is the longest known prefix; every further non-final
    segment is a policy reference.

    Verified to have teeth: treating every segment after the slot as one dotted key
    (no nesting) failed the ``offer``/``inner`` assertions; matching the SHORTEST
    known prefix is untestable with today's one implemented slot, so the dotted
    planned slot ``world.shedding`` locks it."""
    out = parse_policy_cli_args(
        ["dispatch=GreedyNearest"],
        ["dispatch.dual_cycle_bonus_km=0", "dispatch.candidates=sampled", "dispatch.flag=true",
         "dispatch.quoted=\"60\"", "dispatch.offer.keep_below_km=1.5", "dispatch.eq=a=b",
         "dispatch.inner=RandomAssignment", "dispatch.inner.candidates=spatial",
         "world.shedding.x=1"],
    )
    assert out["dispatch"]["type"] == "GreedyNearest"
    p = out["dispatch"]["params"]
    assert p["dual_cycle_bonus_km"] == 0 and isinstance(p["dual_cycle_bonus_km"], int)
    assert p["candidates"] == "sampled" and p["flag"] is True and p["quoted"] == "60"
    assert p["eq"] == "a=b"
    assert p["offer"] == {"params": {"keep_below_km": 1.5}}
    assert p["inner"] == {"type": "RandomAssignment", "params": {"candidates": "spatial"}}
    assert out["world.shedding"] == {"params": {"x": 1}}
    with pytest.raises(PolicyError, match=r"^--policy-param 'dispach.inner=X': no known slot is a prefix "
                                          r"of 'dispach.inner'. Known slots: dispatch.$"):
        parse_policy_cli_args([], ["dispach.inner=X"])
    for bad_policy, bad_param in ((["dispatch"], []), ([], ["dispatch=1"]), ([], ["dispatch.k"])):
        with pytest.raises(PolicyError):
            parse_policy_cli_args(bad_policy, bad_param)
    with pytest.raises(PolicyError, match="twice"):
        parse_policy_cli_args(["dispatch=A", "dispatch=B"], [])


def test_type_change_resets_params_to_new_defaults():
    current = _greedy(dual_cycle_bonus_km=0, candidates="sampled")
    merged = merge_policy_overrides(current, {"dispatch": {"type": "RandomAssignment"}})
    assert merged["dispatch"].name == "RandomAssignment"
    assert merged["dispatch"].params == {"candidates": "sampled"}
    # Greedy's own param on the new type is not inherited; override params are applied.
    merged = merge_policy_overrides(
        current, {"dispatch": {"type": "RandomAssignment", "params": {"candidates": "spatial"}}})
    assert merged["dispatch"].params == {"candidates": "spatial"}


def test_same_type_merges_params():
    """Verified to have teeth: resetting params on every override lost candidates."""
    current = _greedy(dual_cycle_bonus_km=0, candidates="sampled")
    merged = merge_policy_overrides(current, {"dispatch": {"params": {"dual_cycle_radius_km": 1}}})
    assert merged["dispatch"].params == {
        "candidates": "sampled", "dual_cycle_bonus_km": 0.0, "dual_cycle_radius_km": 1.0}
    same = merge_policy_overrides(current, {"dispatch": {"type": "GreedyNearest"}})
    assert same["dispatch"].params == current["dispatch"].params


def test_invalid_override_param_raises():
    current = _greedy()
    with pytest.raises(PolicyParamsError, match=">= 0"):
        merge_policy_overrides(current, {"dispatch": {"params": {"dual_cycle_bonus_km": -1}}})
    with pytest.raises(PolicyParamsError, match="Values are not coerced from strings"):
        merge_policy_overrides(current, {"dispatch": {"params": {"dual_cycle_bonus_km": "5"}}})
    with pytest.raises(UnknownPolicyError):
        merge_policy_overrides(current, {"dispatch": {"type": "GreedyNeerest"}})
    with pytest.raises(PolicyError, match="Unknown policy slot"):
        merge_policy_overrides(current, {"dispach": {"type": "GreedyNearest"}})


def test_orsim_solver_still_works(monkeypatch):
    import apps.simulation.run_container_logistics_simulation as launcher

    monkeypatch.delenv("ORSIM_POLICY_OVERRIDES", raising=False)
    monkeypatch.setenv("ORSIM_SOLVER", "GreedyNearest")
    coll = _collection({"strategy": "RandomAssignment"})
    name, overridden = launcher._apply_policy_overrides_or_exit(coll)
    assert (name, overridden) == ("GreedyNearest", True)
    assert coll["assignment_0"]["profile"]["strategy"] == "GreedyNearest"
    assert apply_solver_override(_collection({}), "RandomAssignment") == "RandomAssignment"
    # A params-only override labels the policy with exactly the overridden params
    # (review F7; format in ``dispatch_override_label``).
    monkeypatch.delenv("ORSIM_SOLVER")
    monkeypatch.setenv("ORSIM_POLICY_OVERRIDES",  # round 2: the solver's params are the inner's
                       json.dumps({"dispatch": {"params": {"inner": {"params": {"dual_cycle_bonus_km": 0}}}}}))
    coll = _collection({"strategy": "GreedyNearest"})
    assert launcher._apply_policy_overrides_or_exit(coll) == (
        "GreedyNearest(dual_cycle_bonus_km=0.0)", True)
    monkeypatch.delenv("ORSIM_POLICY_OVERRIDES")
    assert launcher._apply_policy_overrides_or_exit(_collection({})) == (None, False)


def test_orsim_solver_conflicting_with_policy_override_raises(monkeypatch, capsys):
    """Verified to have teeth: removing the conflict check (ORSIM_SOLVER silently wins)
    failed the ``pytest.raises``."""
    import apps.simulation.run_container_logistics_simulation as launcher

    monkeypatch.setenv("ORSIM_SOLVER", "GreedyNearest")
    monkeypatch.setenv("ORSIM_POLICY_OVERRIDES", json.dumps({"dispatch": {"type": "RandomAssignment"}}))
    with pytest.raises(PolicyError) as exc:
        launcher._resolve_policy_overrides()
    assert str(exc.value) == ("ORSIM_SOLVER='GreedyNearest' conflicts with ORSIM_POLICY_OVERRIDES "
                              "dispatch.type='RandomAssignment'")
    with pytest.raises(SystemExit) as stop:
        launcher._apply_policy_overrides_or_exit(_collection({}))
    assert stop.value.code == 2
    assert capsys.readouterr().out.startswith("Policy configuration invalid; refusing to start the run:")


def test_override_patches_profile_policies_and_strategy_mirror():
    """Verified to have teeth: dropping the ``profile.pop("use_spatial_matching")`` in
    ``apply_policy_overrides`` left the legacy key next to the new ``policies`` block,
    which the runtime resolver refuses. The ``solver_params`` mirror (review F1) is
    locked with teeth in ``test_policy_override_equals_compiled_edit.py``."""
    legacy = {"strategy": "GreedyNearest", "use_spatial_matching": False,
              "solver_params": {"dual_cycle_bonus_km": 4.0, "dual_cycle_radius_km": 0.5}}
    coll = _collection(legacy)
    inner = {"dispatch": {"params": {"inner": {"params": {"dual_cycle_radius_km": 1.0}}}}}
    resolved = apply_policy_overrides(coll, inner)  # round 2: Partitioned/GreedyNearest's inner
    prof = coll["assignment_0"]["profile"]
    assert prof["strategy"] == "GreedyNearest"
    assert prof["policies"]["dispatch"] == resolved["dispatch"].to_profile()
    assert prof["policies"]["dispatch"]["params"]["inner"]["params"] == {
        "candidates": "sampled", "dual_cycle_bonus_km": 4.0, "dual_cycle_radius_km": 1.0}
    assert prof["solver_params"] == {"dual_cycle_bonus_km": 4.0, "dual_cycle_radius_km": 1.0}, (
        "the overridden param must be re-mirrored into the accounting solver_params (F1)")
    assert "use_spatial_matching" not in prof
    assert resolve_dispatch_for_profile(prof).candidate_kind == "sampled"
    # Validation happens before mutation.
    coll = _collection(legacy)
    with pytest.raises(PolicyParamsError):
        apply_policy_overrides(coll, {"dispatch": {"params": {"inner": {"params": {"dual_cycle_radius_km": -1}}}}})
    assert coll["assignment_0"]["profile"] == legacy


def test_stamp_shape():
    from apps.container_logistics.policies.dispatch.helpers.candidates import CandidateSettings

    stamp = policies_stamp(_greedy(), source="bundle",
                           candidate_settings=CandidateSettings.from_profile({}).to_dict(),
                           accounting_params={"dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5})
    assert stamp == {"dispatch": {
        "name": "GreedyNearest", "version": "1", "kind": "operator", "timing": "online",
        "label": "GreedyNearest", "leaf": "GreedyNearest", "information_model": "central",
        "share_tag_format": "audited",
        "params": {"candidates": "spatial", "dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5},
        "source": "bundle",
        "candidate_settings": {"cell_deg": 0.02, "per_order": 40, "max_rings": 8,
                               "max_trucks_per_scope": 500},
        "accounting_params": {"dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5}}}
    json.dumps(stamp)


def test_run_config_meta_mirrors_policies(capsys):
    """The sim main stamps orsim_settings["POLICIES"]; run_config.meta mirrors it.

    Verified to have teeth: mirroring ``None`` in ``get_run_config_meta`` failed the
    last assertion.
    """
    import apps.simulation.run_container_logistics_simulation as launcher
    from apps.container_logistics.scenario.scenario_manager import ScenarioManager

    mgr = ScenarioManager.__new__(ScenarioManager)
    coll = _collection({"strategy": "GreedyNearest", "max_trucks_per_haulier": 40})
    collections = {"assignment": coll, "truck": {}, "order": {}, "facility": {}, "analytics": {}}
    mgr.get_agent_collection = lambda role: collections[role]
    mgr._generation_spec = lambda: None
    mgr.scenario_name = "s"
    mgr.orsim_settings = {}
    launcher._stamp_effective_policies(mgr, overridden=False)
    stamp = mgr.orsim_settings["POLICIES"]
    assert stamp["dispatch"]["source"] == "legacy-profile"
    assert stamp["dispatch"]["candidate_settings"]["max_trucks_per_scope"] == 40
    # Round 2: a legacy profile's selection is Partitioned/GreedyNearest (C4 adds the label).
    assert "Policies effective: dispatch=Partitioned v1 timing=online params=" in capsys.readouterr().out
    assert mgr.get_run_config_meta()["policies"] == stamp


def _compiled_smoke_scenario(tmp_path, monkeypatch) -> str:
    """Compile ``collab_smoke_12t`` (GreedyNearest) into a tmp scenarios root; return its slug."""
    from pathlib import Path

    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    root = tmp_path / "root"
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    spec = json.loads((Path(__file__).resolve().parents[1] / "scenarios" / "collab_smoke_12t"
                       / "spec.json").read_text())
    folder = root / spec["slug"]
    folder.mkdir(parents=True)
    compile_spec_to_bundle(spec, domain="container-logistics-sim", scenario_dir=str(folder),
                           slug=spec["slug"])
    return spec["slug"]


def test_run_simulation_command_rejects_bad_policy_without_spawning(tmp_path, monkeypatch, capsys):
    """Verified to have teeth: removing the validation in command.py reached the
    (patched) spawn and the ``spawned`` assertion failed."""
    from openride_control import command
    from openride_control.manager import ServiceManager

    slug = _compiled_smoke_scenario(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr(ServiceManager, "run_simulation_background",
                        lambda self, **kw: spawned.append(kw))
    rc = command._run(["run-simulation", "--scenario", slug,
                       "--policy", "dispatch=GreedyNearest",
                       "--policy-param", "dispatch.dual_cycle_bonus_km=-1"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and not spawned
    assert payload["ok"] is False and payload["action"] == "run_simulation"
    assert "'dual_cycle_bonus_km' must be >= 0" in payload["message"]
    rc = command._run(["run-simulation", "--solver", "RandomAssignment", "--policy", "dispatch=GreedyNearest"])
    assert rc == 1 and not spawned
    assert "conflicts" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["message"]


def test_run_simulation_command_ships_validated_overrides(tmp_path, monkeypatch, capsys):
    from openride_control import command
    from openride_control.manager import ServiceManager
    from openride_control.models import ActionResult, ServiceState

    seen = {}

    def _fake(self, **kw):
        seen.update(kw)
        return ActionResult(key="simulation", ok=True, state=ServiceState.RUNNING, message="ok")

    monkeypatch.setattr(ServiceManager, "run_simulation_background", _fake)
    slug = _compiled_smoke_scenario(tmp_path, monkeypatch)
    rc = command._run(["run-simulation", "--scenario", slug,
                       "--policy-param", "dispatch.candidates=sampled", "--solver", "GreedyNearest"])
    assert rc == 0, capsys.readouterr().out
    assert seen["policy_overrides"] == {"dispatch": {"type": "GreedyNearest",
                                                     "params": {"candidates": "sampled"}}}


def test_validation_dry_runs_against_the_scenario_bundle_including_old_bundles(tmp_path, monkeypatch):
    """The dry run reads the scenario's compiled assignment profile; an old bundle
    (no ``policies``, legacy ``use_spatial_matching``) goes through the legacy
    translation exactly as at run start, and the on-disk bundle is never written.

    Verified to have teeth: dry-running against an empty collection instead of the
    bundle rejected the GreedyNearest-only param (the slot default is
    RandomAssignment), failing the first assertion.
    """
    from pathlib import Path

    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle
    from openride_control.policies import validate_run_policy_args

    root = tmp_path / "root"
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    spec = json.loads((Path(__file__).resolve().parents[1] / "scenarios" / "collab_smoke_12t"
                       / "spec.json").read_text())
    folder = root / spec["slug"]
    folder.mkdir(parents=True)
    compile_spec_to_bundle(spec, domain="container-logistics-sim", scenario_dir=str(folder),
                           slug=spec["slug"])
    ok = validate_run_policy_args(spec["slug"], None, [], ["dispatch.inner.dual_cycle_bonus_km=0"])
    assert ok == {"dispatch": {"params": {"inner": {"params": {"dual_cycle_bonus_km": 0}}}}}
    bad = validate_run_policy_args(spec["slug"], None, [], ["dispatch.inner.candidates=near"])
    assert isinstance(bad, str) and "must be one of ['sampled', 'spatial']" in bad

    bundle_path = folder / "scenario.json"
    bundle = json.loads(bundle_path.read_text())
    for behavior in bundle["agents"]["assignment"].values():
        behavior["profile"].pop("policies")
        behavior["profile"]["use_spatial_matching"] = False
    bundle_path.write_text(json.dumps(bundle))
    before = bundle_path.read_text()
    assert validate_run_policy_args(spec["slug"], "RandomAssignment", [], []) == {
        "dispatch": {"type": "RandomAssignment"}}
    assert bundle_path.read_text() == before, "the dry run must never write the bundle"


def test_cli_solver_is_sugar_for_policy_dispatch_and_edit_rebases():
    """``--solver X`` becomes ``policies.dispatch.type`` (never the legacy key); an edit
    keeps the current params unless the type changes.

    Verified to have teeth: keeping params across a type change in
    ``_rebase_policies`` carried Greedy's bonus onto RandomAssignment.
    """
    import argparse

    from openride import scenario_cmd as S

    args = argparse.Namespace(solver="GreedyNearest", policy=[],
                              policy_param=["dispatch.dual_cycle_bonus_km=0"])
    spec = S._spec_from_flags(args)
    assert "solver" not in spec
    assert spec["policies"] == {"dispatch": {"type": "GreedyNearest",
                                             "params": {"dual_cycle_bonus_km": 0}}}
    current = {"dispatch": {"type": "GreedyNearest", "timing": "online",
                            "params": {"dual_cycle_bonus_km": 2.0, "candidates": "sampled"}}}
    only_param = {"dispatch": {"params": {"dual_cycle_radius_km": 1}}}
    assert S._rebase_policies(only_param, current)["dispatch"] == {
        "type": "GreedyNearest", "timing": "online",
        "params": {"dual_cycle_bonus_km": 2.0, "candidates": "sampled", "dual_cycle_radius_km": 1}}
    assert S._rebase_policies({"dispatch": {"type": "RandomAssignment"}}, current)["dispatch"] == {
        "type": "RandomAssignment", "timing": "online", "params": {}}
    with pytest.raises(ValueError, match="needs a policy"):
        S._rebase_policies(only_param, None)


def test_openride_run_accepts_policy_flags():
    import argparse

    from openride import run_group

    parser = argparse.ArgumentParser()
    run_group.register(parser.add_subparsers())
    args = parser.parse_args(["run", "s", "--policy", "dispatch=GreedyNearest",
                              "--policy-param", "dispatch.dual_cycle_bonus_km=0"])
    assert args.policy == ["dispatch=GreedyNearest"]
    assert args.policy_param == ["dispatch.dual_cycle_bonus_km=0"]
