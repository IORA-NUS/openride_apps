"""Review fix round (F2 backend, F4, F7, F11-F15): one resolver, one rule, loud errors.

What is locked (plan ``docs/plans/policy_slots_p0_p3.md``, "Fix round" deviations):
- F4: the agent, the run-start check and the run stamp resolve a profile through ONE
  function, which refuses unknown entry keys (``parms``) and unknown slots in a
  compiled ``profile.policies`` block; ``version``/``kind`` are ignored on purpose.
- F11: only an unknown policy name gets the stale-worker hint.
- F12: run-flag validation against a missing scenario says so, instead of validating
  against slot defaults (which named the wrong policy).
- F13/F14: executable timings and the no-``candidates`` kind have one source each.
- F15: the editForm fallback logs.
- F7: the run-name label of a params override.
- F2: ``run-simulation --policy-overrides`` and the synchronous
  ``validate-run-policies`` control command.

Why: every one of these was a second copy of a rule or a silent path that already
disagreed with its twin (independent review, 2026-10-21).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from apps.container_logistics.policies.compat.legacy_profile import legacy_dispatch_from_profile
from apps.container_logistics.policies.dispatch import profile as P
from apps.container_logistics.policies.framework import (
    SLOTS,
    PolicyError,
    PolicyParamsError,
    UnknownPolicyError,
    UnknownSlotError,
)
from apps.container_logistics.scenario import scenario_overrides as SO

REPO_ROOT = Path(__file__).resolve().parents[1]


def _compiled_block(**entry):
    base = {"type": "GreedyNearest", "version": "1", "kind": "operator", "timing": "online",
            "params": {"dual_cycle_bonus_km": 5.0}}
    base.update(entry)
    # policies_format 2: the compile-time marker every round-2 bundle carries (C4).
    return {"strategy": "GreedyNearest", "policies": {"planner": base}, "policies_format": 2}


# --- F4 -----------------------------------------------------------------------------

def test_runtime_resolver_refuses_unknown_entry_keys_and_slots():
    """Verified to have teeth: stripping unknown keys in ``_authored_from_compiled``
    (returning ``{k: v for k in ENTRY_KEYS}`` without the check) resolved ``parms`` to
    the defaults; skipping the slot check in ``resolve_policies_block`` resolved
    ``dispach``."""
    typo = _compiled_block()
    typo["policies"]["planner"]["parms"] = {"dual_cycle_bonus_km": 0}
    for fn in (P.resolve_dispatch_for_profile, SO.current_policies_for_profile):
        with pytest.raises(PolicyError, match="unknown key\\(s\\) 'parms'"):
            fn(typo)
    with pytest.raises(PolicyError, match="assignment agent 'a0'.*'parms'"):
        P.assert_assignment_profiles_resolvable({"a0": {"profile": typo}})

    slot_typo = _compiled_block()
    slot_typo["policies"]["dispach"] = {"type": "GreedyNearest"}
    for fn in (P.resolve_dispatch_for_profile, SO.current_policies_for_profile):
        with pytest.raises(UnknownSlotError, match="dispach"):
            fn(slot_typo)


def test_stamp_path_and_agent_share_one_resolver():
    """``current_policies_for_profile`` IS ``resolve_profile_policies`` (no second copy)."""
    prof = _compiled_block()
    resolved, source = SO.current_policies_for_profile(prof)
    assert (resolved, source) == P.resolve_profile_policies(prof)
    assert source == "bundle" and P.resolve_dispatch_for_profile(prof).source == "policies"
    legacy, legacy_source = SO.current_policies_for_profile({"strategy": "GreedyNearest"})
    # Round 2: a legacy profile resolves to the tree Partitioned/<strategy>.
    assert legacy_source == "legacy-profile" and legacy["planner"].label == "Partitioned/GreedyNearest"


def test_compiled_version_and_kind_are_ignored_not_trusted():
    """A stale ``version``/``kind`` echo neither fails nor leaks: the registry's values win.
    Verified to have teeth: passing ``version`` through to the resolver (keeping it in
    ``_authored_from_compiled``) raised "unknown key 'version'"."""
    prof = _compiled_block(version="999", kind="bogus")
    resolved, _ = P.resolve_profile_policies(prof)
    assert (resolved["planner"].version, resolved["planner"].kind) == ("1", "operator")


def test_non_executable_timing_is_refused_by_the_shared_resolver(monkeypatch):
    """WS4b (G13): ``plan`` is no planner timing any more; the generic "declared but not
    executable" refusal still holds for a slot table that declares one."""
    from apps.container_logistics.policies.framework import resolve as R
    from apps.container_logistics.policies.framework.slots import SlotSpec

    with pytest.raises(PolicyError, match=r"timing='plan' is not a planner timing\. Allowed: online\."):
        P.resolve_dispatch_for_profile(_compiled_block(timing="plan"))
    held = SlotSpec(name="planner", kind="operator", default=R.SLOTS["planner"].default, description="d",
                    timings=("online", "held"), executable_timings=("online",))
    monkeypatch.setattr(R, "SLOTS", {"planner": held})
    with pytest.raises(PolicyError, match="'held' is defined but not executable yet: it lands in a later phase"):
        P.resolve_dispatch_for_profile(_compiled_block(timing="held"))


