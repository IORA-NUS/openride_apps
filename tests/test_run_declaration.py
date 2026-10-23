"""The run-declaration check is ONE pure function, called by every entry path (round-2 review M-A, M-B, FE).

What is pinned, by finding:

- **M-A** -- ``scenario.run_declaration.check_run_declaration`` is called by the sim main
  (``_apply_run_declaration``), the control-plane dry run (``validate-run-policies`` and
  ``run-preflight``) and ``scenario compile``. The reviewer's ClashToy
  (``stamp_settings={"STREAM_GEO": True}``) is refused at all FOUR places; before, the dry
  run and compile passed it and every run exited 2 out of sight.
- **M-B** -- every ``orsim_settings`` key framework code reads or writes is reserved:
  an AST scan of orsim and ``apps/`` finds each reader, and every key it finds must be
  in ``reserved_run_settings()`` (so a new reader with a new key fails here, not in a
  run). ``ORPHAN_TIMEOUT`` and ``CONCURRENT_SCHEDULERS`` (the reviewer's two) are refused.
- **FE** -- a run-name segment equal to a declared cooperation structure id other than
  the active arm, a ``lifecycle:`` tag, a legacy dispatch segment, a dispatch-label shape
  or one carrying the separator is refused; the active arm's id and a solver's own tag
  are not.

Verified to have teeth (mutate / see it fail / restore):

- M-A: dropping the ``check_run_declaration`` loop from ``policies._dry_run`` failed
  ``test_the_clash_toy_is_refused_at_every_entry_path[validate]`` and ``[preflight]``;
  dropping it from ``spec_compile`` failed ``[compile]`` (the bundle was written).
- M-B: removing ``"ORPHAN_TIMEOUT"`` from ``FRAMEWORK_READ_SETTINGS`` failed
  ``test_every_framework_settings_reader_is_reserved`` (``ORPHAN_TIMEOUT`` read by
  ``openride_agent.py`` is not reserved) and ``test_the_reviewers_two_are_refused``.
- FE: deleting the ``text in declared and text != active`` rule failed
  ``test_a_segment_naming_another_declared_arm_is_refused``; making
  ``_looks_like_dispatch_label`` return ``False`` failed
  ``test_framework_segments_are_refused[GreedyNearest]`` and its label cases.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import io
import json
import os
import types
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pytest

import apps.simulation.run_container_logistics_simulation as L
from apps.container_logistics.policies.dispatch.bind import Declaration
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, policy
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.scenario import run_declaration as RD

REPO = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO / "scenarios" / "collab_smoke_12t" / "spec.json"
DOMAIN = "container-logistics-sim"


@pytest.fixture(scope="module")
def smoke(tmp_path_factory):
    """``(scenarios_root, slug, spec, bundle)``: ``collab_smoke_12t`` compiled into a temp root."""
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    root = tmp_path_factory.mktemp("scen")
    spec = json.loads(SMOKE_SPEC.read_text())
    (root / spec["slug"]).mkdir()
    bundle = compile_spec_to_bundle(spec, domain=DOMAIN, scenario_dir=str(root / spec["slug"]), slug=spec["slug"])
    return root, spec["slug"], spec, bundle


@pytest.fixture
def scen_env(smoke, monkeypatch):
    root, slug, _spec, _bundle = smoke
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    for key in list(os.environ):
        if key.startswith("ORSIM_") and key != "ORSIM_SCENARIOS_DIR":
            monkeypatch.delenv(key)
    return slug


def _register_toy(name, *, stamp=None, segments=()):
    @policy(slot="planner", name=name, version="1", description="test toy: no pairs")
    class Toy:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def __init__(self, params=None):
            self.params = params

        def bind(self, params, world, *, top):
            world.get("cooperation")
            return Declaration(stamp_settings=stamp or {}, run_name_segments=tuple(segments))

        def solve_tick(self, tick, params):
            return Result.from_pairs([])

    return Toy


def _tree(stamp=None, segments=()):
    return types.SimpleNamespace(declaration=Declaration(stamp_settings=stamp or {}, run_name_segments=segments),
                                 top=types.SimpleNamespace(name="Toy"), world_reads=frozenset())


_COOP = {"active": "pair", "structures": [{"id": "no-coop", "edges": []},
                                          {"id": "pair", "edges": [["a", "b"]]}]}


# --- M-A: one check, four entry paths -------------------------------------------------


def _via_validate(slug, name, spec, tmp_path):
    from openride_control.policies import validate_run_policy_args

    got = validate_run_policy_args(slug, None, [f"planner={name}"], [])
    return got if isinstance(got, str) else None


def _via_preflight(slug, name, spec, tmp_path):
    from openride_control.policies import run_preflight

    got = run_preflight(slug, policies=[f"planner={name}"])
    return None if got["ok"] else got["message"]


def _via_compile(slug, name, spec, tmp_path):
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    authored = copy.deepcopy(spec)
    authored["policies"] = {"planner": {"type": name}}
    for key in ("planner", "solver", "solverParams"):
        authored.pop(key, None)
    folder = tmp_path / name
    folder.mkdir()
    try:
        compile_spec_to_bundle(authored, domain=DOMAIN, scenario_dir=str(folder), slug=name)
    except PolicyError as exc:
        assert not (folder / "scenario.json").exists(), "a refused compile writes nothing"
        return str(exc)
    return None


def _via_main(slug, name, spec, tmp_path, *, bundle=None):
    from apps.container_logistics.policies.dispatch.bind import bind_profile
    from apps.container_logistics.scenario.scenario_overrides import run_dispatch_overrides

    coll = copy.deepcopy(bundle["agents"]["assignment"])
    run_dispatch_overrides(coll, overrides={"planner": {"type": name}}, solver=None, legacy_env=None)
    profile = next(iter(coll.values()))["profile"]
    sm = types.SimpleNamespace(orsim_settings=dict(bundle["settings"], POLICIES={}))
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            L._apply_run_declaration(sm, bind_profile(profile), "base", profile)
    except SystemExit as exc:
        assert exc.code == 2
        return out.getvalue()
    return None


PATHS = {"validate": _via_validate, "preflight": _via_preflight, "compile": _via_compile, "main": _via_main}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_the_clash_toy_is_refused_at_every_entry_path(path, smoke, scen_env, tmp_path):
    _root, slug, spec, bundle = smoke
    with isolated_registry():
        _register_toy("ClashToy", stamp={"STREAM_GEO": True})
        kwargs = {"bundle": bundle} if path == "main" else {}
        message = PATHS[path](slug, "ClashToy", spec, tmp_path, **kwargs)
    assert message is not None, f"{path} accepted the ClashToy"
    assert "STREAM_GEO" in message and "framework owns" in message, message


@pytest.mark.parametrize("path", sorted(PATHS))
def test_an_admissible_declaration_passes_every_entry_path(path, smoke, scen_env, tmp_path):
    _root, slug, spec, bundle = smoke
    with isolated_registry():
        _register_toy("OkToy", stamp={"MY_SOLVER_SETTING": 1}, segments=("zones3",))
        kwargs = {"bundle": bundle} if path == "main" else {}
        assert PATHS[path](slug, "OkToy", spec, tmp_path, **kwargs) is None


def test_the_sim_main_merges_exactly_what_the_check_returns():
    sm = types.SimpleNamespace(orsim_settings={"POLICIES": {}})
    tree = _tree({"MY_A": 1, "MY_B": [2]}, ("zones3",))
    with contextlib.redirect_stdout(io.StringIO()):
        assert L._apply_run_declaration(sm, tree, "base", {"cooperation": _COOP}) == "base · zones3"
    plan = RD.check_run_declaration(tree, settings_keys={"POLICIES"}, profile={"cooperation": _COOP})
    assert list(sm.orsim_settings) == ["POLICIES", "COOPERATION", "MY_A", "MY_B"]
    assert sm.orsim_settings["COOPERATION"] == plan.cooperation == RD.cooperation_record({"cooperation": _COOP})


def test_the_check_is_pure():
    profile = MappingProxyType({"cooperation": MappingProxyType(_COOP)})
    keys = frozenset({"POLICIES"})
    snapshot = json.dumps(_COOP, sort_keys=True)
    plan = RD.check_run_declaration(_tree({"MY_A": 1}, ("zones3",)), settings_keys=keys, profile=profile)
    assert json.dumps(_COOP, sort_keys=True) == snapshot
    assert dict(plan.settings) == {"MY_A": 1} and plan.run_name_segments == ("zones3",)
    assert RD.check_run_declaration(None, settings_keys=keys, profile=profile) == RD.RunDeclarationPlan()


def test_the_analytics_manager_re_exports_the_record():
    from apps.container_logistics.analytics import manager

    assert manager.cooperation_record is RD.cooperation_record
    assert manager.COOPERATION_SETTING == RD.COOPERATION_SETTING == "COOPERATION"


# --- M-B: the reserved settings are derived from the readers ---------------------------


def _receiver_is_settings(node) -> bool:
    text = ast.unparse(node)
    return text.split(".")[-1].endswith("orsim_settings") or text.endswith("['orsim_settings']")


def _framework_setting_keys():
    """``{key: [file, ...]}`` of every string key read or written on an ``*orsim_settings`` receiver."""
    import orsim

    roots = [Path(orsim.__file__).parent, REPO / "apps"]
    found, unresolved = {}, []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                key = None
                if isinstance(node, ast.Subscript) and _receiver_is_settings(node.value):
                    key = node.slice
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in ("get", "setdefault", "pop") and node.args
                      and _receiver_is_settings(node.func.value)):
                    key = node.args[0]
                elif (isinstance(node, ast.Compare) and len(node.ops) == 1
                      and isinstance(node.ops[0], (ast.In, ast.NotIn)) and _receiver_is_settings(node.comparators[0])):
                    key = node.left
                if key is None:
                    continue
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.setdefault(key.value, []).append(str(path))
                else:
                    unresolved.append((str(path), node.lineno, ast.unparse(key)))
    return found, unresolved


def test_every_framework_settings_reader_is_reserved():
    found, unresolved = _framework_setting_keys()
    assert len(found) >= 35, f"the scan found too little to be scanning the readers: {sorted(found)}"
    missing = {k: v for k, v in found.items() if k not in RD.reserved_run_settings()}
    assert not missing, (
        f"framework code reads orsim_settings keys a solver may still stamp: {missing}. "
        "Add them to run_declaration.FRAMEWORK_READ_SETTINGS.")
    # Non-literal keys exist only where the sim main checks / writes through its helpers.
    sim_main = str(REPO / "apps" / "simulation" / "run_container_logistics_simulation.py")
    assert {p for p, _line, _key in unresolved} <= {sim_main}, unresolved


def test_orsims_schema_is_reserved():
    from orsim.core.orsim_env import orsim_settings_schema

    assert set(orsim_settings_schema) <= RD.FRAMEWORK_READ_SETTINGS


def test_the_runtime_knobs_are_reserved_from_their_source():
    from apps.container_logistics.scenario.runtime_settings import apply_long_run_orsim_settings

    assert set(apply_long_run_orsim_settings({})) <= RD.reserved_run_settings()
    assert RD.FRAMEWORK_RUN_SETTINGS <= RD.reserved_run_settings()


@pytest.mark.parametrize("key", ["ORPHAN_TIMEOUT", "CONCURRENT_SCHEDULERS", "STEP_SETTLE_SECONDS", "HEARTBEAT_INTERVAL"])
def test_the_reviewers_two_are_refused(key, capsys):
    """The reviewer's probe (``r2_fc/clash2.py``) on EMPTY settings: the reserved set alone refuses."""
    sm = types.SimpleNamespace(orsim_settings={})
    with pytest.raises(SystemExit) as exc:
        L._apply_run_declaration(sm, _tree({key: "SOLVER"}), "r", profile={})
    assert exc.value.code == 2 and key in capsys.readouterr().out
    with pytest.raises(RD.RunDeclarationError, match=key):
        RD.check_run_declaration(_tree({key: "SOLVER"}), settings_keys=(), profile={})


