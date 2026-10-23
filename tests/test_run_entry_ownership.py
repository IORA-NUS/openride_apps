"""The run's entry paths own the run settings and refuse loudly (final review M1, M3, M11, M12, L1, L8-L10).

What is pinned, by finding:

- **M1** -- every run setting the sim main writes is in ONE list,
  ``run_container_logistics_simulation.FRAMEWORK_RUN_SETTINGS``, every write goes through
  ``_set_run_setting`` (an AST scan of the module), and a solver ``stamp_settings`` key in
  that list is refused (exit 2) even when this run never writes it (the reviewer's
  ``failloud/clash_probe.py``: ``STREAM_GEO``, ``PERSIST_ROUTE_GEO`` and
  ``ORDER_LIFECYCLE`` used to be accepted, then silently overwritten).
- **M3** -- ``COOPERATION`` is framework-owned: ``analytics.manager.cooperation_record``
  derives it from the active arm of the world's cooperation block; it equals the
  built-ins' own ``solver_lib.cooperation.stamp`` record (so their settings are
  byte-identical), a contract-only solver gets it (so the analytics' planner rows
  exist), and a solver stamping a different record is refused.
- **M11 / L8** -- the control-plane dry run runs with no flag at all, and reads the run's
  inputs from the environment the spawned sim will inherit (``policies.sim_environ``,
  pinned against ``ServiceManager.run_simulation_background``'s own injection).
- **L9** -- ``--structure`` with no assignment agent is refused (message 26 family).
- **M12** -- the assignment agent checks the bundle format and binds EAGERLY in
  ``AssignmentApp.__init__`` (the sim main's check and message; one error at start, never
  a per-tick loop), and its bound tree equals the sim main's.
- **L1** -- any ``PolicyError`` raised in the sim main's policy section is exit 2 with the
  message printed.
- **L10** -- ``check_policies_format`` no longer names ``policies.dispatch`` as the current
  slot, and ``scenario compile`` runs the run-start binding before it writes a bundle.

Verified to have teeth (mutate / see it fail / restore), one per pin:

- M1: removing ``or k in FRAMEWORK_RUN_SETTINGS`` from ``_apply_run_declaration`` (the check now
  lives in ``scenario.run_declaration.check_run_declaration``, round 3 M-A) failed
  ``test_a_solver_may_not_stamp_a_framework_run_setting`` for STREAM_GEO,
  PERSIST_ROUTE_GEO, ORDER_LIFECYCLE (MIN_STEP_WALL_TIME_MS stayed refused: the bundle
  holds it); writing ``scenario_manager.orsim_settings["STREAM_GEO"] = False`` directly in
  ``main`` failed ``test_every_run_setting_write_goes_through_the_helper``; dropping
  ``"ORDER_LIFECYCLE"`` from the list failed ``test_the_list_is_exactly_what_main_writes``.
- M3: changing ``sorted(p.members)`` to ``list(p.members)[::-1]`` in ``cooperation_record``
  failed ``test_the_framework_record_equals_the_builtin_stamp``; deleting the
  ``_set_run_setting(... COOPERATION_SETTING, record)`` line failed
  ``test_a_contract_only_solver_gets_the_cooperation_record``; removing the mismatch
  refusal failed ``test_a_solver_stamping_another_cooperation_record_is_refused``.
- M11: restoring the no-flag early ``return {}`` (before the bundle is read) failed
  ``test_the_dry_run_checks_a_stale_bundle_with_no_flag``.
- L8: passing ``legacy_env=None`` / dropping ``refuse_removed_sharing_env`` in
  ``policies._dry_run`` failed ``test_the_dry_run_reads_the_inherited_environment``;
  making ``sim_environ`` skip the ``ORSIM_SOLVER`` overlay failed
  ``test_sim_environ_matches_what_the_manager_injects``.
- L9: restoring ``if blocks else (None, frozenset())`` failed
  ``test_a_structure_with_no_assignment_agent_is_refused``.
- M12: removing the ``_check_selection_at_start`` call failed
  ``test_the_agent_refuses_a_round1_flat_block_at_start``; removing the eager
  ``self._dispatch_runner(prof)`` failed ``test_the_agent_binds_the_same_tree_as_the_sim_main``
  (the start-up check alone already refuses a bind error, so
  ``test_a_bind_refusal_stops_the_agent_at_start`` still passes without it).
- L1: narrowing the ``except PolicyError`` net around main's policy section failed
  ``test_any_policy_error_in_the_policy_section_exits_2`` (a traceback, not exit 2).
- L10: removing the compile-time ``assert_assignment_profiles_resolvable`` failed
  ``test_compile_refuses_a_bundle_the_sim_main_would_refuse`` (the bundle was written).
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

import pytest

import apps.simulation.run_container_logistics_simulation as L
from apps.container_logistics.analytics.manager import COOPERATION_SETTING, cooperation_record
from apps.container_logistics.policies.dispatch.bind import Declaration, bind_profile
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, policy
from apps.container_logistics.policies.framework.registry import isolated_registry

REPO = Path(__file__).resolve().parents[1]
SIM_MAIN = REPO / "apps" / "simulation" / "run_container_logistics_simulation.py"
SMOKE_SPEC = REPO / "scenarios" / "collab_smoke_12t" / "spec.json"


# --- fixtures -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def smoke(tmp_path_factory):
    """``(scenarios_root, slug, bundle)``: ``collab_smoke_12t`` compiled into a temp root."""
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    root = tmp_path_factory.mktemp("scen")
    spec = json.loads(SMOKE_SPEC.read_text())
    (root / spec["slug"]).mkdir()
    bundle = compile_spec_to_bundle(spec, domain="container-logistics-sim",
                                    scenario_dir=str(root / spec["slug"]), slug=spec["slug"])
    return root, spec["slug"], bundle


class _Stop(Exception):
    pass


def _run_main(monkeypatch, assignment, env):
    """Drive ``L.main()`` up to ``push_run_status``; ``(orsim_settings, run_name)`` or ``("EXIT", code, out)``."""
    from apps.utils import kafka_utils as K

    for key in list(os.environ):
        if key.startswith("ORSIM_") and key != "ORSIM_SCENARIOS_DIR":
            monkeypatch.delenv(key)
    for key, value in {"ORSIM_RUN_NAME": "base", "ORSIM_ORDER_LIFECYCLE": "agents", **env}.items():
        monkeypatch.setenv(key, value)
    coll = {"assignment": copy.deepcopy(assignment), "truck": {}, "order": {}, "facility": {}}
    holder = {}

    class SM:
        def __init__(self, *a, **k):
            self.orsim_settings = {}
            self.reference_time = None
            holder["sm"] = self

        def get_agent_collection(self, role):
            return coll[role]

        def get_scenario_display_name(self):
            return "x"

    def push(topic, run_id, status, **kw):
        raise _Stop(holder["sm"].orsim_settings, kw.get("run_name"))

    monkeypatch.setattr(L, "ScenarioManager", SM)
    monkeypatch.setattr(L, "get_datahub_dir", lambda: "/nonexistent-datahub")
    monkeypatch.setattr(L, "_resolve_run_id", lambda: "run_x")
    monkeypatch.setattr(L, "_ensure_transport_lane_topics", lambda run_id: None)
    monkeypatch.setattr(L, "assert_simulation_dependencies", lambda *a, **k: None)
    monkeypatch.setattr(K, "bootstrap_kafka_for_run", lambda run_id: "t")
    monkeypatch.setattr(K, "flush_producer", lambda *a, **k: None)
    monkeypatch.setattr(K, "push_run_status", push)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            L.main()
    except _Stop as stop:
        return stop.args
    except SystemExit as exc:
        return ("EXIT", exc.code, out.getvalue())
    raise AssertionError("main() returned without reaching push_run_status")


def _register_toy(name, *, stamp=None, bind_error=None):
    """Register a contract-only planner leaf ``name`` (inside ``isolated_registry``)."""

    @policy(slot="planner", name=name, version="1", description="test toy: no pairs")
    class Toy:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def __init__(self, params=None):
            self.params = params

        def bind(self, params, world, *, top):
            world.get("cooperation")  # reads the arm's key, so --structure is not message 25
            if bind_error is not None:
                raise bind_error
            return Declaration(stamp_settings=stamp or {})

        def solve_tick(self, tick, params):
            return Result.from_pairs([])

    return Toy


def _central(name):
    return {"ORSIM_POLICY_OVERRIDES": json.dumps({"planner": {"type": name}})}


def _fake_tree(stamp):
    return types.SimpleNamespace(declaration=Declaration(stamp_settings=stamp),
                                 top=types.SimpleNamespace(name="Toy"), world_reads=frozenset())


# --- M1: the run settings the framework writes ------------------------------------


def _writes(tree):
    """``[(function, key or None, kind)]`` for every write into ``orsim_settings`` in ``tree``."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
            for t in targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute) \
                        and t.value.attr == "orsim_settings":
                    key = t.slice.value if isinstance(t.slice, ast.Constant) else None
                    out.append((fn.name, key, "subscript"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if node.func.attr in ("update", "setdefault", "__setitem__") and \
                        isinstance(owner, ast.Attribute) and owner.attr == "orsim_settings":
                    out.append((fn.name, None, node.func.attr))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_set_run_setting":
                key = node.args[1]
                out.append((fn.name, key.value if isinstance(key, ast.Constant) else key.id, "helper"))
    return out


def test_every_run_setting_write_goes_through_the_helper():
    writes = _writes(ast.parse(SIM_MAIN.read_text()))
    direct = [w for w in writes if w[2] == "subscript" and w[0] != "_set_run_setting"]
    assert not direct, f"orsim_settings written past _set_run_setting: {direct}"
    merges = [w for w in writes if w[2] != "subscript" and w[2] != "helper"]
    assert [w[0] for w in merges] == ["_apply_run_declaration"], merges


def test_the_list_is_exactly_what_main_writes():
    keys = {COOPERATION_SETTING if k == "COOPERATION_SETTING" else k
            for k in (w[1] for w in _writes(ast.parse(SIM_MAIN.read_text())) if w[2] == "helper")}
    assert keys == set(L.FRAMEWORK_RUN_SETTINGS), (sorted(keys), sorted(L.FRAMEWORK_RUN_SETTINGS))
    for key in ("POLICIES", "COOPERATION", "STREAM_GEO", "MIN_STEP_WALL_TIME_MS",
                "PERSIST_ROUTE_GEO", "ORDER_LIFECYCLE"):
        assert key in L.FRAMEWORK_RUN_SETTINGS


def test_the_helper_refuses_a_key_outside_the_list():
    sm = types.SimpleNamespace(orsim_settings={})
    with pytest.raises(KeyError, match="FRAMEWORK_RUN_SETTINGS"):
        L._set_run_setting(sm, "SOMETHING_NEW", 1)
    assert sm.orsim_settings == {}


@pytest.mark.parametrize("key", sorted(L.FRAMEWORK_RUN_SETTINGS - {COOPERATION_SETTING}))
def test_a_solver_may_not_stamp_a_framework_run_setting(key, capsys):
    """The reviewer's clash probe, on an EMPTY settings dict: the list alone refuses."""
    sm = types.SimpleNamespace(orsim_settings={})
    with pytest.raises(SystemExit) as exc:
        L._apply_run_declaration(sm, _fake_tree({key: "SOLVER"}), "r", profile={})
    assert exc.value.code == 2
    assert key in capsys.readouterr().out
    assert key not in sm.orsim_settings


def test_a_new_solver_setting_is_still_accepted():
    sm = types.SimpleNamespace(orsim_settings={"POLICIES": {}})
    assert L._apply_run_declaration(sm, _fake_tree({"MY_SOLVER_SETTING": 3}), "r", profile={}) == "r"
    assert sm.orsim_settings["MY_SOLVER_SETTING"] == 3


def test_a_solver_stamping_stream_geo_is_refused_end_to_end(smoke, monkeypatch):
    _root, _slug, bundle = smoke
    with isolated_registry():
        _register_toy("ToyStreamGeo", stamp={"STREAM_GEO": True})
        got = _run_main(monkeypatch, bundle["agents"]["assignment"],
                        {**_central("ToyStreamGeo"), "ORSIM_HEADLESS": "1"})
    assert got[:2] == ("EXIT", 2), got
    assert "STREAM_GEO" in got[2]


# --- M3: COOPERATION is derived from the world -------------------------------------


def _structures_variants(profile):
    """``profile`` with each declared structure active, plus no active arm."""
    coop = profile.get("cooperation") or {}
    for sid in [s.get("id") for s in coop.get("structures", [])] + ["__none__"]:
        variant = copy.deepcopy(profile)
        variant.setdefault("cooperation", {})["active"] = sid
        yield sid, variant


_HAND_BLOCK = {"active": "trio", "structures": [
    {"id": "trio", "components": [["a", "b", "c"], ["d"]],
     "adjacency": {"a": ["b", "c"], "b": ["a", "c"], "c": ["a", "b"]},
     "pools": [{"id": "p1", "members": ["c", "a", "b"]}]},
    {"id": "edges-only", "edges": [["x", "y"]], "adjacency": {"x": ["y"], "y": ["x"]}},
    {"id": "bare"},
]}


def test_the_framework_record_equals_the_builtin_stamp(smoke):
    from apps.container_logistics.policies.solver_lib import cooperation as solver_coop

    _root, _slug, bundle = smoke
    profiles = [next(iter(bundle["agents"]["assignment"].values()))["profile"],
                {"cooperation": _HAND_BLOCK}, {}]
    seen = 0
    for profile in profiles:
        for sid, variant in _structures_variants(profile):
            arm = solver_coop.resolve_arm(variant)
            want = None if arm is None else solver_coop.stamp(arm)
            got = cooperation_record(variant)
            assert json.dumps(got) == json.dumps(want), (sid, got, want)
            seen += want is not None
    assert seen >= 5


@pytest.mark.parametrize("env", [{}, _central("GreedyNearest"), _central("PooledMarket")])
@pytest.mark.parametrize("structure", ["pair", "no-coop"])
def test_the_builtins_still_stamp_exactly_the_framework_record(smoke, monkeypatch, structure, env):
    """A built-in top's own COOPERATION echo is accepted, and the run carries that record."""
    _root, _slug, bundle = smoke
    settings, name = _run_main(monkeypatch, bundle["agents"]["assignment"],
                               {**env, "ORSIM_COOP_STRUCTURE": structure})
    coll = copy.deepcopy(bundle["agents"]["assignment"])
    for b in coll.values():
        b["profile"]["cooperation"]["active"] = structure
    profile = next(iter(coll.values()))["profile"]
    assert dict(bind_profile(profile).declaration.stamp_settings)[COOPERATION_SETTING] == settings[COOPERATION_SETTING]
    assert settings[COOPERATION_SETTING] == cooperation_record(profile)


def test_a_contract_only_solver_gets_the_cooperation_record(smoke, monkeypatch):
    _root, _slug, bundle = smoke
    with isolated_registry():
        _register_toy("ToyContractOnly")
        settings, name = _run_main(monkeypatch, bundle["agents"]["assignment"], _central("ToyContractOnly"))
        profile = next(iter(bundle["agents"]["assignment"].values()))["profile"]
        assert settings[COOPERATION_SETTING] == cooperation_record(profile)
        assert settings[COOPERATION_SETTING]["structure_id"] == "pair"
        # ...for the overridden arm too (world data AFTER --structure):
        settings, name = _run_main(monkeypatch, bundle["agents"]["assignment"],
                                   {**_central("ToyContractOnly"), "ORSIM_COOP_STRUCTURE": "no-coop"})
        assert settings[COOPERATION_SETTING]["structure_id"] == "no-coop"
        assert "· no-coop ·" in name, name


def test_a_solver_stamping_another_cooperation_record_is_refused(smoke, monkeypatch):
    _root, _slug, bundle = smoke
    profile = next(iter(bundle["agents"]["assignment"].values()))["profile"]
    with isolated_registry():
        _register_toy("ToyLiar", stamp={COOPERATION_SETTING: {"structure_id": "evil", "components": [["a", "b"]]}})
        got = _run_main(monkeypatch, bundle["agents"]["assignment"], _central("ToyLiar"))
        assert got[:2] == ("EXIT", 2), got
        assert "framework-owned" in got[2]
        _register_toy("ToyEcho", stamp={COOPERATION_SETTING: cooperation_record(profile)})
        settings, _name = _run_main(monkeypatch, bundle["agents"]["assignment"], _central("ToyEcho"))
        assert settings[COOPERATION_SETTING] == cooperation_record(profile)


def test_no_arm_means_no_record():
    sm = types.SimpleNamespace(orsim_settings={})
    L._apply_run_declaration(sm, _fake_tree({}), "r", profile={"cooperation": {"active": "x", "structures": []}})
    assert COOPERATION_SETTING not in sm.orsim_settings
    with pytest.raises(SystemExit):
        L._apply_run_declaration(sm, _fake_tree({COOPERATION_SETTING: {"structure_id": "x"}}), "r", profile={})


# --- L1: any PolicyError in the policy section is exit 2 ---------------------------


def test_a_bind_time_policy_error_exits_2_with_the_message(smoke, monkeypatch):
    from apps.container_logistics.policies.framework import PolicyParamsError

    _root, _slug, bundle = smoke
    with isolated_registry():
        _register_toy("ToyBadBind", bind_error=PolicyParamsError("toy bind says no"))
        got = _run_main(monkeypatch, bundle["agents"]["assignment"], _central("ToyBadBind"))
    assert got[:2] == ("EXIT", 2), got
    assert "Policy configuration invalid; refusing to start the run:" in got[2]
    assert "toy bind says no" in got[2]


def test_any_policy_error_in_the_policy_section_exits_2(smoke, monkeypatch):
    """A ``PolicyError`` raised OUTSIDE the per-step helpers (here: from the declaration step)."""
    _root, _slug, bundle = smoke

    def boom(*a, **k):
        raise PolicyError("raised outside any helper's own except")

    monkeypatch.setattr(L, "_apply_run_declaration", boom)
    got = _run_main(monkeypatch, bundle["agents"]["assignment"], {})
    assert got[:2] == ("EXIT", 2), got
    assert "raised outside any helper's own except" in got[2]


# --- L9 -------------------------------------------------------------------------------


def test_a_structure_with_no_assignment_agent_is_refused():
    from apps.container_logistics.scenario.scenario_overrides import apply_cooperation_override

    for empty in ({}, None, {"x": "not a behavior"}):
        with pytest.raises(PolicyError, match="no assignment agent"):
            apply_cooperation_override(empty, "pair")
        assert apply_cooperation_override(empty, None) == (None, frozenset())


# --- M11 / L8: the control-plane dry run ---------------------------------------------


@pytest.fixture
def scen_root(smoke, monkeypatch, tmp_path):
    """A scenarios root with the smoke bundle and a STALE copy of it (format marker stripped)."""
    root, slug, bundle = smoke
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    stale = copy.deepcopy(bundle)
    for b in stale["agents"]["assignment"].values():
        b["profile"].pop("policies_format", None)
    (root / "stale_copy").mkdir(exist_ok=True)
    (root / "stale_copy" / "scenario.json").write_text(json.dumps(stale))
    for key in list(os.environ):
        if key.startswith("ORSIM_") and key != "ORSIM_SCENARIOS_DIR":
            monkeypatch.delenv(key)
    return slug


def test_the_dry_run_checks_a_stale_bundle_with_no_flag(scen_root):
    from openride_control.policies import cmd_validate_run_policies, validate_run_policy_args

    msg = validate_run_policy_args("stale_copy", None, [], [])
    assert isinstance(msg, str) and "policies_format=None" in msg, msg
    payload = cmd_validate_run_policies("stale_copy", None)
    assert payload["ok"] is False and payload["action"] == "validate_run_policies" and payload["message"] == msg
    assert cmd_validate_run_policies(scen_root, None) == {
        "ok": True, "action": "validate_run_policies", "overrides": {}}
    # A scenario with no bundle and nothing to patch: the sim compiles it at start.
    assert validate_run_policy_args("does_not_exist_xyz", None, [], []) == {}


def test_the_dry_run_reads_the_inherited_environment(scen_root):
    from openride_control.policies import run_preflight, validate_run_policy_args

    cases = {
        "ORSIM_SHARING_POLICY": ("transfer", "ORSIM_SHARING_POLICY='transfer' is removed"),
        "ORSIM_SOLVER": ("GreedyNeerest", "GreedyNeerest"),
        "ORSIM_MARKET_MAX_ROUNDS": ("5.0", "ORSIM_MARKET_MAX_ROUNDS='5.0' must be an integer"),
        "ORSIM_COOP_STRUCTURE": ("pair-typo", "pair-typo"),
        "ORSIM_POLICY_OVERRIDES": ("{not json", "ORSIM_POLICY_OVERRIDES is not valid JSON"),
    }
    for key, (value, needle) in cases.items():
        env = {key: value}
        if key == "ORSIM_MARKET_MAX_ROUNDS":
            env["ORSIM_PLANNER_TOPOLOGY"] = "pooled"
        msg = validate_run_policy_args(scen_root, None, [], [], environ=env)
        assert isinstance(msg, str) and needle in msg, (key, msg)
        pre = run_preflight(scen_root, environ=env)
        assert pre["ok"] is False and needle in pre["message"], (key, pre)
    # A flag replaces the inherited variable, as the manager's injection does.
    assert validate_run_policy_args(scen_root, "RandomAssignment", [], [],
                                    environ={"ORSIM_SOLVER": "GreedyNeerest"}) == {}
    assert validate_run_policy_args(scen_root, None, [], [], cooperation_structure="no-coop",
                                    environ={"ORSIM_COOP_STRUCTURE": "pair-typo"}) == {}
    # The default reads os.environ.
    os.environ["ORSIM_SHARING_POLICY"] = "x"
    try:
        assert "is removed" in validate_run_policy_args(scen_root, None, [], [])
    finally:
        del os.environ["ORSIM_SHARING_POLICY"]


def test_sim_environ_matches_what_the_manager_injects(monkeypatch):
    """``sim_environ`` == the environment ``ServiceManager.run_simulation_background`` spawns with."""
    import openride_control.manager as mgr_mod
    from openride_control.paths import openride_apps_env
    from openride_control.policies import sim_environ

    seen = {}

    class _Proc:
        def poll(self):
            return 0

    def spawn(extra_env=None, background=True):
        seen["env"] = {**openride_apps_env(), **(extra_env or {})}
        return _Proc()

    monkeypatch.setattr(mgr_mod, "spawn_simulation_process", spawn)
    monkeypatch.setattr(mgr_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mgr_mod.ServiceManager, "is_simulation_running", lambda self: False)
    monkeypatch.setattr(mgr_mod.ServiceManager, "_ensure_backend_for_simulation", lambda self: None)
    monkeypatch.setenv("ORSIM_SOLVER", "Inherited")
    monkeypatch.setenv("ORSIM_PLANNER_TOPOLOGY", "pooled")
    flags = [
        dict(scenario=None, solver=None, overrides=None, cooperation_structure=None),
        dict(scenario="s1", solver=" RandomAssignment ", overrides={"planner": {"type": "PooledMarket"}},
             cooperation_structure=" pair "),
        dict(scenario="s2", solver="  ", overrides={}, cooperation_structure=""),
    ]
    for f in flags:
        try:
            mgr_mod.ServiceManager().run_simulation_background(
                scenario=f["scenario"], solver=f["solver"], policy_overrides=f["overrides"],
                cooperation_structure=f["cooperation_structure"])
        except Exception:  # noqa: BLE001 - only the spawn env matters here
            pass
        want = {k: v for k, v in seen.pop("env").items() if k.startswith("ORSIM_")}
        got = {k: v for k, v in sim_environ(scenario=f["scenario"], solver=f["solver"],
                                             overrides=f["overrides"],
                                             cooperation_structure=f["cooperation_structure"]).items()
               if k.startswith("ORSIM_")}
        for k in ("ORSIM_SCENARIO", "ORSIM_SOLVER", "ORSIM_COOP_STRUCTURE", "ORSIM_POLICY_OVERRIDES",
                  "ORSIM_PLANNER_TOPOLOGY"):
            assert got.get(k) == want.get(k), (f, k, got.get(k), want.get(k))


# --- M12: the agent checks and binds eagerly ------------------------------------------


def _agent(monkeypatch, profile, agent_id="assignment_main"):
    import apps.container_logistics.assignment.app as app_module

    def _stub(self, *, run_id, sim_clock, behavior, messenger, agent_helper=None):
        self.run_id, self.behavior = run_id, behavior

    monkeypatch.setattr(app_module.ORSimApp, "__init__", _stub)
    helper = types.SimpleNamespace(unique_id=agent_id)
    return app_module.AssignmentApp(run_id="r", sim_clock="c", behavior={"profile": profile},
                                    messenger=None, agent_helper=helper)


def test_the_agent_refuses_a_round1_flat_block_at_start(monkeypatch, capsys):
    flat = {"policies": {"planner": {"type": "GreedyNearest"}}}
    with pytest.raises(PolicyError) as exc:
        _agent(monkeypatch, copy.deepcopy(flat))
    with pytest.raises(SystemExit):
        L._assert_policies_resolvable_or_exit({"assignment_main": {"profile": copy.deepcopy(flat)}})
    sim_main_message = capsys.readouterr().out.split("refusing to start the run: ", 1)[1].strip()
    assert str(exc.value) == sim_main_message, (str(exc.value), sim_main_message)
    assert "policies_format=None" in str(exc.value)


def test_a_bind_refusal_stops_the_agent_at_start(monkeypatch):
    from apps.container_logistics.policies.framework import PolicyParamsError

    with isolated_registry():
        _register_toy("ToyAgentBadBind", bind_error=PolicyParamsError("agent bind says no"))
        profile = {"policies": {"planner": {"type": "ToyAgentBadBind", "timing": "online", "params": {}}},
                   "policies_format": 2}
        with pytest.raises(PolicyError, match="agent bind says no"):
            _agent(monkeypatch, profile)


def test_the_agent_binds_the_same_tree_as_the_sim_main(smoke, monkeypatch):
    from apps.container_logistics.scenario.scenario_overrides import run_dispatch_overrides

    _root, _slug, bundle = smoke

    def summary(tree):
        return [(n.name, n.path, json.dumps(n.params.to_dict() if n.params is not None else None,
                                            sort_keys=True, default=str),
                 sorted(n.declaration.cross_company),
                 json.dumps(dict(n.declaration.stamp_settings), sort_keys=True, default=str),
                 tuple(n.declaration.run_name_segments)) for n in tree.nodes] + [sorted(tree.world_reads)]

    for overrides in ({}, {"planner": {"type": "PooledMarket"}}, {"planner": {"type": "GreedyNearest"}}):
        coll = copy.deepcopy(bundle["agents"]["assignment"])
        run_dispatch_overrides(coll, overrides=overrides)
        profile = next(iter(coll.values()))["profile"]
        app = _agent(monkeypatch, copy.deepcopy(profile))
        assert app._dispatch_runner_cache[1].tree is app._dispatch_runner(app._assignment_profile()).tree, \
            "the eager bind is the runner every tick reuses"
        assert summary(app._dispatch_runner(app._assignment_profile()).tree) == summary(bind_profile(profile))


# --- L10 ------------------------------------------------------------------------------


def test_the_round1_message_names_the_slot_rename():
    from apps.container_logistics.policies.compat.legacy import check_policies_format

    with pytest.raises(PolicyError) as exc:
        check_policies_format({"policies": {"planner": {"type": "GreedyNearest"}}})
    text = str(exc.value)
    assert "policies_format=None, not 2: it was compiled before round 2" in text
    assert "a flat policies.dispatch {type: X} meant" not in text
    assert "now 'planner'" in text and "policies.planner {type: X} now means a CENTRAL X" in text


def test_compile_refuses_a_bundle_the_sim_main_would_refuse(tmp_path):
    from apps.container_logistics.policies.framework import PolicyParamsError
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    spec = json.loads(SMOKE_SPEC.read_text())
    spec["policies"] = {"planner": {"type": "ToyCompileBadBind"}}
    spec.pop("planner", None)
    spec.pop("solver", None)
    spec.pop("solverParams", None)
    with isolated_registry():
        _register_toy("ToyCompileBadBind", bind_error=PolicyParamsError("compile bind says no"))
        (tmp_path / spec["slug"]).mkdir()
        with pytest.raises(PolicyError, match="compile bind says no"):
            compile_spec_to_bundle(spec, domain="container-logistics-sim",
                                   scenario_dir=str(tmp_path / spec["slug"]), slug=spec["slug"])
    assert not (tmp_path / spec["slug"] / "scenario.json").exists(), "nothing is written"
