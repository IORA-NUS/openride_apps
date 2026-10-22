"""Final review fixes (selection side) of policy slots round 2.

``docs/plans/policy_slots_round2_whole_tick.md`` "Deviations (coder)" -> "Final review
fixes (selection)". One section per review item; each test names the item it pins.
"""

from __future__ import annotations

import copy

import pytest

from apps.container_logistics.policies.framework import PolicyError
from apps.container_logistics.scenario import scenario_overrides as so

# --- MAJOR-1: an override never manufactures the policies_format marker -------------

#: A round-1 bundle profile: flat ``policies.planner``, no ``policies_format`` marker.
ROUND1 = {
    "strategy": "GreedyNearest",
    "planner": {},
    "policies": {"planner": {"type": "GreedyNearest", "version": "1", "kind": "operator",
                              "timing": "online", "params": {}}},
}

OVERRIDE_CASES = [
    ("--solver GreedyNearest", {"solver": "GreedyNearest"}),
    ("--solver RandomAssignment", {"solver": "RandomAssignment"}),
    ("--policy-param planner.dual_cycle_bonus_km=0",
     {"overrides": {"planner": {"params": {"dual_cycle_bonus_km": 0}}}}),
    ("--policy planner=PooledMarket", {"overrides": {"planner": {"type": "PooledMarket"}}}),
    ("ORSIM_PLANNER_TOPOLOGY=pooled", {"legacy_env": {"topology": "pooled"}}),
]


@pytest.mark.parametrize("label,kw", OVERRIDE_CASES, ids=[c[0] for c in OVERRIDE_CASES])
def test_major1_override_on_unmarked_round1_bundle_is_refused(label, kw):
    """Verified to have teeth: removing the ``check_policies_format`` call from
    ``scenario_overrides._profiles_with_current`` failed all five cases (only the
    un-prefixed ``_commit`` backstop refused them; with both removed, the review's
    ``fin/marker_bypass.py`` shows them ACCEPTED as a central solver with marker 2).
    The backstop is pinned separately below."""
    coll = {"a0": {"profile": copy.deepcopy(ROUND1)}}
    with pytest.raises(PolicyError, match=r"assignment agent 'a0': profile\.policies has policies_format=None"):
        so.run_dispatch_overrides(coll, **kw)
    assert coll["a0"]["profile"] == ROUND1  # nothing written, no marker manufactured


def test_major1_round2_and_legacy_profiles_still_accept_overrides():
    marked = {**copy.deepcopy(ROUND1), "policies_format": 2}
    so.run_dispatch_overrides({"a0": {"profile": marked}}, solver="RandomAssignment")
    assert marked["policies_format"] == 2
    legacy = {"strategy": "GreedyNearest"}
    so.run_dispatch_overrides({"a0": {"profile": legacy}}, solver="RandomAssignment")
    assert legacy["policies_format"] == 2 and legacy["strategy"] == "RandomAssignment"


def test_major1_commit_backstop_refuses_to_mark_an_unmarked_block():
    """Verified to have teeth: removing the pre-write ``check_policies_format`` loop
    from ``scenario_overrides._commit`` let it stamp ``policies_format: 2`` onto the
    round-1 profile."""
    from apps.container_logistics.policies.framework import resolve_policies_block

    prof = copy.deepcopy(ROUND1)
    with pytest.raises(PolicyError, match="policies_format=None"):
        so._commit([(prof, resolve_policies_block({}))])
    assert "policies_format" not in prof


def test_major1_control_dry_run_refuses_override_on_unmarked_bundle(monkeypatch):
    """The control plane's dry run (``validate-run-policies``) shares the guard.

    Verified to have teeth: removing both checks (``_profiles_with_current`` and the
    ``_commit`` backstop) made the dry run return the overrides object (accepted)."""
    from openride_control import policies as cp

    monkeypatch.setattr(cp, "load_assignment_collection",
                        lambda scenario: {"a0": {"profile": copy.deepcopy(ROUND1)}})
    for solver, flags in [("RandomAssignment", []), (None, ["planner=PooledMarket"])]:
        msg = cp.validate_run_policy_args("x", solver, flags, [])
        assert isinstance(msg, str) and "policies_format=None" in msg, msg
    msg = cp.validate_run_policy_args("x", None, [], ["planner.dual_cycle_bonus_km=0"])
    assert isinstance(msg, str) and "policies_format=None" in msg, msg


