"""The `--allow-cooperation-reset` escape hatch must be reachable from the CLI.

Plan §14.4 R3-8 / review HIGH-3. The guard, its typed exception, its control-plane
code and 16 tests all existed — but **no CLI surface exposed the opt-in**, so from
`openride scenario new/edit` the refusal was unrecoverable. A guard with no override
gets disabled wholesale, which is what this prevents.

These are CLI-LEVEL assertions on purpose: the 16 existing guard tests never touch
argparse, which is exactly how the gap survived.

**Scope honesty:** `openride/scenario_cmd.py` imports `rich`, which is not installed
in this venv (the CLI runs under `pyjupenv`), so it cannot be imported here. The
assertions on it are therefore made against its **AST** — structural, not a substring
grep, but still not an execution of argparse. `openride/control.py` has no such
dependency and IS driven for real below.
"""

import ast
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openride import control  # importable: no `rich` dependency

_SCENARIO_CMD = Path(__file__).resolve().parents[1] / "openride" / "scenario_cmd.py"
_FLAG = "--allow-cooperation-reset"
_DEST = "allow_cooperation_reset"


def _tree():
    return ast.parse(_SCENARIO_CMD.read_text())


def _add_argument_targets_for_flag(tree):
    """Every `<parser>.add_argument("--allow-cooperation-reset", ...)` receiver."""
    targets = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == _FLAG
                and isinstance(node.func.value, ast.Name)):
            targets.append(node.func.value.id)
    return targets


def test_cli_exposes_allow_cooperation_reset():
    """The flag must be registered on BOTH authoring subcommands."""
    targets = _add_argument_targets_for_flag(_tree())
    assert "new" in targets, "`scenario new` cannot opt in to a cooperation reset"
    assert "edit" in targets, "`scenario edit` cannot opt in to a cooperation reset"


def test_cli_flag_is_a_store_true_switch():
    for node in ast.walk(_tree()):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == _FLAG):
            actions = [kw.value.value for kw in node.keywords
                       if kw.arg == "action" and isinstance(kw.value, ast.Constant)]
            assert actions == ["store_true"], f"unexpected action for {_FLAG}: {actions}"


def test_the_parsed_flag_is_forwarded_by_both_command_handlers():
    """A parsed-but-ignored flag is decorative. Both handlers must pass it on."""
    tree = _tree()
    forwarding = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == _DEST:
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "?")
                    forwarding.append(name)
    assert "edit_scenario" in forwarding, "`scenario edit` parses the flag but drops it"
    assert "_create_scenario" in forwarding, "`scenario new` parses the flag but drops it"
    assert "generate_scenario" in forwarding, "_create_scenario does not forward the flag"


# --- control.py is importable, so drive it for real --------------------------

def test_control_wrappers_accept_the_flag():
    for fn in (control.generate_scenario, control.edit_scenario):
        params = inspect.signature(fn).parameters
        assert _DEST in params, f"{fn.__name__} cannot forward the opt-in"
        assert params[_DEST].default is False, f"{fn.__name__} defaults to permitting a reset"


@pytest.mark.parametrize("fn_name", ["generate_scenario", "edit_scenario"])
def test_control_wrapper_emits_the_flag_only_when_asked(monkeypatch, fn_name):
    """Forwarded as an actual argv flag, and absent by default."""
    seen = {}

    def _fake(args, **kw):
        seen["args"] = list(args)
        return {"ok": True, "scenario": {"slug": "x"}}

    monkeypatch.setattr(control, "_run_command", _fake)
    fn = getattr(control, fn_name)
    call = (lambda **kw: fn({"name": "n"}, **kw)) if fn_name == "generate_scenario" \
        else (lambda **kw: fn("slug", {"name": "n"}, **kw))

    call(allow_cooperation_reset=True)
    assert _FLAG in seen["args"], f"{fn_name} swallowed the opt-in"

    call()
    assert _FLAG not in seen["args"], f"{fn_name} permits a reset by default"


def test_control_error_carries_the_machine_readable_code():
    """The UI/CLI must branch on the code, never on the message text."""
    err = control.ControlError("boom")
    assert hasattr(err, "code")
    assert err.code is None
