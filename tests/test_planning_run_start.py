"""The run-start side of planning declarations (NetworkFlowDay build, WS-D: F1 item 10, R3/R4/R5).

What is pinned:

- **R3** (day foresight under ``ORDER_LIFECYCLE=agents``) is refused with ONE text by the
  sim main and the control-plane dry run (``validate-run-policies``, ``run-preflight``),
  and NOT by scenario compile (the lifecycle mode is a per-run input; compile passes
  ``lifecycle_mode=None``, critique M8). The sim main resolves the mode BEFORE the
  declaration check, so a refused run has stamped nothing planning-related.
- **R4** (``max_hold_seconds`` above the 300 s cap) is refused at all four entry paths.
- An admissible planning declaration stamps ``PLANNING`` (declaration + ``oracle`` +
  ``axis: "elapsed"``), raises ``ORPHAN_TIMEOUT`` to ``max(180, 2*hold+60)``, appends
  `` · oracle:day`` (day foresight only) and ships ``UNASSIGNED_ORDER_MAX_WAIT_STEPS``
  into the assignment profiles as ``unassigned_order_max_wait_steps`` (critique M11);
  a run without a planning declaration gets none of it.
- A solver's own run-name segment may not use a framework run-input prefix
  (``oracle:``, ``cadence:``, ``orders:``).
- **R5** -- a ``top_only`` policy is refused as ``--solver`` and is not listed as a
  per-scope solver.

The planning trees are FAKES (``declaration.planning`` read via ``getattr``), so these
tests hold whether or not the bind contract's ``Declaration.planning`` field exists yet.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import types
from pathlib import Path

import pytest

import apps.simulation.run_container_logistics_simulation as L
from apps.container_logistics.policies.framework import PolicyError
from apps.container_logistics.scenario import run_declaration as RD
from apps.container_logistics.scenario import scenario_overrides as SO

REPO = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO / "scenarios" / "collab_smoke_12t" / "spec.json"
DOMAIN = "container-logistics-sim"

DAY = {"at_day_start": True, "every_steps": 18, "foresight": "day", "max_hold_seconds": 90}
NONE = {"at_day_start": True, "every_steps": None, "foresight": "none", "max_hold_seconds": 30}

R3_TEXT = (
    "planner policy PlanToy declares planning foresight 'day', which reads orders before the "
    "simulation releases them; that needs ORDER_LIFECYCLE=service (orders pre-created with their "
    "request step). This run uses 'agents' (ORSIM_ORDER_LIFECYCLE=agents): drop that override or "
    "select an online planner."
)
R4_TEXT = (
    "planner policy PlanToy declares max_hold_seconds 301, above the planning hold cap 300 s (a held "
    "step stops every agent's clock; runs must stay well inside the 6 h broker visibility timeout). "
    "Lower the solver's time limit."
)


def _tree(planning=None, segments=()):
    declaration = types.SimpleNamespace(stamp_settings={}, run_name_segments=tuple(segments),
                                        cross_company=frozenset())
    if planning is not None:
        declaration.planning = types.MappingProxyType(dict(planning))
    return types.SimpleNamespace(declaration=declaration, top=types.SimpleNamespace(name="PlanToy"),
                                 world_reads=frozenset({"cooperation"}))


@pytest.fixture(scope="module")
def smoke(tmp_path_factory):
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


def _fake_binding(monkeypatch, planning):
    """Every bind on every entry path returns a fake tree declaring ``planning``."""
    import apps.container_logistics.policies.dispatch.bind as bind_mod
    import apps.container_logistics.policies.dispatch.profile as profile_mod
    import apps.container_logistics.scenario.spec_compile as compile_mod

    def resolvable(collection, patched_world_keys=frozenset()):
        return {aid: _tree(planning) for aid, b in (collection or {}).items() if isinstance(b, dict)}

    monkeypatch.setattr(profile_mod, "assert_assignment_profiles_resolvable", resolvable)
    monkeypatch.setattr(compile_mod, "assert_assignment_profiles_resolvable", resolvable)
    monkeypatch.setattr(bind_mod, "bind_profile", lambda profile: _tree(planning))


class _Stop(Exception):
    pass


def _run_main(monkeypatch, assignment, env, planning):
    """Drive ``L.main()`` to ``push_run_status`` with a fake planning top; settings+name or EXIT."""
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
            self.orsim_settings = {"UNASSIGNED_ORDER_MAX_WAIT_STEPS": 360}
            self.reference_time = None
            holder["sm"] = self

        def get_agent_collection(self, role):
            return coll[role]

        def get_scenario_display_name(self):
            return "x"

    def push(topic, run_id, status, **kw):
        raise _Stop(holder["sm"].orsim_settings, kw.get("run_name"), coll)

    _fake_binding(monkeypatch, planning)
    monkeypatch.setattr(L, "_stamp_effective_policies", lambda sm, overridden: _tree(planning))
    monkeypatch.setattr(L, "ScenarioManager", SM)
    monkeypatch.setattr(L, "apply_order_lifecycle_service_mode", lambda sm, name: name)
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
        return ("EXIT", exc.code, out.getvalue(), holder["sm"].orsim_settings)
    raise AssertionError("main() returned without reaching push_run_status")


# --- the pure check --------------------------------------------------------------------


def test_no_planning_declared_is_the_old_plan():
    plan = RD.check_run_declaration(_tree(), settings_keys=(), profile={}, lifecycle_mode="agents")
    assert plan.planning is None
    assert RD.declared_planning(_tree()) is None and RD.declared_planning(None) is None
    # A Declaration without the field at all (older contract) declares nothing.
    bare = types.SimpleNamespace(declaration=types.SimpleNamespace(stamp_settings={}, run_name_segments=()),
                                 top=types.SimpleNamespace(name="X"))
    assert RD.check_run_declaration(bare, settings_keys=(), profile={}).planning is None


def test_the_planning_record():
    plan = RD.check_run_declaration(_tree(DAY), settings_keys=(), profile={}, lifecycle_mode="service")
    assert dict(plan.planning) == {**DAY, "oracle": True, "axis": "elapsed"}
    assert RD.planning_run_name_segment(plan.planning) == "oracle:day"
    none = RD.check_run_declaration(_tree(NONE), settings_keys=(), profile={}, lifecycle_mode="agents")
    assert dict(none.planning) == {**NONE, "oracle": False, "axis": "elapsed"}
    assert RD.planning_run_name_segment(none.planning) is None


@pytest.mark.parametrize("mode", ["agents"])
def test_r3_text(mode):
    with pytest.raises(RD.RunDeclarationError) as exc:
        RD.check_run_declaration(_tree(DAY), settings_keys=(), profile={}, lifecycle_mode=mode)
    assert str(exc.value) == R3_TEXT


@pytest.mark.parametrize("mode", [None, "service"])
def test_r3_does_not_fire_without_agents_mode(mode):
    assert RD.check_run_declaration(_tree(DAY), settings_keys=(), profile={}, lifecycle_mode=mode).planning


def test_r4_text_and_boundary():
    with pytest.raises(RD.RunDeclarationError) as exc:
        RD.check_run_declaration(_tree({**DAY, "max_hold_seconds": 301}), settings_keys=(), profile={})
    assert str(exc.value) == R4_TEXT
    assert RD.check_run_declaration(_tree({**DAY, "max_hold_seconds": 300}), settings_keys=(),
                                    profile={}).planning["max_hold_seconds"] == 300


@pytest.mark.parametrize("hold,current,want", [
    (30, None, None), (60, None, None), (61, None, 182), (90, None, 240), (90, 180, 240),
    (90, 600, None), (300, "x", 660), (300, 100, 660)])
def test_orphan_timeout_raise(hold, current, want):
    # max(180, 2*hold+60); None when the current value (default 180) already suffices.
    assert RD.planning_orphan_timeout({**DAY, "max_hold_seconds": hold}, current) == want
    assert RD.planning_orphan_timeout(None, current) is None


@pytest.mark.parametrize("segment", ["oracle:day", "cadence:6", "orders:2000", "oracle:anything"])
def test_framework_run_input_prefixes_are_refused_as_solver_segments(segment):
    with pytest.raises(RD.RunDeclarationError, match="framework run-input tag"):
        RD.check_run_declaration(_tree(segments=(segment,)), settings_keys=(), profile={})


@pytest.mark.parametrize("segment", ["oracle", "cadence", "my-orders:3"])
def test_lookalikes_without_the_prefix_stay_allowed(segment):
    assert RD.check_run_declaration(_tree(segments=(segment,)), settings_keys=(),
                                    profile={}).run_name_segments == (segment,)


def test_the_new_settings_are_framework_owned():
    for key in ("PLANNING", "ORPHAN_TIMEOUT", "ASSIGNMENT_CADENCE", "MAX_ORDERS_PER_TICK", "WORKING_HOURS"):
        assert key in RD.FRAMEWORK_RUN_SETTINGS and key in RD.reserved_run_settings()
    for key in ("PLANNING", "ASSIGNMENT_CADENCE", "WORKING_HOURS"):
        assert key in RD.FRAMEWORK_READ_SETTINGS


# --- R3 / R4 at every entry path -----------------------------------------------------------


def _via_validate(slug, env):
    from openride_control.policies import validate_run_policy_args

    got = validate_run_policy_args(slug, None, [], [], environ=env)
    return got if isinstance(got, str) else None


def _via_validate_flag(slug, env):
    from openride_control.policies import validate_run_policy_args

    mode = env.get("ORSIM_ORDER_LIFECYCLE")
    got = validate_run_policy_args(slug, None, [], [], environ={}, order_lifecycle=mode)
    return got if isinstance(got, str) else None


def _via_preflight(slug, env):
    from openride_control.policies import run_preflight

    got = run_preflight(slug, environ=env)
    return None if got["ok"] else got["message"]


DRY = {"validate": _via_validate, "validate_flag": _via_validate_flag, "preflight": _via_preflight}


@pytest.mark.parametrize("path", sorted(DRY))
def test_r3_in_the_dry_run(path, smoke, scen_env, monkeypatch):
    _fake_binding(monkeypatch, DAY)
    message = DRY[path](scen_env, {"ORSIM_ORDER_LIFECYCLE": "agents"})
    assert message == R3_TEXT, message
    assert DRY[path](scen_env, {"ORSIM_ORDER_LIFECYCLE": "service"}) is None
    assert DRY[path](scen_env, {}) is None  # unset = service


@pytest.mark.parametrize("path", sorted(DRY))
def test_r4_in_the_dry_run(path, smoke, scen_env, monkeypatch):
    _fake_binding(monkeypatch, {**DAY, "max_hold_seconds": 301})
    assert DRY[path](scen_env, {}) == R4_TEXT


def test_compile_does_not_refuse_r3_but_refuses_r4(smoke, monkeypatch, tmp_path):
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    _root, _slug, spec, _bundle = smoke
    monkeypatch.setenv("ORSIM_ORDER_LIFECYCLE", "agents")  # compile must not read it
    _fake_binding(monkeypatch, DAY)
    (tmp_path / "a").mkdir()
    compile_spec_to_bundle(copy.deepcopy(spec), domain=DOMAIN, scenario_dir=str(tmp_path / "a"), slug="a")
    assert (tmp_path / "a" / "scenario.json").exists()
    _fake_binding(monkeypatch, {**DAY, "max_hold_seconds": 301})
    (tmp_path / "b").mkdir()
    with pytest.raises(PolicyError) as exc:
        compile_spec_to_bundle(copy.deepcopy(spec), domain=DOMAIN, scenario_dir=str(tmp_path / "b"), slug="b")
    assert str(exc.value) == R4_TEXT
    assert not (tmp_path / "b" / "scenario.json").exists()


def test_r3_in_the_sim_main_before_anything_planning_is_stamped(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    got = _run_main(monkeypatch, bundle["agents"]["assignment"], {"ORSIM_ORDER_LIFECYCLE": "agents"}, DAY)
    assert got[:2] == ("EXIT", 2), got
    assert R3_TEXT in got[2]
    settings = got[3]
    for key in ("PLANNING", "ORPHAN_TIMEOUT", "COOPERATION", "ORDER_LIFECYCLE"):
        assert key not in settings, key


def test_r4_in_the_sim_main(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    got = _run_main(monkeypatch, bundle["agents"]["assignment"], {"ORSIM_ORDER_LIFECYCLE": "service"},
                    {**DAY, "max_hold_seconds": 301})
    assert got[:2] == ("EXIT", 2) and R4_TEXT in got[2], got


def test_the_sim_main_stamps_an_admissible_planning_run(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    settings, name, coll = _run_main(monkeypatch, bundle["agents"]["assignment"],
                                     {"ORSIM_ORDER_LIFECYCLE": "service"}, DAY)
    assert settings["PLANNING"] == {**DAY, "oracle": True, "axis": "elapsed"}
    assert settings["ORPHAN_TIMEOUT"] == 240
    assert name.split(" · ")[-1] == "oracle:day", name
    for behavior in coll["assignment"].values():
        assert behavior["profile"][SO.PLANNING_MAX_WAIT_PROFILE_KEY] == 360
    json.dumps(settings["PLANNING"])  # rides the celery JSON boundary


def test_foresight_none_stamps_without_the_oracle_segment(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    settings, name, _coll = _run_main(monkeypatch, bundle["agents"]["assignment"],
                                      {"ORSIM_ORDER_LIFECYCLE": "agents"}, NONE)
    assert settings["PLANNING"]["oracle"] is False and "oracle" not in name
    assert "ORPHAN_TIMEOUT" not in settings  # 2*30+60 = 120 < 180


def test_a_default_run_gets_nothing_planning_related(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    settings, name, coll = _run_main(monkeypatch, bundle["agents"]["assignment"], {}, None)
    for key in ("PLANNING", "ORPHAN_TIMEOUT", "ASSIGNMENT_CADENCE", "MAX_ORDERS_PER_TICK", "WORKING_HOURS"):
        assert key not in settings, key
    assert name == "base · lifecycle:agents", name
    assert coll["assignment"] == bundle["agents"]["assignment"], "a default run patches no profile"


def test_ship_planning_world_helper():
    coll = {"a": {"profile": {}}, "b": {}, "x": "not a behavior"}
    assert SO.ship_planning_world(coll, None) == 0 and coll["a"]["profile"] == {}
    assert SO.ship_planning_world(coll, 360) == 2
    assert coll["a"]["profile"]["unassigned_order_max_wait_steps"] == 360
    assert coll["b"]["profile"]["unassigned_order_max_wait_steps"] == 360


# --- R5: top_only is never the --solver leaf ---------------------------------------------


def test_r5_top_only_is_refused_as_solver(monkeypatch):
    import apps.container_logistics.policies.framework as fw

    real_get, real_list = fw.get_policy_spec, fw.list_policies
    fake = types.SimpleNamespace(name="DayToy", is_composite=False, top_only=True, inner_param=None)

    def get(slot, name):
        return fake if name == "DayToy" else real_get(slot, name)

    monkeypatch.setattr(fw, "get_policy_spec", get)
    monkeypatch.setattr(fw, "list_policies", lambda slot=None: [*real_list(slot), fake])
    with pytest.raises(PolicyError) as exc:
        SO.precheck_override_names({}, "DayToy")
    assert str(exc.value) == (
        "planner policy DayToy plans whole days and must be the top of the selection (top_only); it "
        "cannot be the inner of a decomposition or the --solver leaf. Select it with --policy planner=DayToy."
    )
    with pytest.raises(PolicyError, match="top_only"):
        SO.run_dispatch_overrides({"a": {"profile": {}}}, solver="DayToy")
    assert "DayToy" not in SO.known_solvers()
    with pytest.raises(PolicyError) as exc:
        SO.precheck_override_names({}, "Partitioned")
    assert "DayToy" not in str(exc.value)


# --- FX1 M1: the run's EFFECTIVE dispatch cadence ships to a planning top -------------------


def _applied(behavior):
    return behavior.get("steps_per_action", 1)


def test_a_planning_run_ships_the_applied_cadence(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    authored = {_applied(b) for b in bundle["agents"]["assignment"].values()}
    settings, name, coll = _run_main(monkeypatch, bundle["agents"]["assignment"],
                                     {"ORSIM_ORDER_LIFECYCLE": "service"}, DAY)
    for behavior in coll["assignment"].values():
        shipped = behavior["profile"][RD.PLANNING_CADENCE_PROFILE_KEY]
        assert type(shipped) is int and shipped == _applied(behavior)
    assert {b["profile"][RD.PLANNING_CADENCE_PROFILE_KEY] for b in coll["assignment"].values()} == authored
    assert "cadence:" not in name and "ASSIGNMENT_CADENCE" not in settings


def test_a_planning_run_ships_the_overridden_cadence(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    assert all(_applied(b) != 6 for b in bundle["agents"]["assignment"].values())  # the override is visible
    settings, name, coll = _run_main(monkeypatch, bundle["agents"]["assignment"],
                                     {"ORSIM_ORDER_LIFECYCLE": "service", "ORSIM_ASSIGNMENT_CADENCE": "6"}, DAY)
    for behavior in coll["assignment"].values():
        assert behavior["steps_per_action"] == 6
        assert behavior["profile"][RD.PLANNING_CADENCE_PROFILE_KEY] == 6
    assert settings["ASSIGNMENT_CADENCE"] == {"steps": 6, "source": "override"}
    assert name.split(" · ")[-1] == "cadence:6", name


def test_a_default_run_with_a_cadence_override_ships_no_planning_cadence(smoke, monkeypatch):
    _root, _slug, _spec, bundle = smoke
    settings, name, coll = _run_main(monkeypatch, bundle["agents"]["assignment"],
                                     {"ORSIM_ASSIGNMENT_CADENCE": "6"}, None)
    for behavior in coll["assignment"].values():
        assert behavior["steps_per_action"] == 6
        assert RD.PLANNING_CADENCE_PROFILE_KEY not in behavior["profile"]
        assert SO.PLANNING_MAX_WAIT_PROFILE_KEY not in behavior["profile"]


def test_ship_planning_cadence_helper():
    coll = {"a": {"steps_per_action": 28, "profile": {}}, "b": {}, "x": "not a behavior"}
    assert RD.ship_planning_cadence(coll) == 2
    assert coll["a"]["profile"]["assignment_cadence_steps"] == 28
    assert coll["b"]["profile"]["assignment_cadence_steps"] == 1  # the agent's own default gate
    assert RD.planning_cadence_mismatches(coll) == []
    assert RD.ship_planning_cadence(coll, 6) == 2 and coll["a"]["profile"]["assignment_cadence_steps"] == 6
    # Shipped before the override was applied: the cross-check catches the drift.
    assert RD.planning_cadence_mismatches(coll) == [("a", 6, 28), ("b", 6, 1)]
    assert RD.planning_cadence_mismatches({"c": {"steps_per_action": 3, "profile": {}}}) == []


def test_a_shipped_cadence_the_behavior_does_not_run_is_refused(monkeypatch):
    import types

    coll = {"a": {"steps_per_action": 28, "profile": {RD.PLANNING_CADENCE_PROFILE_KEY: 6}}}
    sm = types.SimpleNamespace(orsim_settings={}, get_agent_collection=lambda role: coll)
    with pytest.raises(PolicyError, match="planning cadence"):
        L._check_planning_cadence(sm)


# --- FX1 minor 9: the stamps spell out their time axis ---------------------------------------


def test_the_planning_and_working_hours_stamps_name_the_elapsed_axis(monkeypatch):
    import types

    hours = {"axis": "elapsed", "hauliers": {"acme": [21600, 72000]}}
    coll = {"a": {"steps_per_action": 28, "profile": {"working_hours": copy.deepcopy(hours)}}}
    sm = types.SimpleNamespace(orsim_settings={"REFERENCE_TIME": "2020-01-01 08:00:00"},
                               get_agent_collection=lambda role: coll)
    monkeypatch.delenv("ORSIM_ASSIGNMENT_CADENCE", raising=False)
    monkeypatch.delenv("ORSIM_MAX_ORDERS_PER_TICK", raising=False)
    with contextlib.redirect_stdout(io.StringIO()):
        L._apply_assignment_run_inputs_or_exit(sm, "base")
        L._apply_planning_record(sm, {**DAY, "oracle": True, "axis": "elapsed"}, "base")
    for key in ("WORKING_HOURS", "PLANNING"):
        assert sm.orsim_settings[key]["axis"] == "elapsed", key
        assert sm.orsim_settings[key]["axis_origin"] == "2020-01-01 08:00:00", key
    assert coll["a"]["profile"]["working_hours"] == hours  # the agents' record is untouched
    assert RD.axis_explicit_record({"hauliers": {}}) == {"hauliers": {}, "axis": "elapsed"}
