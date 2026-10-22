"""Locks the ``openride policy`` CLI group, its control wrappers, and the capture plumbing.

Plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.4/P3.6/P3.7. What is locked and why:

- the ``policy`` group exposes list/show/validate/bench, and ``solver list`` renders
  ``policy list dispatch`` restricted to the per-scope solvers (one catalog, no
  second list to drift); ``policy list`` shows the component kinds and an
  information-model column, ``policy show`` renders ref params (round-2 §5 C4);
- the control wrappers parse the control plane's payloads (the transport is
  monkeypatched; the real command is exercised by the P3 gate);
- ``policy validate`` exits 1 on any FAIL, so it can gate a script;
- ``every=N,max=M`` is parsed by ONE function (``dispatch.capture.parse_capture_spec``)
  that the sim main, the control command and the CLI all use, and the sim main maps
  ``ORSIM_CAPTURE_DISPATCH`` onto every assignment profile in a form
  ``CaptureSettings.from_profile`` accepts.

``main()`` of the sim needs Kafka and the preflight services, so the env mapping is
driven through the helper it calls (as ``tests/test_policy_fail_loud.py`` does).
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from apps.container_logistics.policies.dispatch.capture import CaptureSettings, parse_capture_spec
from apps.container_logistics.policies.framework import PolicyError
from openride import cli, control
from openride_control import command as control_command
from openride_control.manager import ServiceManager
from openride_control.models import ActionResult, ServiceState

CLI_DIR = Path(__file__).resolve().parents[1] / "openride"

_FAKE_CATALOG = {
    "dispatch": {
        "kind": "operator", "default": "Beta",
        "timings": ["online"], "executable_timings": ["online"], "description": "d",
        "policies": [
            {"name": "Alpha", "version": "3", "description": "first", "requires": ["ortools"],
             "missing": ["ortools"], "available": False, "order_sensitive": None,
             "params_schema": {"type": "object", "properties": {
                 "k": {"type": "number", "default": 1.0, "minimum": 0.0, "description": "a knob"}}},
             "defaults": {"k": 1.0}},
            {"name": "Beta", "version": "1", "description": "second", "requires": [], "missing": [],
             "available": True, "order_sensitive": None, "information_model": "per_group",
             "composite": True, "inner_param": "inner",
             "params_schema": {"type": "object", "properties": {
                 "offer": {"type": "object", "x-policy-ref": "market.offer", "x-leaf-only": False,
                           "default": {"type": "OfferAll", "params": {}}, "description": "an offer"},
                 "inner": {"type": "object", "x-policy-ref": "dispatch", "x-leaf-only": True,
                           "default": {"type": "Alpha", "params": {"k": 1.0}}, "description": "the solver"}}},
             "defaults": {}},
        ],
    },
}
_FAKE_COMPONENTS = {
    "market.offer": {"default": "OfferAll", "description": "offers", "policies": [
        {"name": "OfferAll", "version": "1", "description": "all", "defaults": {},
         "params_schema": {"type": "object", "properties": {}}},
        {"name": "OfferNone", "version": "1", "description": "none", "defaults": {},
         "params_schema": {"type": "object", "properties": {}}}]},
}


def _fake_catalogs(monkeypatch):
    from openride.ui import console

    monkeypatch.setattr(control, "known_policy_slots", lambda: _FAKE_CATALOG)
    monkeypatch.setattr(control, "known_policy_components", lambda: _FAKE_COMPONENTS)
    monkeypatch.setattr(console, "width", 240)  # no cell wrapping in the asserted text


def _subparser_names(tree: ast.AST, subparsers_var: str) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == subparsers_var and node.args
                and isinstance(node.args[0], ast.Constant)):
            names.add(node.args[0].value)
    return names


def _assigned_from(tree: ast.AST, var: str) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == var
                and isinstance(node.value, ast.Call)):
            return node.value
    raise AssertionError(f"no assignment to {var!r} in policy_cmd.py")


def test_policy_group_has_list_show_validate_bench_subcommands():
    """AST: ``policy`` is a top-level group whose subparsers are list/show/validate/bench.

    Verified to have teeth: renaming ``psub.add_parser("bench", ...)`` to ``"benchmark"``
    failed the subcommand assertion; dropping ``policy_cmd.register(sub)`` from cli.py
    failed the registration assertion.
    """
    tree = ast.parse((CLI_DIR / "policy_cmd.py").read_text())
    group = _assigned_from(tree, "pol")
    assert isinstance(group.func, ast.Attribute) and group.func.attr == "add_parser"
    assert group.args[0].value == "policy", "the group must be registered as `policy`"
    subparsers = _assigned_from(tree, "psub")
    assert subparsers.func.attr == "add_subparsers" and subparsers.func.value.id == "pol"
    assert _subparser_names(tree, "psub") == {"list", "show", "validate", "bench"}

    cli_tree = ast.parse((CLI_DIR / "cli.py").read_text())
    calls = {
        (n.func.value.id, n.func.attr) for n in ast.walk(cli_tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
    }
    assert ("policy_cmd", "register") in calls, "cli.py must register the policy group"


def test_policy_group_parses_each_subcommand():
    """The real parser accepts every documented form (and rejects bench without a source)."""
    parser = cli._build_parser()
    assert parser.parse_args(["policy", "list"]).slot is None
    assert parser.parse_args(["policy", "list", "dispatch", "--json"]).json is True
    shown = parser.parse_args(["policy", "show", "dispatch", "GreedyNearest"])
    assert (shown.slot, shown.name) == ("dispatch", "GreedyNearest")
    assert parser.parse_args(["policy", "validate", "dispatch", "X"]).name == "X"
    bench = parser.parse_args(["policy", "bench", "dispatch", "--policies", "A,B", "--synthetic", "scale",
                               "--policy-param", "A.k=1"])
    assert (bench.synthetic, bench.fixtures, bench.policy_param) == ("scale", None, ["A.k=1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["policy", "bench", "dispatch", "--policies", "A"])
    with pytest.raises(SystemExit):
        parser.parse_args(["policy", "bench", "dispatch", "--policies", "A", "--synthetic", "scale",
                           "--fixtures", "/tmp"])


def test_solver_list_is_policy_list_dispatch_restricted_to_leaves(monkeypatch, capsys):
    """``solver list`` prints ``policy list dispatch`` without the decompositions (round 2:
    ``--solver`` accepts only per-scope solvers), text and JSON.

    Verified to have teeth: changing it to ``render_policy_list(None, ...)`` (every slot
    instead of dispatch) failed the JSON keys; dropping ``leaves_only=True`` listed Beta.
    """
    _fake_catalogs(monkeypatch)

    def out(argv):
        assert cli.main(argv) == 0
        return capsys.readouterr().out

    leaves, full = out(["solver", "list"]), out(["policy", "list", "dispatch"])
    assert "Alpha" in leaves and "Beta" not in leaves and "Beta" in full
    alias_json = json.loads(out(["solver", "list", "--json"]))
    assert list(alias_json) == ["dispatch"]
    assert [p["name"] for p in alias_json["dispatch"]["policies"]] == ["Alpha"]
    full_json = json.loads(out(["policy", "list", "dispatch", "--json"]))
    assert [p["name"] for p in full_json["dispatch"]["policies"]] == ["Alpha", "Beta"]


def test_policy_list_marks_default_missing_deps_components_and_models(monkeypatch, capsys):
    """Verified to have teeth: dropping the component rows from ``render_policy_list``
    failed the ``OfferNone`` assertion; dropping the model column failed ``per_group``."""
    _fake_catalogs(monkeypatch)
    assert cli.main(["policy", "list"]) == 0
    text = capsys.readouterr().out
    assert "missing: ortools" in text, "a policy with a missing dependency must say so"
    assert "market.offer" in text and "OfferNone" in text and "component" in text
    assert "planned" not in text and "lands in" not in text, "no slot has a lifecycle status any more"
    assert "per_group" in text and "composite" in text
    assert "✓" in text
    assert cli.main(["policy", "list", "market.offer"]) == 0
    only_kind = capsys.readouterr().out
    assert "OfferNone" in only_kind and "Alpha" not in only_kind
    assert cli.main(["policy", "list", "nope"]) == 2
    assert "unknown policy slot or component kind 'nope'" in capsys.readouterr().out


def test_policy_show_renders_params_and_rejects_unknown_names(monkeypatch, capsys):
    """Verified to have teeth: rendering a ref param's raw ``type`` (``object``) instead
    of ``_type_cell`` failed the ``ref → market.offer`` assertion."""
    _fake_catalogs(monkeypatch)
    assert cli.main(["policy", "show", "dispatch", "Alpha"]) == 0
    text = capsys.readouterr().out
    assert "version 3" in text and "a knob" in text and ">= 0.0" in text
    assert cli.main(["policy", "show", "dispatch", "Beta"]) == 0
    text = capsys.readouterr().out
    assert "ref → market.offer" in text and "OfferAll, OfferNone" in text, "a ref lists its kind's names"
    assert "ref → dispatch (leaf)" in text and "per_group" in text
    assert cli.main(["policy", "show", "market.offer", "OfferNone"]) == 0
    assert "none" in capsys.readouterr().out
    assert cli.main(["policy", "show", "dispatch", "Gamma"]) == 2
    assert "registered: Alpha, Beta" in capsys.readouterr().out
    assert cli.main(["policy", "show", "idle", "X"]) == 2


def test_policy_validate_exits_1_on_any_fail(monkeypatch, capsys):
    """Verified to have teeth: returning 0 unconditionally from ``_cmd_validate`` failed it."""
    results = [{"id": "structure.returns_plan", "ok": True, "detail": "fine", "ms": 1.0},
               {"id": "performance.scale", "ok": False, "detail": "too slow", "ms": 900.0}]
    monkeypatch.setattr(control, "policy_validate", lambda slot, name: {
        "ok": False, "results": results, "message": "1 check(s) failed: performance.scale"})
    assert cli.main(["policy", "validate", "dispatch", "Alpha"]) == 1
    text = capsys.readouterr().out
    assert "FAIL" in text and "too slow" in text
    monkeypatch.setattr(control, "policy_validate", lambda slot, name: {
        "ok": True, "results": results[:1], "message": "all 1 checks passed"})
    assert cli.main(["policy", "validate", "dispatch", "Alpha"]) == 0
    assert "PASS" in capsys.readouterr().out


def _transport(monkeypatch, payload):
    seen = []

    def fake(args, **kw):
        seen.append(list(args))
        return payload

    monkeypatch.setattr(control, "_run_command", fake)
    return seen


def test_known_policy_slots_wrapper_parses_the_catalog(monkeypatch):
    seen = _transport(monkeypatch, {"ok": True, "policies": {}, "defaults": {}, "slots": _FAKE_CATALOG})
    assert control.known_policy_slots() == _FAKE_CATALOG
    assert seen == [["known-policies"]]
    _transport(monkeypatch, {"ok": True, "policies": {}, "defaults": {}})
    with pytest.raises(control.ControlError, match="no 'slots'"):
        control.known_policy_slots()


def test_policy_validate_wrapper_returns_failed_results_and_raises_on_refusal(monkeypatch):
    """Verified to have teeth: dropping the ``results`` pass-through in
    ``control.policy_validate`` turned a battery FAIL into a ControlError."""
    payload = {"ok": False, "action": "policy_validate", "results": [{"id": "a.b", "ok": False}]}
    seen = _transport(monkeypatch, payload)
    assert control.policy_validate("dispatch", "X") == payload, "a FAIL is a result, not an error"
    assert seen == [["policy-validate", "--slot", "dispatch", "--name", "X"]]
    _transport(monkeypatch, {"ok": False, "code": "INVALID", "message": "Unknown dispatch policy 'X'"})
    with pytest.raises(control.ControlError, match="Unknown dispatch policy") as excinfo:
        control.policy_validate("dispatch", "X")
    assert excinfo.value.code == "INVALID"


def test_policy_bench_wrapper_builds_argv_and_parses_report(monkeypatch):
    seen = _transport(monkeypatch, {"ok": True, "report": {"rows": [1]}, "table": "T"})
    out = control.policy_bench("dispatch", ["A", "B"], synthetic="scale", params=["A.k=1", "B.j=x"])
    assert out == {"report": {"rows": [1]}, "table": "T"}
    assert seen == [["policy-bench", "--slot", "dispatch", "--policies", "A,B", "--synthetic", "scale",
                     "--policy-param", "A.k=1", "--policy-param", "B.j=x"]]
    seen = _transport(monkeypatch, {"ok": True, "report": {}, "table": ""})
    control.policy_bench("dispatch", ["A"], fixtures_dir="/d")
    assert seen[0][-2:] == ["--fixtures", "/d"]
    _transport(monkeypatch, {"ok": False, "code": "INVALID", "message": "no step_*.json.gz fixtures"})
    with pytest.raises(control.ControlError, match="no step_"):
        control.policy_bench("dispatch", ["A"], fixtures_dir="/d")


@pytest.mark.parametrize("text, expected", [
    ("every=10,max=40", {"every_n_ticks": 10, "max_files": 40}),
    (" max=5 , every=3 ", {"every_n_ticks": 3, "max_files": 5}),
    ("every=7", {"every_n_ticks": 7, "max_files": 40}),
    ("max=2", {"every_n_ticks": 1, "max_files": 2}),
])
def test_parse_capture_spec_accepts_good_specs(text, expected):
    assert parse_capture_spec(text) == expected


@pytest.mark.parametrize("text", [
    "", " ", ",", "every", "every=", "every=0", "every=-1", "every=1.5", "every=ten", "every=²",
    "max=0", "every=1,every=2", "every=1,files=3", "every=1;max=2", "true",
])
def test_parse_capture_spec_rejects_bad_specs_naming_the_format(text):
    """Verified to have teeth: dropping the ``isascii()`` guard let ``every=²`` through
    to ``int()`` (a ValueError, not a PolicyError); dropping the repeat check accepted
    ``every=1,every=2``."""
    with pytest.raises(PolicyError, match="every=N,max=M"):
        parse_capture_spec(text)


def test_sim_main_maps_capture_env_onto_every_assignment_profile(monkeypatch, tmp_path, capsys):
    """``ORSIM_CAPTURE_DISPATCH`` -> ``profile["policy_capture"]`` on every behavior.

    Verified to have teeth: writing the dict only onto the first behavior failed the
    every-profile assertion; dropping ``run_id`` from the directory failed the path
    assertion.
    """
    import apps.simulation.run_container_logistics_simulation as launcher

    coll = {"assignment_0": {"profile": {"strategy": "GreedyNearest"}}, "assignment_1": {}, "x": "skip"}
    monkeypatch.delenv("ORSIM_CAPTURE_DISPATCH", raising=False)
    assert launcher._apply_capture_env_or_exit(coll, str(tmp_path), "run_1") is None
    assert "policy_capture" not in coll["assignment_0"]["profile"] and coll["assignment_1"] == {}

    monkeypatch.setenv("ORSIM_CAPTURE_DISPATCH", "every=10,max=40")
    directory = launcher._apply_capture_env_or_exit(coll, str(tmp_path), "run_1")
    assert directory == str(tmp_path / "fixtures" / "dispatch" / "run_1")
    for key in ("assignment_0", "assignment_1"):
        prof = coll[key]["profile"]
        assert prof["policy_capture"] == {"dir": directory, "every_n_ticks": 10, "max_files": 40,
                                          "run_id": "run_1"}, key
        assert CaptureSettings.from_profile(prof) == CaptureSettings(
            dir=directory, every_n_ticks=10, max_files=40, run_id="run_1")
    assert coll["assignment_0"]["profile"]["strategy"] == "GreedyNearest", "other keys untouched"

    monkeypatch.setenv("ORSIM_CAPTURE_DISPATCH", "every=0")
    with pytest.raises(SystemExit) as excinfo:
        launcher._apply_capture_env_or_exit(coll, str(tmp_path), "run_1")
    assert excinfo.value.code == 2
    assert "every=N,max=M" in capsys.readouterr().out


def test_sim_main_applies_capture_after_the_policy_overrides():
    """The mapping sits in ``main`` next to the policy-override block, before the
    run-start resolvability check (so a capture never runs on an unchecked profile)."""
    import apps.simulation.run_container_logistics_simulation as launcher

    source = inspect.getsource(launcher.main)
    at = source.index("_apply_capture_env_or_exit(")
    assert source.index("_apply_dispatch_overrides_or_exit(") < at < source.index(
        "_assert_policies_resolvable_or_exit("
    )


def _patched_manager(monkeypatch):
    calls = {}

    def fake_run(self, **kwargs):
        calls["kwargs"] = kwargs
        return ActionResult(key="simulation", ok=True, state=ServiceState.RUNNING, message="started")

    monkeypatch.setattr(ServiceManager, "run_simulation_background", fake_run)
    return calls


def test_run_simulation_command_refuses_a_bad_capture_spec_before_spawning(monkeypatch, capsys):
    """Verified to have teeth: disabling the ``capture_error`` refusal in command.py let
    the bad spec reach ``run_simulation_background``."""
    calls = _patched_manager(monkeypatch)
    assert control_command._run(["run-simulation", "--capture-dispatch", "every=0"]) == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False and "every=N,max=M" in payload["message"]
    assert "kwargs" not in calls, "nothing may spawn on a malformed capture spec"

    assert control_command._run(["run-simulation", "--capture-dispatch", "every=10,max=40"]) == 0
    assert calls["kwargs"]["capture_dispatch"] == "every=10,max=40"


def test_manager_ships_capture_dispatch_as_env(monkeypatch):
    """``run_simulation_background(capture_dispatch=...)`` sets ``ORSIM_CAPTURE_DISPATCH``."""
    import openride_control.manager as manager_mod

    seen = {}

    class _Proc:
        returncode = None

        def poll(self):
            return None

    def fake_spawn(extra_env=None, background=True):
        seen["env"] = dict(extra_env or {})
        return _Proc()

    def never(*_a, **_k):
        raise AssertionError("the test must never reach a real stop of the host's simulation")

    mgr = ServiceManager()
    # Safety first: run_simulation_background's error path stops ANY host simulation.
    monkeypatch.setattr(manager_mod, "stop_simulation_process", never)
    monkeypatch.setattr(mgr, "stop_simulation", never)
    monkeypatch.setattr(manager_mod, "spawn_simulation_process", fake_spawn)
    monkeypatch.setattr(manager_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mgr, "is_simulation_running", lambda: False)
    monkeypatch.setattr(mgr, "_ensure_backend_for_simulation", lambda: None)
    monkeypatch.setattr(mgr, "_watch_simulation_process", lambda proc: None)
    assert mgr.run_simulation_background(scenario="s", capture_dispatch=" every=5 ").ok
    assert seen["env"]["ORSIM_CAPTURE_DISPATCH"] == "every=5"
    assert mgr.run_simulation_background(scenario="s").ok
    assert "ORSIM_CAPTURE_DISPATCH" not in seen["env"]


def test_cli_runner_ships_capture_dispatch_as_env():
    from openride.runner import SimulationRunner

    env = SimulationRunner(scenario="s", run_id="r", capture_dispatch="every=2,max=3")._build_env()
    assert env["ORSIM_CAPTURE_DISPATCH"] == "every=2,max=3"
    assert "ORSIM_CAPTURE_DISPATCH" not in SimulationRunner(scenario="s", run_id="r")._build_env()


def test_openride_run_exposes_capture_dispatch():
    args = cli._build_parser().parse_args(["run", "slug", "--capture-dispatch", "every=10,max=40"])
    assert args.capture_dispatch == "every=10,max=40"
    with pytest.raises(control.ControlError) as excinfo:
        control.validate_capture_dispatch("every=0")
    assert excinfo.value.code == "INVALID"
    assert control.validate_capture_dispatch(None) is None
