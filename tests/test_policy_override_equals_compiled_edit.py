"""A per-run policy override runs the SAME experiment as that edit compiled into the spec.

What is locked (review F1, plan ``docs/plans/policy_slots_p0_p3.md`` deviation 19 and
the fix-round deviations):
- ``apply_policy_overrides`` re-mirrors the resolved dispatch params into the
  accounting copies (``solver_params``, ``planner.deployment.params``) exactly as
  compile does, through the ONE helper ``dispatch/profile.py::mirror_dispatch_params``.
  So pairs, share tags (``benefit_km``), published strings and pooled round stats of
  a per-run override equal those of the compiled edit, on both topologies.
- The run stamp records the accounting params (``dispatch.accounting_params``).
- A dispatch TYPE change resets the policy params to the new policy's defaults and the
  mirror rewrites the shared keys when the new policy declares them; a policy that
  does not declare them (Random) leaves the bundle's accounting values alone.
- Compile refuses a ``planner.deployment`` that contradicts an authored
  ``policies.dispatch`` (type, or a DECLARED param's value), tolerates agreeing
  values and undeclared accounting-only keys, and a compiled recipe recompiles.

Why: a per-run arm whose ranking and accounting disagree is a hybrid run nobody
configured, and an analyst comparing it with a spec-edited arm compares two
different experiments without knowing it (CLAUDE.md §5 doc 21 §2a).
"""

from __future__ import annotations

import copy
import gzip
import json
import tempfile
from pathlib import Path

import pytest

from apps.container_logistics.datagen.preprocess import SpecValidationError
from apps.container_logistics.scenario.scenario_overrides import (
    apply_policy_overrides,
    current_policies_for_profile,
)
from tests.golden.harness import run_case

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "container-logistics-sim"
WORLD = "collab_scale_500t_v2"  # tick states A and B of the golden fixture come from it
BONUS_0 = {"dispatch": {"params": {"dual_cycle_bonus_km": 0.0}}}


def _spec(slug: str = WORLD) -> dict:
    return json.loads((REPO_ROOT / "scenarios" / slug / "spec.json").read_text())


def _compile(spec: dict, root: Path) -> dict:
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    folder = tempfile.mkdtemp(dir=root)
    return compile_spec_to_bundle(copy.deepcopy(spec), domain=DOMAIN, scenario_dir=folder,
                                  slug=spec["slug"])


def _profile(bundle: dict) -> dict:
    return copy.deepcopy(next(iter(bundle["agents"]["assignment"].values()))["profile"])


def _with_policies(spec: dict, policies: dict) -> dict:
    """``spec`` edited the way the editor saves it: ``policies`` set, legacy dials dropped."""
    edited = copy.deepcopy(spec)
    edited["policies"] = copy.deepcopy(policies)
    edited["solver"] = None
    edited.get("planner", {}).pop("deployment", None)
    return edited


@pytest.fixture(scope="module")
def tick_states():
    raw = gzip.open(REPO_ROOT / "tests" / "golden" / "dispatch_golden_v1.json.gz").read()
    return json.loads(raw)["tick_states"]


@pytest.fixture(scope="module")
def profiles(tmp_path_factory):
    """``{"base", "compiled_edit"}``: the world compiled unedited and with bonus 0 in spec."""
    root = tmp_path_factory.mktemp("scen")
    mp = pytest.MonkeyPatch()
    mp.setenv("ORSIM_SCENARIOS_DIR", str(root))
    try:
        spec = _spec()
        edit = _with_policies(spec, {"dispatch": {"type": "GreedyNearest",
                                                  "params": {"dual_cycle_bonus_km": 0.0}}})
        return {"base": _profile(_compile(spec, root)),
                "compiled_edit": _profile(_compile(edit, root))}
    finally:
        mp.undo()


def _overridden(profile: dict, overrides: dict) -> dict:
    coll = {"assignment_0": {"profile": copy.deepcopy(profile)}}
    apply_policy_overrides(coll, overrides)
    return coll["assignment_0"]["profile"]


def _case(profile: dict, topology: str, state: str = "A") -> dict:
    prof = copy.deepcopy(profile)
    prof["planner"]["topology"] = topology
    prof["cooperation"]["active"] = "chain"  # share tags carry benefit_km
    return json.loads(json.dumps({"profile": prof, "strategy": prof["strategy"], "tick_state": state},
                                 sort_keys=True))


def _benefits(out: dict) -> list:
    return sorted(round(tag["benefit_km"], 6) for _key, tag in out["share_tags"]
                  if tag.get("benefit_km") is not None)


