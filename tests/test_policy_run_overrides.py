"""Per-run policy overrides: CLI parsing, merge rules, profile patching, the run stamp.

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.2, P2.5, P2.6):
- ``--policy-param`` values are JSON when they parse, else the raw string; slot
  names may contain dots.
- A changed type resets params to the new policy's defaults; the same type merges.
  Every merge is re-validated (no silent acceptance of a bad override).
- ``ORSIM_SOLVER`` / ``--solver`` still work and set the INNERMOST per-scope solver
  (round-2 §3.3; a decomposition name is message 11); disagreeing with a type that
  ``ORSIM_POLICY_OVERRIDES`` names at that place is refused (message 12).
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
    current_policies_for_profile,
    run_dispatch_overrides,
)
from tests.policy_registry_names import leaf_names


def _collection(profile):
    return {"assignment_0": {"profile": deepcopy(profile)}}


def _greedy(**params):
    return resolve_policies_block({"dispatch": {"type": "GreedyNearest", "params": params}})


def test_parse_policy_cli_args_dotted_slot_name(monkeypatch):
    """A slot name may contain dots: the slot is the LONGEST known prefix.

    No dotted slot exists today, so a table with ``world`` and ``world.x`` stands in;
    matching the shortest prefix would read ``world.x.k=1`` as slot ``world``."""
    from types import MappingProxyType

    from apps.container_logistics.policies.framework import resolve as resolve_mod
    monkeypatch.setattr(resolve_mod, "SLOTS", MappingProxyType({"dispatch": None, "world": None, "world.x": None}))
    out = parse_policy_cli_args([], ["world.x.k=1", "world.y=2"])
    assert out == {"world.x": {"params": {"k": 1}}, "world": {"params": {"y": 2}}}


def test_parse_policy_cli_args_json_coercion():
    """Round 2 (§3.3): the slot is the longest known prefix; every further non-final
    segment is a policy reference.

    Verified to have teeth: treating every segment after the slot as one dotted key
    (no nesting) failed the ``offer``/``inner`` assertions. Dotted slot names are
    locked by :func:`test_parse_policy_cli_args_dotted_slot_name`."""
    out = parse_policy_cli_args(
        ["dispatch=GreedyNearest"],
        ["dispatch.dual_cycle_bonus_km=0", "dispatch.candidates=sampled", "dispatch.flag=true",
         "dispatch.quoted=\"60\"", "dispatch.offer.keep_below_km=1.5", "dispatch.eq=a=b",
         "dispatch.inner=RandomAssignment", "dispatch.inner.candidates=spatial"],
    )
    assert out["dispatch"]["type"] == "GreedyNearest"
    p = out["dispatch"]["params"]
    assert p["dual_cycle_bonus_km"] == 0 and isinstance(p["dual_cycle_bonus_km"], int)
    assert p["candidates"] == "sampled" and p["flag"] is True and p["quoted"] == "60"
    assert p["eq"] == "a=b"
    assert p["offer"] == {"params": {"keep_below_km": 1.5}}
    assert p["inner"] == {"type": "RandomAssignment", "params": {"candidates": "spatial"}}
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


_ENV = ("ORSIM_POLICY_OVERRIDES", "ORSIM_SOLVER", "ORSIM_PLANNER_TOPOLOGY", "ORSIM_OFFER_POLICY",
        "ORSIM_CLAIM_POLICY", "ORSIM_ARBITRATION_RULE", "ORSIM_MARKET_MAX_ROUNDS")


def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_orsim_solver_still_works(monkeypatch):
    import apps.simulation.run_container_logistics_simulation as launcher

    _clean_env(monkeypatch)
    monkeypatch.setenv("ORSIM_SOLVER", "GreedyNearest")
    coll = _collection({"strategy": "RandomAssignment"})
    merged, labels = launcher._apply_dispatch_overrides_or_exit(coll)
    assert labels == {"dispatch": "GreedyNearest", "topology": None} and merged
    assert coll["assignment_0"]["profile"]["strategy"] == "GreedyNearest"
    assert apply_solver_override(_collection({}), "RandomAssignment") == "RandomAssignment"
    # A params-only override labels the policy with exactly the overridden params
    # (review F7; format in ``dispatch_override_label``).
    monkeypatch.delenv("ORSIM_SOLVER")
    monkeypatch.setenv("ORSIM_POLICY_OVERRIDES",  # round 2: the solver's params are the inner's
                       json.dumps({"dispatch": {"params": {"inner": {"params": {"dual_cycle_bonus_km": 0}}}}}))
    coll = _collection({"strategy": "GreedyNearest"})
    assert launcher._apply_dispatch_overrides_or_exit(coll)[1]["dispatch"] == \
        "GreedyNearest(dual_cycle_bonus_km=0.0)"
    monkeypatch.delenv("ORSIM_POLICY_OVERRIDES")
    untouched = _collection({})
    assert launcher._apply_dispatch_overrides_or_exit(untouched) == ({}, {"dispatch": None, "topology": None})
    assert untouched == _collection({}), "nothing requested mutates nothing"


def test_orsim_solver_conflicting_with_policy_override_raises(monkeypatch, capsys):
    """Message 12, at the place ``--solver`` sets: the inner of a decomposition, the top
    of a central selection.

    Verified to have teeth: removing the conflict check in
    ``scenario_overrides._set_innermost`` (ORSIM_SOLVER silently wins) failed both
    ``pytest.raises``."""
    import apps.simulation.run_container_logistics_simulation as launcher

    _clean_env(monkeypatch)
    monkeypatch.setenv("ORSIM_SOLVER", "RandomAssignment")
    monkeypatch.setenv("ORSIM_POLICY_OVERRIDES",
                       json.dumps({"dispatch": {"params": {"inner": {"type": "GreedyNearest"}}}}))
    with pytest.raises(SystemExit) as stop:
        launcher._apply_dispatch_overrides_or_exit(_collection({}))
    assert stop.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("Policy configuration invalid; refusing to start the run:")
    assert ("--solver 'RandomAssignment' conflicts with dispatch.inner.type='GreedyNearest' from "
            "--policy/--policy-param/--policy-overrides; --solver is shorthand for the innermost solver, "
            "pass one of them.") in out
    with pytest.raises(PolicyError) as exc:  # a central top: --solver would replace it
        run_dispatch_overrides(_collection({}), overrides={"dispatch": {"type": "GreedyNearest"}},
                               solver="RandomAssignment")
    assert str(exc.value).startswith("--solver 'RandomAssignment' conflicts with dispatch.type='GreedyNearest'")
    # Agreeing is fine, and a decomposition top keeps its own params.
    merged, labels = run_dispatch_overrides(
        _collection({}), overrides={"dispatch": {"params": {"inner": {"type": "GreedyNearest"}}}},
        solver="GreedyNearest")
    assert labels["dispatch"] == "GreedyNearest"


def test_solver_is_innermost_leaf():
    """``--solver X`` sets the innermost per-scope solver of whatever is selected
    (Partitioned's or PooledMarket's ``inner``), replaces a central leaf top, resets
    params only when the type changes, and keeps the decomposition's own params.

    Verified to have teeth: making ``_set_innermost`` always write the top type turned
    the Partitioned case into a central RandomAssignment. (The "same type keeps its
    params" assertion is held by two independent mechanisms, ``_set_innermost``'s early
    return and ``merge_params``' same-type merge; removing either alone survives.)"""
    def label(profile, **kw):
        coll = _collection(profile)
        run_dispatch_overrides(coll, **kw)
        return current_policies_for_profile(coll["assignment_0"]["profile"])[0]["dispatch"]

    assert label({"strategy": "GreedyNearest"}, solver="RandomAssignment").label == \
        "Partitioned/RandomAssignment"
    pooled = label({"strategy": "GreedyNearest", "planner": {"topology": "pooled", "market": {"max_rounds": 7}}},
                   solver="RandomAssignment")
    assert pooled.label == "PooledMarket/RandomAssignment" and pooled.params["max_rounds"] == 7
    central = label({}, overrides={"dispatch": {"type": "GreedyNearest"}}, solver=None)
    assert central.label == "GreedyNearest", "a leaf --policy is a CENTRAL arm (round 2)"
    same = label({"strategy": "GreedyNearest", "solver_params": {"dual_cycle_bonus_km": 2.0}},
                 solver="GreedyNearest")
    assert same.params["inner"]["params"]["dual_cycle_bonus_km"] == 2.0, "the same type keeps its params"
    swapped = label({"strategy": "GreedyNearest", "solver_params": {"dual_cycle_bonus_km": 2.0}},
                    overrides={"dispatch": {"type": "PooledMarket"}}, solver="RandomAssignment")
    assert swapped.label == "PooledMarket/RandomAssignment"


def test_solver_naming_a_composite_is_rejected():
    """Message 11. Verified to have teeth: dropping the ``is_composite`` check in
    ``_checked_solver`` built ``Partitioned/Partitioned``-shaped garbage that the
    resolver refused with message 2 instead."""
    coll = _collection({"strategy": "GreedyNearest"})
    for name in ("PooledMarket", "Partitioned"):
        with pytest.raises(PolicyError) as exc:
            run_dispatch_overrides(coll, solver=name)
        assert str(exc.value) == (
            f"--solver {name!r} names a decomposition, not a per-scope solver. --solver sets the inner "
            f"solver ({leaf_names()}); select a decomposition with --policy dispatch={name}.")
    assert coll == _collection({"strategy": "GreedyNearest"}), "nothing mutated"
    with pytest.raises(UnknownPolicyError, match="^--solver 'Nope': Unknown dispatch policy 'Nope'"):
        run_dispatch_overrides(coll, solver="Nope")


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
    # Round 2 (C4): the line prints the selection label and its information model.
    assert ("Policies effective: dispatch=Partitioned/GreedyNearest v1 timing=online "
            "information_model=per_group params=") in capsys.readouterr().out
    assert stamp["dispatch"]["candidate_settings"]["sample_scope"] == "per_group"
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
    rc = command._run(["run-simulation", "--scenario", slug,
                       "--solver", "RandomAssignment", "--policy", "dispatch=GreedyNearest"])
    assert rc == 1 and not spawned
    assert "conflicts with dispatch.type='GreedyNearest'" in json.loads(
        capsys.readouterr().out.strip().splitlines()[-1])["message"]


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
                       "--policy-param", "dispatch.inner.candidates=sampled", "--solver", "RandomAssignment"])
    assert rc == 0, capsys.readouterr().out
    # Round 2 (C4): --solver travels separately (ORSIM_SOLVER), never folded into the overrides.
    assert seen["policy_overrides"] == {"dispatch": {"params": {"inner": {"params": {"candidates": "sampled"}}}}}
    assert seen["solver"] == "RandomAssignment"


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
    # --solver alone ships no overrides (it is ORSIM_SOLVER) but is still dry-run.
    assert validate_run_policy_args(spec["slug"], "RandomAssignment", [], []) == {}
    assert bundle_path.read_text() == before, "the dry run must never write the bundle"


