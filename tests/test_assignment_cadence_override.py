"""F4 / F4b: assignment cadence and max orders per tick -- spec field, CLI, stamp, segment (WS-D).

Pinned:

- **R8** text for both inputs (CLI parser == sim env reader == dry run);
- spec fields ``assignmentCadenceSteps`` / ``maxOrdersPerTick`` compile into the
  assignment behavior (``steps_per_action`` / ``profile.max_orders_per_tick``) and the
  recipe echo ONLY when present (absent: identical bundle);
- the sim main patches the behaviors from ``ORSIM_ASSIGNMENT_CADENCE`` /
  ``ORSIM_MAX_ORDERS_PER_TICK``, stamps ``ASSIGNMENT_CADENCE = {steps, source}`` /
  ``MAX_ORDERS_PER_TICK = {orders, source}`` only when overridden (``override``) or
  spec-authored (``bundle``) -- never from the value alone (critique minor 5) -- and
  appends `` · cadence:N`` / `` · orders:N`` ONLY on override;
- the CLI ships the env variables; the control plane's manager injects them.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import types
from pathlib import Path

import pytest

import apps.simulation.run_container_logistics_simulation as L
from apps.container_logistics.datagen.preprocess import SpecValidationError
from apps.container_logistics.policies.framework import PolicyError
from apps.container_logistics.scenario import scenario_overrides as SO

REPO = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO / "scenarios" / "collab_smoke_12t" / "spec.json"
DOMAIN = "container-logistics-sim"


# --- R8 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 361, "0", "abc", "6.0", "-3", True, 6.0, " 1e2 "])
def test_r8_cadence(value):
    with pytest.raises(PolicyError) as exc:
        SO.parse_assignment_cadence(value)
    assert str(exc.value) == f"--assignment-cadence must be an integer step count in 1..360 (got {value!r})"


@pytest.mark.parametrize("value", [0, 20001, "x", False])
def test_r8_max_orders(value):
    with pytest.raises(PolicyError) as exc:
        SO.parse_max_orders_per_tick(value)
    assert str(exc.value) == f"--max-orders-per-tick must be an integer order count in 1..20000 (got {value!r})"


def test_parse_ok():
    assert SO.parse_assignment_cadence(None) is None and SO.parse_assignment_cadence("  ") is None
    assert SO.parse_assignment_cadence("6") == 6 and SO.parse_assignment_cadence(360) == 360
    assert SO.parse_max_orders_per_tick("20000") == 20000 and SO.parse_max_orders_per_tick(1) == 1
    assert SO.assignment_cadence_from_env({"ORSIM_ASSIGNMENT_CADENCE": "12"}) == 12
    assert SO.max_orders_per_tick_from_env({}) is None


def test_the_cli_validator_raises_control_error():
    from openride import control

    assert control.validate_assignment_run_inputs("6", None) == (6, None)
    with pytest.raises(control.ControlError, match=r"--assignment-cadence must be .* \(got '0'\)"):
        control.validate_assignment_run_inputs("0", None)


# --- spec field compile ------------------------------------------------------------------


def _compile(spec, folder):
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    folder.mkdir(parents=True, exist_ok=True)
    return compile_spec_to_bundle(copy.deepcopy(spec), domain=DOMAIN, scenario_dir=str(folder), slug=spec["slug"])


@pytest.fixture(scope="module")
def spec():
    return json.loads(SMOKE_SPEC.read_text())


@pytest.fixture(scope="module")
def plain(spec, tmp_path_factory):
    return _compile(spec, tmp_path_factory.mktemp("plain") / spec["slug"])


def _behavior(bundle):
    return next(iter(bundle["agents"]["assignment"].values()))


def _strip(bundle):
    b = copy.deepcopy(bundle)
    b.pop("createdAt", None)
    return json.dumps(b, sort_keys=True)


def test_spec_fields_compile(spec, plain, tmp_path):
    assert "assignmentCadenceSteps" not in plain["recipe"] and "maxOrdersPerTick" not in plain["recipe"]
    assert _behavior(plain)["steps_per_action"] == 28
    authored = {**copy.deepcopy(spec), "assignmentCadenceSteps": 6, "maxOrdersPerTick": 2000}
    bundle = _compile(authored, tmp_path / "a" / spec["slug"])
    assert _behavior(bundle)["steps_per_action"] == 6
    assert _behavior(bundle)["profile"]["max_orders_per_tick"] == 2000
    assert bundle["recipe"]["assignmentCadenceSteps"] == 6 and bundle["recipe"]["maxOrdersPerTick"] == 2000
    again = _compile(bundle["recipe"], tmp_path / "b" / spec["slug"])
    assert _behavior(again)["steps_per_action"] == 6
    nulled = {**copy.deepcopy(spec), "assignmentCadenceSteps": None, "maxOrdersPerTick": None}
    assert _strip(_compile(nulled, tmp_path / "c" / spec["slug"])) == _strip(plain)


@pytest.mark.parametrize("key,value", [("assignmentCadenceSteps", 0), ("assignmentCadenceSteps", 361),
                                       ("assignmentCadenceSteps", "6"), ("assignmentCadenceSteps", True),
                                       ("maxOrdersPerTick", 20001), ("maxOrdersPerTick", 2.5)])
def test_spec_fields_out_of_range_refuse(spec, tmp_path, key, value):
    with pytest.raises(SpecValidationError, match=f"spec: {key} must be an integer"):
        _compile({**copy.deepcopy(spec), key: value}, tmp_path / spec["slug"])


def test_spec_keys_are_registered_and_carried():
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        SPEC_KEY_NULL_POLICY, SPEC_KEYS, Carry, NullPolicy, assemble_spec)

    for key in ("assignmentCadenceSteps", "maxOrdersPerTick"):
        assert SPEC_KEYS[key] is Carry.CARRY_IF_ABSENT and SPEC_KEY_NULL_POLICY[key] is NullPolicy.UNSUPPLIED
    previous = {"assignmentCadenceSteps": 6, "maxOrdersPerTick": 900}
    out = assemble_spec({"name": "x"}, DOMAIN, previous=previous)
    assert out["assignmentCadenceSteps"] == 6 and out["maxOrdersPerTick"] == 900
    out = assemble_spec({"name": "x", "assignmentCadenceSteps": 12}, DOMAIN, previous=previous)
    assert out["assignmentCadenceSteps"] == 12


# --- the sim main ------------------------------------------------------------------------


def _main(monkeypatch, env, *, recipe=None, steps=28, max_orders=500):
    for key in ("ORSIM_ASSIGNMENT_CADENCE", "ORSIM_MAX_ORDERS_PER_TICK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    coll = {"assignment_main": {"steps_per_action": steps, "profile": {"max_orders_per_tick": max_orders}}}
    settings = {} if recipe is None else {"GENERATION_SPEC": recipe}
    sm = types.SimpleNamespace(orsim_settings=settings, get_agent_collection=lambda role: coll)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            name = L._apply_assignment_run_inputs_or_exit(sm, "base")
    except SystemExit as exc:
        return ("EXIT", exc.code, out.getvalue())
    settings.pop("GENERATION_SPEC", None)
    return settings, name, coll["assignment_main"]


def test_default_run_stamps_nothing(monkeypatch):
    settings, name, behavior = _main(monkeypatch, {}, recipe={"name": "x"})
    assert settings == {} and name == "base" and behavior["steps_per_action"] == 28
    # A legacy bundle whose cadence differs from 28 still gains NO key (minor 5).
    settings, name, _ = _main(monkeypatch, {}, steps=1)
    assert settings == {} and name == "base"


def test_override_patches_stamps_and_segments(monkeypatch):
    settings, name, behavior = _main(monkeypatch, {"ORSIM_ASSIGNMENT_CADENCE": "6",
                                                   "ORSIM_MAX_ORDERS_PER_TICK": "2000"})
    assert behavior["steps_per_action"] == 6 and behavior["profile"]["max_orders_per_tick"] == 2000
    assert settings == {"ASSIGNMENT_CADENCE": {"steps": 6, "source": "override"},
                        "MAX_ORDERS_PER_TICK": {"orders": 2000, "source": "override"}}
    assert name == "base · cadence:6 · orders:2000"
    # Overriding to the bundle's own value is still an override (stamped + tagged).
    settings, name, _ = _main(monkeypatch, {"ORSIM_ASSIGNMENT_CADENCE": "28"})
    assert settings == {"ASSIGNMENT_CADENCE": {"steps": 28, "source": "override"}} and name == "base · cadence:28"


def test_spec_sourced_values_stamp_without_a_segment(monkeypatch):
    settings, name, _ = _main(monkeypatch, {}, recipe={"assignmentCadenceSteps": 6, "maxOrdersPerTick": 900},
                              steps=6, max_orders=900)
    assert settings == {"ASSIGNMENT_CADENCE": {"steps": 6, "source": "bundle"},
                        "MAX_ORDERS_PER_TICK": {"orders": 900, "source": "bundle"}}
    assert name == "base"
    settings, name, _ = _main(monkeypatch, {"ORSIM_ASSIGNMENT_CADENCE": "12"},
                              recipe={"assignmentCadenceSteps": 6}, steps=6)
    assert settings["ASSIGNMENT_CADENCE"] == {"steps": 12, "source": "override"} and name == "base · cadence:12"


def test_r8_in_the_sim_main_is_exit_2(monkeypatch):
    got = _main(monkeypatch, {"ORSIM_ASSIGNMENT_CADENCE": "0"})
    assert got[:2] == ("EXIT", 2)
    assert "--assignment-cadence must be an integer step count in 1..360 (got '0')" in got[2]


def test_r8_in_the_dry_run(monkeypatch):
    from openride_control.policies import cmd_validate_run_policies, run_preflight, validate_run_policy_args

    msg = validate_run_policy_args("does_not_exist_xyz", None, [], [], environ={}, max_orders_per_tick="0")
    assert msg == "--max-orders-per-tick must be an integer order count in 1..20000 (got '0')"
    pre = run_preflight("does_not_exist_xyz", environ={"ORSIM_ASSIGNMENT_CADENCE": "999"})
    assert pre["ok"] is False and "--assignment-cadence must be" in pre["message"]
    assert cmd_validate_run_policies("x", None, environ={}, assignment_cadence="-1")["ok"] is False


# --- the CLI and the control plane carry the inputs -----------------------------------------


def test_the_cli_parses_and_the_runner_ships_the_env():
    import argparse

    from openride import run_group
    from openride.runner import SimulationRunner

    parser = argparse.ArgumentParser()
    run_group.register(parser.add_subparsers())
    args = parser.parse_args(["run", "s", "--assignment-cadence", "6", "--max-orders-per-tick", "2000"])
    assert (args.assignment_cadence, args.max_orders_per_tick) == ("6", "2000")
    env = SimulationRunner(scenario="s", run_id="r", assignment_cadence=6, max_orders_per_tick=2000)._build_env()
    assert env["ORSIM_ASSIGNMENT_CADENCE"] == "6" and env["ORSIM_MAX_ORDERS_PER_TICK"] == "2000"
    assert "ORSIM_ASSIGNMENT_CADENCE" not in SimulationRunner(scenario="s", run_id="r")._build_env()


def test_sim_environ_matches_the_manager_injection(monkeypatch):
    import openride_control.manager as mgr_mod
    from openride_control.paths import openride_apps_env
    from openride_control.policies import sim_environ

    seen = {}

    class _Proc:
        def poll(self):
            return 0

    monkeypatch.setattr(mgr_mod, "spawn_simulation_process",
                        lambda extra_env=None, background=True: seen.setdefault(
                            "env", {**openride_apps_env(), **(extra_env or {})}) and _Proc())
    monkeypatch.setattr(mgr_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mgr_mod.ServiceManager, "is_simulation_running", lambda self: False)
    monkeypatch.setattr(mgr_mod.ServiceManager, "_ensure_backend_for_simulation", lambda self: None)
    for key in ("ORSIM_ASSIGNMENT_CADENCE", "ORSIM_MAX_ORDERS_PER_TICK", "ORSIM_ORDER_LIFECYCLE"):
        monkeypatch.delenv(key, raising=False)
    try:
        mgr_mod.ServiceManager().run_simulation_background(
            scenario="s", order_lifecycle="agents", assignment_cadence="6", max_orders_per_tick=" 2000 ")
    except Exception:  # noqa: BLE001 - only the spawn env matters
        pass
    want = seen["env"]
    got = sim_environ(scenario="s", order_lifecycle="agents", assignment_cadence="6", max_orders_per_tick=" 2000 ")
    for key in ("ORSIM_ASSIGNMENT_CADENCE", "ORSIM_MAX_ORDERS_PER_TICK", "ORSIM_ORDER_LIFECYCLE"):
        assert got.get(key) == want.get(key), key
    assert want["ORSIM_MAX_ORDERS_PER_TICK"] == "2000"


def test_the_control_command_refuses_r8_before_spawning(monkeypatch, capsys):
    import openride_control.manager as mgr_mod
    from openride_control import command

    def no_spawn(*a, **k):
        raise AssertionError("spawned despite R8")

    monkeypatch.setattr(mgr_mod, "spawn_simulation_process", no_spawn)
    code = command._run(["run-simulation", "--scenario", "does_not_exist_xyz", "--assignment-cadence", "0"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and payload["ok"] is False
    assert payload["message"] == "--assignment-cadence must be an integer step count in 1..360 (got '0')"
