"""T23 — ``openride scenario rules-baseline`` must rewrite ONLY the world block.

The staleness guard's whole value is that moving the baseline forward is an
**explicit human act**. The three tempting ways to weaken it are all wrong in the
same way — recompute the digest at compile, scope the guard to `name` rules, or
drop it — so the sanctioned escape hatch has to be trustworthy in the one way that
matters: it must record the new world and change **nothing else**, above all not
the rules, which are the thing a human is supposed to have reviewed.

**Scope honesty** (same as ``test_cli_cooperation_reset_flag.py``):
``openride/scenario_cmd.py`` imports ``rich``, which is not installed in this venv,
so its argparse surface is asserted against the AST. ``openride/control.py`` has no
such dependency and IS driven for real.
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/home/user")

from openride import control  # importable: no `rich` dependency

_SCENARIO_CMD = Path("/home/user/openride/scenario_cmd.py")


def _tree():
    return ast.parse(_SCENARIO_CMD.read_text())


def _subparser_names(tree):
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser"
                and node.args
                and isinstance(node.args[0], ast.Constant)):
            out.append(node.args[0].value)
    return out


def _flag_targets(tree, flag):
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == flag
                and isinstance(node.func.value, ast.Name)):
            out.append(node.func.value.id)
    return out


# --------------------------------------------------------------------------- #
# the CLI surface exists
# --------------------------------------------------------------------------- #

def test_the_rules_baseline_verb_is_registered():
    names = _subparser_names(_tree())
    assert "rules-baseline" in names, (
        "the guard's only sanctioned escape hatch has no CLI surface — a guard with "
        "no override gets disabled wholesale"
    )
    assert "compile" in names


def test_compile_offers_baseline_rules_for_the_create_then_compile_flow():
    assert "comp" in _flag_targets(_tree(), "--baseline-rules")


def test_reseed_is_not_blocked_for_a_rules_carrying_scenario():
    """§18.1. Facility placement is seeded from a module constant, never from the
    spec seed, so a reseed provably cannot move a facility. A block would forbid a
    safe operation AND teach authors the guard is about seeds when it is about the
    site list — an author who internalises that will confidently change
    ``num_facilities`` expecting protection from a rule that never covered it."""
    source = _SCENARIO_CMD.read_text()
    assert "--reseed" in source
    tree = _tree()
    # `--reseed` must still be registered on `compile` and nothing may have been
    # added that refuses it.
    assert "comp" in _flag_targets(tree, "--reseed")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            if "reseed" in lowered:
                assert not any(
                    bad in lowered
                    for bad in ("cannot reseed", "refus", "not allowed", "blocked")
                ), f"a --reseed block was introduced: {node.value!r}"


# --------------------------------------------------------------------------- #
# T23 — the writer touches only the world block
# --------------------------------------------------------------------------- #

_FLAGSHIP = Path("/home/user/openride_apps/scenarios/rebate_ports_500_trucks")


@pytest.fixture
def scenario_copy(tmp_path):
    if not (_FLAGSHIP / "spec.json").is_file():
        pytest.skip("flagship scenario not present")
    dest = tmp_path / "rebate_ports_500_trucks"
    shutil.copytree(_FLAGSHIP, dest)
    return dest


def test_rules_baseline_rewrites_only_the_world_block(scenario_copy):
    spec_path = scenario_copy / "spec.json"
    before_text = spec_path.read_text()
    before = json.loads(before_text)
    assert before.get("facilityRules"), "fixture: the scenario carries no rules"

    new_world = {
        "site_digest": "blake2b16:deadbeefdeadbeef",
        "facility_count": 320,
        "code_counts": {"CT": 8, "CU": 248, "MT": 64},
        "seed_at_baseline": before.get("seed"),
    }
    control.write_facility_rules_world(str(scenario_copy), new_world)

    after = json.loads(spec_path.read_text())
    assert after["facilityRulesWorld"] == new_world

    # NOTHING else moved — above all not the rules, which are the thing a human is
    # supposed to have reviewed before running this command.
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"facilityRulesWorld"}, f"the writer also changed {changed}"
    assert after["facilityRules"] == before["facilityRules"]

    # And the file's own conventions survive, so a re-baseline never shows up as a
    # whole-file reformat in a diff.
    assert spec_path.read_text().endswith("\n")
    assert json.dumps(after, indent=2, sort_keys=True) + "\n" == spec_path.read_text()


def test_rules_baseline_does_not_recompile(scenario_copy):
    """It records the new world; it does not bless it. Recompiling here would make
    the command read like validation, which is how a guard gets routed around."""
    bundle_path = scenario_copy / "scenario.json"
    before_mtime = bundle_path.stat().st_mtime_ns
    before_size = bundle_path.stat().st_size

    control.write_facility_rules_world(str(scenario_copy), {
        "site_digest": "blake2b16:0011223344556677",
        "facility_count": 300,
        "code_counts": {"CT": 6, "CU": 234, "MT": 60},
    })

    assert bundle_path.stat().st_mtime_ns == before_mtime
    assert bundle_path.stat().st_size == before_size


def test_the_writer_refuses_to_change_anything_but_the_world_block(scenario_copy):
    """The writer's own guard: it diffs old against new and refuses a write that
    would touch a second key. A helper that can quietly rewrite the rules is worse
    than no helper."""
    import inspect

    source = inspect.getsource(control.write_facility_rules_world)
    assert "facilityRulesWorld" in source
    # Structural, not a grep for a comment: there must be a comparison that can
    # raise, not just a docstring promising one.
    tree = ast.parse(source.lstrip())
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert raises, "the writer promises to refuse but has no raise path"


# --------------------------------------------------------------------------- #
# R2-8 (F6) — the flag must not fail on the common case
# --------------------------------------------------------------------------- #

def test_baseline_rules_flag_is_a_noop_on_a_rules_less_scenario():
    """13 of the 15 shipped scenarios author no rules. Turning rc 0 into rc 2 on that
    case trains operators to drop the flag, which is worse than not having it.

    Asserted structurally (the CLI needs `rich`, absent from this venv): the refusal
    must be conditional, and the `compile --baseline-rules` call site must opt out.
    """
    tree = _tree()
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_rules_baseline"),
        None,
    )
    assert fn is not None, "_rules_baseline not found — has it moved?"
    kwonly = {a.arg for a in fn.args.kwonlyargs}
    assert "no_rules_is_error" in kwonly, (
        "_rules_baseline cannot distinguish the standalone verb from "
        "`compile --baseline-rules`, so the common case still fails"
    )

    # The refusal must sit INSIDE a conditional on that flag, not at the top level of
    # the has_rules branch.
    src = _SCENARIO_CMD.read_text()
    assert "if no_rules_is_error:" in src
    assert "nothing to re-baseline" in src

    # ... and the compile call site must actually opt out.
    compile_fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_cmd_compile"),
        None,
    )
    assert compile_fn is not None
    opts = [
        kw.value for call in ast.walk(compile_fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name) and call.func.id == "_rules_baseline"
        for kw in call.keywords if kw.arg == "no_rules_is_error"
    ]
    assert opts and all(
        isinstance(v, ast.Constant) and v.value is False for v in opts
    ), "`compile --baseline-rules` still treats a rules-less scenario as a refusal"


def test_non_zero_stays_reserved_for_a_genuine_refusal():
    """The relaxation must not soften the cases that SHOULD fail closed: `--yes`
    withheld non-interactively, and an aborted confirmation."""
    src = _SCENARIO_CMD.read_text()
    assert "Refusing to re-baseline" in src
    assert "return 2" in src
    assert "Aborted — spec.json untouched." in src