def test_cli_solver_writes_the_innermost_solver_never_a_flat_leaf():
    """``scenario new --solver X`` writes ``Partitioned{inner: X}`` (a flat ``{type: X}``
    now means a CENTRAL X); ``edit --solver X`` sets the innermost solver of the current
    selection; ``--policy-param`` alone rebases nested over the current block.

    Verified to have teeth: restoring the round-1 ``dispatch["type"] = solver`` in
    ``_policies_from_flags`` failed the first assertion; resetting the carried inner on a
    top-type change in ``_rebase_policies`` failed the PooledMarket assertion.
    """
    import argparse

    from openride import scenario_cmd as S

    def flags(solver=None, policy=(), param=()):
        return argparse.Namespace(solver=solver, policy=list(policy), policy_param=list(param))

    spec = S._spec_from_flags(flags("GreedyNearest", param=["dispatch.inner.dual_cycle_bonus_km=0"]))
    assert "solver" not in spec
    assert spec["policies"] == {"dispatch": {"type": "Partitioned", "params": {
        "inner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}}}
    current = {"dispatch": {"type": "PooledMarket", "timing": "online", "params": {
        "max_rounds": 5, "inner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 2.0}}}}}
    edited = S._policies_from_flags(flags("RandomAssignment"), current)
    assert edited["dispatch"] == {"type": "PooledMarket", "timing": "online", "params": {
        "max_rounds": 5, "inner": {"type": "RandomAssignment"}}}, "edit sets the innermost solver"
    kept = S._policies_from_flags(flags("GreedyNearest"), current)
    assert kept == current, "the same solver keeps its params"
    only_param = S._policies_from_flags(flags(param=["dispatch.inner.dual_cycle_radius_km=1"]), current)
    assert only_param["dispatch"]["params"]["inner"] == {
        "type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 2.0, "dual_cycle_radius_km": 1}}
    to_part = S._policies_from_flags(flags(policy=["dispatch=Partitioned"]), current)
    assert to_part["dispatch"]["params"] == {"inner": current["dispatch"]["params"]["inner"]}, (
        "changing the decomposition keeps the per-scope solver")
    central = S._policies_from_flags(flags("RandomAssignment"), {"dispatch": {"type": "GreedyNearest"}})
    assert central == {"dispatch": {"type": "RandomAssignment"}}, "a central leaf top is replaced"
    with pytest.raises(ValueError, match="names a decomposition"):
        S._policies_from_flags(flags("PooledMarket"), None)
    with pytest.raises(ValueError, match=r"conflicts with dispatch\.inner\.type='GreedyNearest'"):
        S._policies_from_flags(flags("RandomAssignment", param=["dispatch.inner=GreedyNearest"]), None)
    with pytest.raises(ValueError, match="needs a policy"):
        S._rebase_policies({"dispatch": {"params": {"k": 1}}}, None)


def test_openride_run_accepts_policy_flags():
    import argparse

    from openride import run_group

    parser = argparse.ArgumentParser()
    run_group.register(parser.add_subparsers())
    args = parser.parse_args(["run", "s", "--policy", "dispatch=GreedyNearest",
                              "--policy-param", "dispatch.dual_cycle_bonus_km=0"])
    assert args.policy == ["dispatch=GreedyNearest"]
    assert args.policy_param == ["dispatch.dual_cycle_bonus_km=0"]
    solver_help = next(a.help for a in parser._subparsers._group_actions[0].choices["run"]._actions
                       if "--solver" in a.option_strings)
    assert "innermost per-scope solver" in solver_help.lower(), solver_help


def test_stamp_candidate_settings_names_the_sample_scope():
    """``candidate_settings.sample_scope`` says where ``max_trucks_per_scope`` applies:
    per haulier on a central leaf's tick scope, per cooperation group under Partitioned,
    per company under PooledMarket (C3 deviation 6; a central arm can see MORE
    candidates than the same leaf per group on a cooperating structure).

    Verified to have teeth: stamping a constant ``"per_scope"`` in
    ``_stamp_effective_policies`` failed every case; mapping by the top NAME instead of
    its information model is invisible here (equivalent for the built-ins)."""
    import types

    import apps.simulation.run_container_logistics_simulation as launcher

    for overrides, want in (({}, "per_group"), ({"dispatch": {"type": "PooledMarket"}}, "per_company"),
                            ({"dispatch": {"type": "GreedyNearest"}}, "per_haulier")):
        coll = _collection({"strategy": "GreedyNearest"})
        run_dispatch_overrides(coll, overrides=overrides, solver="GreedyNearest" if not overrides else None)
        sm = types.SimpleNamespace(orsim_settings={}, get_agent_collection=lambda role, c=coll: c)
        launcher._stamp_effective_policies(sm, True)
        assert sm.orsim_settings["POLICIES"]["dispatch"]["candidate_settings"]["sample_scope"] == want, overrides