def test_non_mapping_params_are_a_params_error():
    with pytest.raises(PolicyParamsError, match="params must be an object"):
        P.resolve_dispatch_for_profile(_compiled_block(params=[1]))


def test_legacy_drop_rule_is_implemented_once(caplog):
    """Verified to have teeth: re-inlining a private filter in
    ``legacy_dispatch_from_profile`` (no log) failed the WARNING assertion."""
    from apps.container_logistics.policies.framework import resolve as R

    with caplog.at_level(logging.WARNING, logger=R.__name__):
        entry = legacy_dispatch_from_profile(
            {"strategy": "RandomAssignment", "solver_params": {"dual_cycle_bonus_km": 5.0}})
    assert entry["params"] == {}
    assert [r.levelno for r in caplog.records if "not declared by planner RandomAssignment" in r.getMessage()] \
        == [logging.WARNING]


# --- F13 / F14 ----------------------------------------------------------------------

def test_executable_timings_and_kind_rule_have_one_source():
    """The timings come from the slot table; the candidate kind is the SOLVER's own, never the framework's.

    WS1: the framework's ``candidate_kind`` (``resolved.candidate_kind_for``) is gone; a
    leaf declares a ``candidate_hint`` from its own ``candidates`` param at bind, and the
    helper base gives a param-less policy none. Verified to have teeth: making
    ``PairLeafMixin.bind`` hint ``"spatial"`` unconditionally failed the RandomAssignment
    assertion.
    """
    from apps.container_logistics.policies.dispatch import resolved as RS
    from apps.container_logistics.policies.dispatch import runner
    from apps.container_logistics.policies.dispatch.base import DispatchPolicy
    from apps.container_logistics.policies.dispatch.bind import RunWorld

    assert runner.EXECUTABLE_TIMINGS is SLOTS["planner"].executable_timings
    assert not hasattr(P, "_KIND_WITHOUT_CANDIDATES_PARAM")
    assert not hasattr(RS, "candidate_kind_for") and not hasattr(RS, "KIND_WITHOUT_CANDIDATES_PARAM")
    assert runner.build_runner_for_policy("RandomAssignment").tree.declaration.candidate_hint is None
    assert runner.build_runner_for_policy("GreedyNearest").tree.declaration.candidate_hint == "spatial"
    assert runner.build_runner_for_policy(
        "RandomAssignment", {"candidates": "spatial"}).tree.declaration.candidate_hint == "spatial"

    class _NoCandidates:
        pass

    assert DispatchPolicy().bind(_NoCandidates(), RunWorld({}), top=True).candidate_hint is None


# --- F11 ----------------------------------------------------------------------------

def _build_app(monkeypatch, profile):
    from orsim.lifecycle import ORSimApp

    from apps.container_logistics.assignment.app import AssignmentApp

    def _stub(self, run_id, sim_clock, behavior, messenger=None, **kwargs):
        self.behavior = behavior

    monkeypatch.setattr(ORSimApp, "__init__", _stub)
    return AssignmentApp(run_id="r", sim_clock="c", behavior={"profile": profile}, messenger=None)


def test_stale_worker_hint_only_for_an_unknown_policy(monkeypatch):
    """Verified to have teeth: catching ``PolicyError`` (the old clause) in
    ``AssignmentApp.__init__`` appended the hint to the param error."""
    with pytest.raises(UnknownPolicyError, match="restart the celery workers"):
        _build_app(monkeypatch, {"strategy": "NoSuchPolicy"})
    with pytest.raises(PolicyParamsError) as exc:
        _build_app(monkeypatch, {"strategy": "GreedyNearest",
                                 "solver_params": {"dual_cycle_bonus_km": -1.0}})
    assert "restart" not in str(exc.value), str(exc.value)