@pytest.mark.parametrize("topology", ["partitioned", "pooled"])
def test_per_run_override_equals_the_compiled_edit(profiles, tick_states, topology):
    """Verified to have teeth: removing the ``mirror_dispatch_params`` call in
    ``apply_policy_overrides`` failed both topologies (partitioned on share tags /
    published strings via ``benefit_km`` 7.765 vs 7.356; pooled on the award set)."""
    override = _overridden(profiles["base"], BONUS_0)
    assert override["solver_params"]["dual_cycle_bonus_km"] == 0.0
    got = run_case(_case(override, topology), tick_states)
    want = run_case(_case(profiles["compiled_edit"], topology), tick_states)
    default = run_case(_case(profiles["base"], topology), tick_states)
    assert got["pairs"] and got["share_tags"], "vacuous case: no pairs or no share tags"
    assert got["pairs"] != default["pairs"] or got["share_tags"] != default["share_tags"], (
        "bonus 0 must change this tick, or the equality below proves nothing")
    for field in ("pairs", "share_tags", "published", "round_stats", "rng_probe"):
        assert got[field] == want[field], (
            f"[{topology}] per-run override differs from the compiled edit on {field!r}: "
            "the accounting (solver_params) is not re-mirrored from the override")
    if topology == "partitioned":
        assert _benefits(got) == _benefits(want) and _benefits(got), "benefit_km must match"


def test_stamp_records_accounting_params_equal_to_the_policy(profiles):
    """Verified to have teeth: stamping ``accounting_params`` from the bundle's
    pre-override ``solver_params`` failed the equality."""
    from apps.container_logistics.policies.framework import policies_stamp

    override = _overridden(profiles["base"], BONUS_0)
    resolved, _source = current_policies_for_profile(override)
    stamp = policies_stamp(resolved, candidate_settings={},
                           accounting_params=override["solver_params"], source="override")
    d = stamp["dispatch"]
    assert d["accounting_params"] == {"dual_cycle_bonus_km": 0.0, "dual_cycle_radius_km": 0.5}
    shared = {k: d["params"][k] for k in d["accounting_params"]}
    assert shared == d["accounting_params"], "accounting must equal what the policy ranks with"


def test_run_main_stamp_carries_the_effective_solver_params(profiles, capsys):
    """Verified to have teeth: passing ``accounting_params={}`` in
    ``_stamp_effective_policies`` failed the first assertion."""
    import types

    import apps.simulation.run_container_logistics_simulation as launcher

    prof = _overridden(profiles["base"], BONUS_0)
    sm = types.SimpleNamespace(orsim_settings={},
                               get_agent_collection=lambda role: {"a0": {"profile": prof}})
    launcher._stamp_effective_policies(sm, True)
    stamp = sm.orsim_settings["POLICIES"]["dispatch"]
    assert stamp["accounting_params"] == prof["solver_params"]
    assert "accounting_params=" in capsys.readouterr().out


def test_type_change_resets_policy_params_and_mirrors_only_declared_keys():
    """``--solver X`` semantics (fix-round deviation): Greedy's defaults overwrite the
    shared dials; Random declares none, so the bundle's accounting values stay.
    Verified to have teeth: making the mirror write every key of ``solver_params``
    (``target.update(dispatch_params)`` minus the ``k in target`` filter) added
    ``candidates`` to ``solver_params`` and failed the Random assertion."""
    random_bundle = {"strategy": "RandomAssignment",
                     "solver_params": {"dual_cycle_bonus_km": 3.0, "dual_cycle_radius_km": 0.9},
                     "planner": {"deployment": {"type": "RandomAssignment",
                                                "params": {"dual_cycle_bonus_km": 3.0}}}}
    to_greedy = _overridden(random_bundle, {"dispatch": {"type": "GreedyNearest"}})
    assert to_greedy["policies"]["dispatch"]["params"]["dual_cycle_bonus_km"] == 5.0
    assert to_greedy["solver_params"] == {"dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5}
    assert to_greedy["planner"]["deployment"]["params"] == {"dual_cycle_bonus_km": 5.0}

    greedy_bundle = {**random_bundle, "strategy": "GreedyNearest"}
    to_random = _overridden(greedy_bundle, {"dispatch": {"type": "RandomAssignment"}})
    assert to_random["solver_params"] == random_bundle["solver_params"], (
        "Random declares no dual-cycle params: the bundle's accounting must be unchanged")


# --- compile: planner.deployment next to an authored policies block ------------------

@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(tmp_path))
    return tmp_path


def _smoke_with(planner_deployment, policies) -> dict:
    spec = _spec("collab_smoke_12t")
    spec["solver"] = None
    spec["policies"] = policies
    if planner_deployment is None:
        spec["planner"].pop("deployment", None)
    else:
        spec["planner"]["deployment"] = planner_deployment
    return spec


def test_deployment_type_disagreeing_with_policies_is_a_compile_error(root):
    """Verified to have teeth: deleting the type check in
    ``_refuse_deployment_contradicting_policies`` let this compile."""
    spec = _smoke_with({"type": "RandomAssignment", "params": {}},
                       {"dispatch": {"type": "GreedyNearest"}})
    with pytest.raises(SpecValidationError) as exc:
        _compile(spec, root)
    msg = str(exc.value)
    assert "planner.deployment.type='RandomAssignment'" in msg and "'GreedyNearest'" in msg
    assert "remove planner.deployment" in msg
    with pytest.raises(SpecValidationError, match="planner.deployment.type='Nope'"):
        _compile(_smoke_with({"type": "Nope"}, {"dispatch": {"type": "GreedyNearest"}}), root)