def test_a_key_the_bundle_holds_is_refused_too():
    with pytest.raises(RD.RunDeclarationError, match="SCENARIO_ONLY_KEY"):
        RD.check_run_declaration(_tree({"SCENARIO_ONLY_KEY": 1}), settings_keys={"SCENARIO_ONLY_KEY"}, profile={})


# --- FE: run-name segments ----------------------------------------------------------------


def test_a_segment_naming_another_declared_arm_is_refused():
    with pytest.raises(RD.RunDeclarationError, match="no-coop.*active arm is 'pair'"):
        RD.check_run_declaration(_tree(segments=("no-coop",)), settings_keys=(), profile={"cooperation": _COOP})
    # No active arm at all: every declared id is "another" arm.
    inactive = {"cooperation": {**_COOP, "active": None}}
    with pytest.raises(RD.RunDeclarationError, match="pair"):
        RD.check_run_declaration(_tree(segments=("pair",)), settings_keys=(), profile=inactive)


def test_the_active_arm_id_is_allowed():
    plan = RD.check_run_declaration(_tree(segments=("pair",)), settings_keys=(), profile={"cooperation": _COOP})
    assert plan.run_name_segments == ("pair",)


@pytest.mark.parametrize("segment", [
    "lifecycle:agents", "lifecycle:service", "pooled", "partitioned", "two-stage",
    "GreedyNearest", "RandomAssignment(seed=3)", "PooledMarket/GreedyNearest",
    "Partitioned(max_trucks_per_haulier=5)/RandomAssignment",
    "x · y", "", "  ", " pad",
])
def test_framework_segments_are_refused(segment):
    with pytest.raises(RD.RunDeclarationError, match="run-name segment"):
        RD.check_run_declaration(_tree(segments=(segment,)), settings_keys=(), profile={"cooperation": _COOP})