# --- MINOR-9: message 12 names every flag the conflicting type can come from ------


def test_minor9_message12_names_policy_flag_too():
    """``--policy planner=GreedyNearest --solver RandomAssignment``: the conflicting
    type came from ``--policy``, so the message must name it (it used to blame
    ``--policy-param/--policy-overrides`` only).

    Verified to have teeth: the pre-fix wording in ``scenario_overrides._set_innermost``
    failed this equality."""
    with pytest.raises(PolicyError) as exc:
        so.run_dispatch_overrides({"a0": {"profile": {"strategy": "GreedyNearest"}}},
                                  overrides={"planner": {"type": "GreedyNearest"}},
                                  solver="RandomAssignment")
    assert str(exc.value) == (
        "--solver 'RandomAssignment' conflicts with planner.type='GreedyNearest' from "
        "--policy/--policy-param/--policy-overrides; --solver is shorthand for the innermost "
        "solver, pass one of them.")


# --- MINOR-10: message 5's hint survives an explicitly named top -------------------


def test_minor10_message5_hint_when_top_named_and_inner_unstated():
    """``--policy planner=Partitioned --policy-param planner.dual_cycle_bonus_km=0``
    is prechecked before the bundle names the inner, i.e. against the DEFAULT inner
    (RandomAssignment), which lacks the key: the hint now names the registered leaves
    that declare it. A STATED inner that lacks the key gets no hint (it would mislead).

    Verified to have teeth: dropping the ``elif not stated`` branch of
    ``resolve._inner_key_hint`` lost the hint (both decompositions)."""
    from openride_control.policies import validate_run_policy_args

    for top in ("Partitioned", "PooledMarket"):
        msg = validate_run_policy_args("nope", None, [f"planner={top}"], ["planner.dual_cycle_bonus_km=0"])
        assert msg.endswith("'dual_cycle_bonus_km' is a param of an inner solver (e.g. GreedyNearest): "
                            "use planner.inner.dual_cycle_bonus_km."), msg
    msg = validate_run_policy_args("nope", None, ["planner=Partitioned"],
                                   ["planner.inner=RandomAssignment", "planner.dual_cycle_bonus_km=0"])
    assert msg.endswith("Declared params: inner."), msg
    msg = validate_run_policy_args("nope", None, ["planner=Partitioned"], ["planner.nope_key=0"])
    assert msg.endswith("Declared params: inner."), msg
    # The stated-inner hint (spec message 5) is unchanged.
    msg = validate_run_policy_args("nope", None, ["planner=Partitioned"],
                                   ["planner.inner=GreedyNearest", "planner.dual_cycle_bonus_km=0"])
    assert msg.endswith("'dual_cycle_bonus_km' is a param of the inner GreedyNearest: "
                        "use planner.inner.dual_cycle_bonus_km."), msg


# --- MINOR-3: names cannot carry run-name separators; values are escaped -----------


def _register_leaf(name, *, str_param=False):
    from dataclasses import dataclass

    from apps.container_logistics.policies.dispatch.base import DispatchPolicy
    from apps.container_logistics.policies.dispatch.plan import Plan
    from apps.container_logistics.policies.framework import PolicyParams, param, policy

    @policy(slot="planner", name=name, version="1", description="test leaf")
    class Leaf(DispatchPolicy):
        if str_param:
            @dataclass(frozen=True)
            class Params(PolicyParams):
                tag: str = param("x", help="free text")
        else:
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

        def decide(self, ctx):
            return Plan(chains={})

    return Leaf


@pytest.mark.parametrize("bad", ["Bad · Name", "A/B", "X(y)", "X)", "k=v", "a,b", "with space", "1st", "",
                                 "central:X", "Grëedy"])