# --- F15 ----------------------------------------------------------------------------

def test_editform_fallback_logs_slug_and_error(caplog):
    """Verified to have teeth: deleting the ``logger.warning`` in
    ``policy_slots_from_spec`` failed the log assertion."""
    from apps.container_logistics.scenario.frontend_scenario_spec import policy_slots_from_spec

    gs = {"slug": "broken_slug", "policies": {"planner": {"type": "NoSuchPolicy"}}}
    with caplog.at_level(logging.WARNING):
        form = policy_slots_from_spec(gs)
    assert form == {"planner": {"type": "NoSuchPolicy"}}, "display behaviour is unchanged"
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(m.startswith("POLICY SLOTS EDITFORM FALLBACK") and "broken_slug" in m
               and "NoSuchPolicy" in m for m in msgs), msgs


# --- F7 -----------------------------------------------------------------------------

def test_run_name_label_for_type_and_params_overrides():
    """Verified to have teeth: dropping ``sort_keys``-style key sorting (iterating the
    override's own order) failed the two-key case; returning the bare name for a
    params override failed the first assertion."""
    from apps.container_logistics.policies.framework import merge_policy_overrides, resolve_policies_block
    from apps.simulation.run_container_logistics_simulation import dispatch_override_label

    base = resolve_policies_block({"planner": {"type": "GreedyNearest"}})

    def label(overrides):
        return dispatch_override_label(overrides, merge_policy_overrides(base, overrides))

    assert label({"planner": {"params": {"dual_cycle_bonus_km": 0}}}) == \
        "GreedyNearest(dual_cycle_bonus_km=0.0)"
    assert label({"planner": {"params": {"dual_cycle_radius_km": 1, "candidates": "sampled"}}}) == \
        'GreedyNearest(candidates="sampled",dual_cycle_radius_km=1.0)'
    assert label({"planner": {"type": "RandomAssignment"}}) == "RandomAssignment"
    assert label({"planner": {"type": "RandomAssignment", "params": {"candidates": "spatial"}}}) == \
        'RandomAssignment(candidates="spatial")'
    assert label({"planner": {"timing": "online"}}) is None


# --- F12 / F2 -----------------------------------------------------------------------

@pytest.fixture()
def smoke_slug(tmp_path, monkeypatch):
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    root = tmp_path / "root"
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    spec = json.loads((REPO_ROOT / "scenarios" / "collab_smoke_12t" / "spec.json").read_text())
    (root / spec["slug"]).mkdir(parents=True)
    compile_spec_to_bundle(spec, domain="container-logistics-sim",
                           scenario_dir=str(root / spec["slug"]), slug=spec["slug"])
    (root / "empty_folder").mkdir()
    return spec["slug"]


def test_validation_against_a_missing_scenario_says_so(smoke_slug):
    """Verified to have teeth: falling back to ``{}`` (the old slot-default dry run)
    produced "params for RandomAssignment: unknown key" instead."""
    from openride_control.policies import validate_run_policy_args

    flags = ["planner.inner.dual_cycle_bonus_km=3"]  # round 2: the solver is the inner
    msg = validate_run_policy_args("does_not_exist_xyz", None, [], flags)
    assert msg.startswith("scenario 'does_not_exist_xyz' not found"), msg
    msg = validate_run_policy_args("empty_folder", None, [], flags)
    assert "has no compiled bundle (scenario.json)" in msg and "openride scenario compile empty_folder" in msg
    assert validate_run_policy_args("does_not_exist_xyz", None, [], []) == {}, "no flags: nothing to check"
    assert validate_run_policy_args(smoke_slug, None, [], flags) == {
        "planner": {"params": {"inner": {"params": {"dual_cycle_bonus_km": 3}}}}}