@pytest.mark.parametrize("segment", ["zones3", "chain", "GreedyNearestish", "my/own", "Foo(x=1)"])
def test_a_solvers_own_tags_are_allowed(segment):
    plan = RD.check_run_declaration(_tree(segments=(segment,)), settings_keys=(), profile={"cooperation": _COOP})
    assert plan.run_name_segments == (segment,)


def test_a_non_str_segment_is_refused():
    class Sneaky(str):
        pass

    for segment in (Sneaky("zones3"), 3):
        tree = types.SimpleNamespace(declaration=types.SimpleNamespace(stamp_settings={}, run_name_segments=(segment,)),
                                     top=types.SimpleNamespace(name="Toy"))
        with pytest.raises(RD.RunDeclarationError, match="not a non-empty string"):
            RD.check_run_declaration(tree, settings_keys=(), profile={})


def test_a_segment_naming_another_arm_is_refused_by_the_dry_run(smoke, scen_env):
    from openride_control.policies import cmd_validate_run_policies, run_preflight

    _root, slug, _spec, _bundle = smoke
    with isolated_registry():
        _register_toy("ArmLiar", segments=("no-coop",))
        payload = cmd_validate_run_policies(slug, None, [f"planner=ArmLiar"])
        assert payload["ok"] is False and "no-coop" in payload["message"], payload
        # Under --structure no-coop the arm IS active, so the same segment is true and allowed.
        assert cmd_validate_run_policies(slug, None, ["planner=ArmLiar"], cooperation_structure="no-coop")["ok"]
        assert run_preflight(slug, policies=["planner=ArmLiar"])["ok"] is False


def test_the_builtins_on_every_compiled_spec_pass(smoke):
    """No default tree trips the check (byte-identity of defaults): the compiled smoke bundle,
    plus every bundle under ``ORSIM_SCENARIOS_DIR`` when it is set (the gate's compiled copies)."""
    from apps.container_logistics.policies.dispatch.bind import bind_profile
    from apps.container_logistics.scenario.scenario_bundle import read_bundle

    bundles = [smoke[3]]
    extra = os.environ.get("ORSIM_SCENARIOS_DIR", "").strip()
    if extra and Path(extra).is_dir():
        bundles += [b for b in (read_bundle(str(d)) for d in sorted(Path(extra).iterdir()) if d.is_dir()) if b]
    for bundle in bundles:
        for behavior in (bundle["agents"].get("assignment") or {}).values():
            profile = behavior["profile"]
            plan = RD.check_run_declaration(bind_profile(copy.deepcopy(profile)),
                                            settings_keys=bundle["settings"], profile=profile)
            assert plan.cooperation == RD.cooperation_record(profile)