def test_minor3_policy_names_with_label_separators_are_refused(bad):
    """Verified to have teeth: removing the ``check_policy_name`` call from
    ``registry._check_metadata`` registered every one of these (the empty name is
    refused by the non-empty check either way)."""
    from apps.container_logistics.policies.framework import PolicyRegistrationError
    from apps.container_logistics.policies.framework.registry import isolated_registry

    with isolated_registry():
        with pytest.raises(PolicyRegistrationError):
            _register_leaf(bad)


def test_minor3_ordinary_names_still_register():
    from apps.container_logistics.policies.framework.registry import isolated_registry

    with isolated_registry():
        for good in ("NetFlow", "Net_Flow2", "net-flow.v2", "_Private"):
            _register_leaf(good)


def test_minor3_param_values_are_json_escaped_in_the_label():
    """A param VALUE with the segment separator, a slash, parens or a quote cannot forge a
    run-name segment: ``_label_value`` json-escapes it with ``ensure_ascii=True``.

    Verified to have teeth: ``ensure_ascii=False`` in ``scenario_overrides._label_value``
    put a raw ``' · '`` (and ``ü``) into the segment."""
    from apps.container_logistics.policies.framework import parse_policy_cli_args
    from apps.container_logistics.policies.framework.registry import isolated_registry

    with isolated_registry():
        _register_leaf("StrLeaf", str_param=True)
        cases = {
            "a · b": 'StrLeaf(tag="a \\u00b7 b")',
            "a/b": 'StrLeaf(tag="a/b")',
            "a,b)": 'StrLeaf(tag="a,b)")',
            'q"uo': 'StrLeaf(tag="q\\"uo")',
            "ü": 'StrLeaf(tag="\\u00fc")',
        }
        for value, want in cases.items():
            ov = parse_policy_cli_args([], ["planner.inner=StrLeaf", f"planner.inner.tag={value}"])
            _, labels = so.run_dispatch_overrides({"a0": {"profile": {"strategy": "GreedyNearest"}}},
                                                  overrides=ov)
            assert labels["planner"] == want, (value, labels)
            assert " · " not in labels["planner"] and labels["planner"].isascii()


# --- MINOR-4: --capture-dispatch every=N counts dispatch TICKS, not sim steps ------


def test_minor4_capture_every_n_counts_ticks_not_steps(tmp_path):
    """An agent ticking every 28 steps (``consortium_collab_7d``) with ``every=4`` used
    to capture EVERY tick (``28k % 4 == 0``) and ``every=10`` every 5th; now ``every=N``
    is every N-th tick, the first included.

    Verified to have teeth: restoring ``settings.wants(ctx.clock.time_step)`` in
    ``CaptureRecorder.before_decide`` wrote all 9 ticks for ``every=4``."""
    from apps.container_logistics.policies.dispatch import capture
    from apps.container_logistics.policies.dispatch.runner import build_runner_for_app, build_runner_for_policy
    from apps.container_logistics.policies.testing import fixtures as fx

    for every, want in [(4, [0, 112, 224]), (10, [0]), (3, [0, 84, 168])]:
        directory = tmp_path / f"every{every}"
        prof = {"strategy": "GreedyNearest",
                "policy_capture": {"dir": str(directory), "every_n_ticks": every, "max_files": 50}}
        runner = build_runner_for_app(build_runner_for_policy("GreedyNearest").policy, prof)
        base = fx.three_company_chain()
        for k in range(9):
            runner.run(fx.Fixture(**{**base.__dict__, "time_step": 28 * k}).context(runner))
        got = sorted(capture.read_fixture(str(p))["time_step"] for p in directory.glob("step_*.json.gz"))
        assert got == want, (every, got)


# --- MINOR-6: compile never writes a DERIVED policies block into spec.json --------