def test_policy_overrides_json_merges_with_flags_and_conflicts_are_refused():
    """Verified to have teeth: letting the CLI value overwrite a differing JSON value in
    ``_set_once`` failed the conflict assertions."""
    from openride_control.policies import build_run_policy_overrides

    js = json.dumps({"planner": {"type": "GreedyNearest", "timing": "online",
                                  "params": {"dual_cycle_bonus_km": 0}}})
    # Round 2 (C4): --solver is not folded in (it ships as ORSIM_SOLVER; its conflicts
    # are run_dispatch_overrides' message 12, tests/test_policy_run_overrides.py).
    merged = build_run_policy_overrides([], ["planner.dual_cycle_radius_km=1"], js)
    assert merged == {"planner": {"type": "GreedyNearest", "timing": "online",
                                   "params": {"dual_cycle_bonus_km": 0, "dual_cycle_radius_km": 1}}}
    for policies, params, needle in [
        (["planner=RandomAssignment"], [], "--policy sets planner.type"),
        ([], ["planner.dual_cycle_bonus_km=2"], "--policy-param sets planner.params.dual_cycle_bonus_km"),
        ([], ["planner.dual_cycle_bonus_km=false"], "--policy-param sets"),
    ]:
        with pytest.raises(PolicyError, match=needle):
            build_run_policy_overrides(policies, params, js)
    for bad in ("{not json", "[1]", '{"planner": 3}'):
        with pytest.raises(PolicyError, match="--policy-overrides"):
            build_run_policy_overrides([], [], bad)


def _run_command(argv, capsys):
    from openride_control import command

    rc = command._run(argv)
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_run_simulation_policy_overrides_validated_before_spawning(smoke_slug, monkeypatch, capsys):
    """Verified to have teeth: not forwarding ``args.policy_overrides`` to
    ``validate_run_policy_args`` in command.py spawned the bad run."""
    from openride_control.manager import ServiceManager
    from openride_control.models import ActionResult, ServiceState

    spawned = []

    def _fake(self, **kw):
        spawned.append(kw)
        return ActionResult(key="simulation", ok=True, state=ServiceState.RUNNING, message="ok")

    monkeypatch.setattr(ServiceManager, "run_simulation_background", _fake)
    for bad, needle in [
        ({"planner": {"parms": {"dual_cycle_bonus_km": 0}}}, "unknown key(s) 'parms'"),
        ({"planner": {"params": {"inner": {"params": {"dual_cycle_bonus_km": -1}}}}}, "must be >= 0"),
        ({"dispach": {"type": "GreedyNearest"}}, "Unknown policy slot 'dispach'"),
    ]:
        rc, payload = _run_command(["run-simulation", "--scenario", smoke_slug,
                                    "--policy-overrides", json.dumps(bad)], capsys)
        assert rc == 1 and not spawned, payload
        assert payload["ok"] is False and needle in payload["message"], payload
    good = {"planner": {"params": {"inner": {"params": {"dual_cycle_bonus_km": 0}}}}}
    rc, payload = _run_command(["run-simulation", "--scenario", smoke_slug,
                                "--policy-overrides", json.dumps(good)], capsys)
    assert rc == 0 and spawned[-1]["policy_overrides"] == good


def test_validate_run_policies_command(smoke_slug, monkeypatch, capsys):
    """Verified to have teeth: emitting ``ok: true`` without calling the validator
    failed the error case; it never spawns anything."""
    from openride_control.manager import ServiceManager

    monkeypatch.setattr(ServiceManager, "run_simulation_background",
                        lambda self, **kw: pytest.fail("validate-run-policies must not spawn"))
    rc, payload = _run_command(["validate-run-policies", "--scenario", smoke_slug,
                                "--solver", "RandomAssignment",
                                "--policy-overrides", json.dumps(
                                    {"planner": {"params": {"inner": {"params": {"candidates": "spatial"}}}}})],
                               capsys)
    assert rc == 0 and payload["ok"] is True and payload["action"] == "validate_run_policies", payload
    # Round 2 (C4): the payload's overrides are ORSIM_POLICY_OVERRIDES; --solver is not folded in.
    assert payload["overrides"] == {"planner": {"params": {"inner": {"params": {"candidates": "spatial"}}}}}
    rc, payload = _run_command(["validate-run-policies", "--scenario", smoke_slug,
                                "--policy-param", "planner.inner.candidates=near"], capsys)
    assert rc == 1 and payload == {**payload, "ok": False, "action": "validate_run_policies"}
    assert "must be one of ['sampled', 'spatial']" in payload["message"]
    rc, payload = _run_command(["validate-run-policies", "--scenario", "nope_xyz",
                                "--solver", "GreedyNearest"], capsys)
    assert rc == 1 and "scenario 'nope_xyz' not found" in payload["message"]
    rc, payload = _run_command(["validate-run-policies", "--scenario", smoke_slug], capsys)
    assert rc == 0 and payload["overrides"] == {}