def test_deployment_params_disagreeing_with_policies_are_a_compile_error(root):
    """A leftover declared param would be silently overwritten by the mirror.
    Verified to have teeth: deleting the params check let the first compile pass."""
    spec = _smoke_with({"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 9.0}},
                       {"dispatch": {"type": "GreedyNearest"}})
    with pytest.raises(SpecValidationError) as exc:
        _compile(spec, root)
    msg = str(exc.value)
    assert "dual_cycle_bonus_km=9.0 vs policies.dispatch.params.dual_cycle_bonus_km=5.0" in msg, msg
    assert "move the values into policies.dispatch.params" in msg
    # A bool never "agrees" with a number.
    with pytest.raises(SpecValidationError, match="disagree"):
        _compile(_smoke_with({"params": {"dual_cycle_bonus_km": True}},
                             {"dispatch": {"type": "GreedyNearest",
                                           "params": {"dual_cycle_bonus_km": 1.0}}}), root)


def test_agreeing_and_accounting_only_deployment_params_compile(root):
    """Agreeing values (the recipe mirror) and keys the policy does not declare (Random:
    accounting-only) are tolerated; the accounting-only value reaches solver_params.
    Verified to have teeth: refusing every key not in ``policies.dispatch.params``
    failed the Random compile."""
    agree = _smoke_with({"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 2, "dual_cycle_radius_km": 0.5}},
                        {"dispatch": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 2.0}}})
    prof = _profile(_compile(agree, root))
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 2.0
    rnd = _smoke_with({"type": "RandomAssignment", "params": {"dual_cycle_bonus_km": 7.0}},
                      {"dispatch": {"type": "RandomAssignment"}})
    prof = _profile(_compile(rnd, root))
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 7.0
    assert prof["policies"]["dispatch"]["params"] == {"candidates": "sampled"}


_RANDOM_ACCOUNTING = {"type": "RandomAssignment", "params": {"dual_cycle_bonus_km": 7.0}}


@pytest.mark.parametrize("policies,deployment", [
    (None, "shipped"),  # legacy spec, translated; recipe echoes the mirror
    ({"dispatch": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0.0}}}, None),
    ({"dispatch": {"type": "RandomAssignment"}}, _RANDOM_ACCOUNTING),  # accounting-only dial
])
def test_a_compiled_recipe_recompiles_to_the_same_agents(root, policies, deployment):
    """Feeding a compiled recipe back must compile and regenerate identical
    trucks/orders/facilities and the same assignment profile (accounting included).
    Verified to have teeth: refusing every undeclared deployment key failed the
    Random case; making the mirror write every key (not only those already present)
    failed the legacy case; dropping an authored deployment from the recipe
    (``planner.pop`` unconditionally in ``_recipe_planner``) lost bonus 7.0."""
    spec = _spec("collab_smoke_12t")
    if policies is not None:
        spec = _with_policies(spec, policies)
        if deployment is not None:
            spec["planner"]["deployment"] = copy.deepcopy(deployment)
    first = _compile(spec, root)
    second = _compile(first["recipe"], root)
    for role in ("truck", "order", "facility"):
        assert json.dumps(first["agents"][role], sort_keys=True) == json.dumps(
            second["agents"][role], sort_keys=True), f"{role} agents changed on recompile"
    assert _profile(first) == _profile(second), "assignment profile changed on recompile"
    if deployment is _RANDOM_ACCOUNTING:
        assert _profile(second)["solver_params"]["dual_cycle_bonus_km"] == 7.0


def test_compile_mirror_puts_the_policy_default_into_accounting(root, monkeypatch):
    """When the profile's default ``solver_params`` and the policy's declared default
    differ, the compiled accounting must carry the POLICY default (what it ranks with).
    The recipe echoes no deployment mirror next to an authored block (preprocess
    ``_recipe_planner``), so ``spec.json`` cannot carry a stale copy into the next edit.
    Verified to have teeth: removing the profile ``mirror_dispatch_params`` call in
    ``spec_compile._resolve_compiled_policies`` left solver_params at 8.0; echoing
    ``deepcopy(planner_cfg)`` in ``_recipe_planner`` put the mirror in the recipe."""
    from apps.container_logistics.datagen import defaults as D

    sp = copy.deepcopy(D.ASSIGNMENT_SETTINGS["profile"]["solver_params"])
    monkeypatch.setitem(D.ASSIGNMENT_SETTINGS["profile"], "solver_params",
                        {**sp, "dual_cycle_bonus_km": 8.0})
    bundle = _compile(_smoke_with(None, {"dispatch": {"type": "GreedyNearest"}}), root)
    prof = _profile(bundle)
    assert prof["policies"]["dispatch"]["params"]["dual_cycle_bonus_km"] == 5.0
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 5.0, "accounting must equal the policy"
    assert "deployment" not in bundle["recipe"]["planner"], "no mirror echoed next to policies"
    assert prof["planner"]["deployment"]["params"]["dual_cycle_bonus_km"] == 5.0