def _compile_folder(tmp_path, monkeypatch, slug, spec=None):
    """Copy the shipped ``scenarios/<slug>`` (optionally replacing its spec.json) into a
    tmp ``ORSIM_SCENARIOS_DIR`` and compile it through the real ``compile_scenario``."""
    import json
    import os
    import shutil

    from apps.container_logistics.scenario.frontend_scenario_spec import compile_scenario

    root = tmp_path / "root"
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shutil.copytree(os.path.join(repo, "scenarios", slug), root / slug)
    if spec is not None:
        (root / slug / "spec.json").write_text(json.dumps(spec))
    compile_scenario(repo, "container-logistics-sim", slug)
    return root / slug


def _agents(folder):
    import json

    bundle = json.loads((folder / "scenario.json").read_text())
    return {r: json.dumps(bundle["agents"][r], sort_keys=True) for r in ("truck", "order", "facility", "assignment")}


def test_minor6_legacy_spec_keeps_its_legacy_keys_and_recompiles_identically(tmp_path, monkeypatch):
    """A legacy spec.json (no ``policies``) used to get the translated
    ``Partitioned{inner: GreedyNearest}`` block written into it on its first compile.

    Verified to have teeth: writing the bare recipe in ``_compile_into_folder`` (the
    pre-fix ``write_spec(tmp, recipe)``) put ``policies`` into the written spec.json."""
    import json

    folder = _compile_folder(tmp_path, monkeypatch, "collab_smoke_12t")
    written = json.loads((folder / "spec.json").read_text())
    assert "policies" not in written and written["solver"] == "GreedyNearest"
    prof = next(iter(json.loads((folder / "scenario.json").read_text())["agents"]["assignment"].values()))["profile"]
    assert prof["policies"]["planner"]["type"] == "Partitioned" and prof["policies_format"] == 2
    assert json.loads((folder / "scenario.json").read_text())["recipe"]["policies"]["planner"]["type"] == "Partitioned"
    first = _agents(folder)
    from apps.container_logistics.scenario.frontend_scenario_spec import compile_scenario
    import os

    compile_scenario(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "container-logistics-sim",
                     "collab_smoke_12t")
    assert _agents(folder) == first, "recompile from the written spec.json changed the agents"


def test_minor6_authored_policies_block_is_still_echoed(tmp_path, monkeypatch):
    import json
    import os

    src = json.loads(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "scenarios", "collab_smoke_12t", "spec.json")).read())
    src.pop("solver", None)
    src["planner"] = {k: v for k, v in (src.get("planner") or {}).items() if k not in ("topology", "deployment", "market")}
    src["policies"] = {"planner": {"type": "PooledMarket", "params": {"inner": {"type": "GreedyNearest"}}}}
    folder = _compile_folder(tmp_path, monkeypatch, "collab_smoke_12t", spec=src)
    written = json.loads((folder / "spec.json").read_text())
    assert written["policies"]["planner"]["type"] == "PooledMarket"
    assert written["policies"]["planner"]["params"]["inner"]["type"] == "GreedyNearest"


def test_a_format2_bundle_compiled_before_the_slot_rename_gets_the_hint_and_recompile():
    """A format-2 bundle compiled before ``dispatch`` -> ``planner`` still holds
    ``policies.dispatch``: refused with the rename hint AND the recompile fix, for both
    the marked and the unmarked (round-1) form."""
    from apps.container_logistics.policies.dispatch.legacy import check_policies_format

    entry = {"type": "Partitioned", "version": "1", "kind": "operator", "timing": "online",
             "params": {"inner": {"type": "GreedyNearest", "params": {}}}}
    stale = {"strategy": "GreedyNearest", "policies_format": 2, "policies": {"dispatch": entry}}
    with pytest.raises(PolicyError, match=r"names slot 'dispatch'.*\(The slot 'dispatch' was renamed "
                                          r"to 'planner'\.\) Recompile the scenario"):
        check_policies_format(stale)
    unmarked = {"strategy": "GreedyNearest", "policies": {"dispatch": entry}}
    with pytest.raises(PolicyError, match=r"Recompile the scenario.*\(The slot 'dispatch' was renamed"):
        check_policies_format(unmarked)
    check_policies_format({**stale, "policies": {"planner": entry}})  # the current spelling passes
