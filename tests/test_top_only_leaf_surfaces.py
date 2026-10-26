"""Every "per-scope solver" listing omits a ``top_only`` planner (NetworkFlowDay build, WS-D2).

A ``top_only`` policy (one that plans whole days) is never a leaf: not a ``--solver``
value, not an inner, not a legacy strategy. WS-A put the rule in one place
(``framework.leaf_policies`` / the catalog's ``top_only`` flag); these tests pin every
CLI / scenario / control-plane surface that enumerates leaves to it:

- ``scenario_overrides.known_solvers`` and message 11's leaf list (``_checked_solver``);
- ``frontend_scenario_spec.known_solvers`` (the ``known-solvers`` control command, the
  dashboard and ``openride run``'s solver picker) and ``normalize_solver``;
- ``openride solver list`` (text + JSON), ``policy show``'s ref bounds and leaf line;
- ``openride scenario new/edit --solver`` (``scenario_cmd._with_solver``) refuses it (R5).

The toy (``tests/planning_toy.py``) is registered only inside ``isolated_registry()``.
With no ``top_only`` registered every surface is byte-identical to the composite-only
filter it replaced.
"""

from __future__ import annotations

import json

import pytest

from apps.container_logistics.policies.framework import PolicyError, list_policies
from apps.container_logistics.policies.framework.catalog import component_catalog, policy_catalog
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.scenario import frontend_scenario_spec as fss
from apps.container_logistics.scenario import scenario_overrides as so
from openride import cli, control, policy_cmd, scenario_cmd
from tests.planning_toy import register_toys

TOY = "ToyDayPlan"


@pytest.fixture
def toys():
    with isolated_registry():
        register_toys()
        yield


def _old_leaf_names() -> list[str]:
    """The pre-WS-D2 filter: every non-composite planner policy."""
    return [s.name for s in list_policies("planner") if not s.is_composite]


def _real_catalogs(monkeypatch, *, strip_top_only: bool = False):
    from openride.ui import console

    slots = policy_catalog()
    if strip_top_only:
        slots = {k: {**v, "policies": [{q: w for q, w in p.items() if q != "top_only"}
                                       for p in v.get("policies") or []]}
                 for k, v in slots.items()}
    monkeypatch.setattr(control, "known_policy_slots", lambda: slots)
    monkeypatch.setattr(control, "known_policy_components", component_catalog)
    monkeypatch.setattr(console, "width", 240)


def _out(capsys, argv) -> str:
    assert cli.main(argv) == 0
    return capsys.readouterr().out


# --- with a top_only toy registered: every surface omits it -----------------------------

def test_scenario_overrides_known_solvers_omits_top_only(toys):
    names = so.known_solvers()
    assert TOY not in names and "ToyPlanNotTopOnly" in names and "GreedyNearest" in names


def test_message_11_leaf_list_omits_top_only(toys):
    with pytest.raises(PolicyError) as exc:
        so._checked_solver("Partitioned")
    msg = str(exc.value)
    assert "names a decomposition" in msg and "GreedyNearest" in msg and TOY not in msg


def test_checked_solver_still_refuses_top_only_r5(toys):
    with pytest.raises(PolicyError, match="top_only"):
        so._checked_solver(TOY)


def test_frontend_known_solvers_and_normalize_omit_top_only(toys):
    assert TOY not in fss.known_solvers()["solvers"]
    assert "GreedyNearest" in fss.known_solvers()["solvers"]
    assert fss.normalize_solver(TOY) == fss.LEGACY_LEAF_DEFAULT
    assert fss.normalize_solver("GreedyNearest") == "GreedyNearest"


def test_solver_list_omits_top_only_text_and_json(toys, monkeypatch, capsys):
    _real_catalogs(monkeypatch)
    full = json.loads(_out(capsys, ["policy", "list", "planner", "--json"]))
    assert TOY in [p["name"] for p in full["planner"]["policies"]], "policy list still shows it"
    leaves = json.loads(_out(capsys, ["solver", "list", "--json"]))
    assert TOY not in [p["name"] for p in leaves["planner"]["policies"]]
    assert TOY not in _out(capsys, ["solver", "list"])


def test_ref_targets_leaf_only_omit_top_only(toys):
    slots = policy_catalog()
    leaf = policy_cmd._ref_targets("planner", slots, {}, True)
    assert TOY not in leaf and "GreedyNearest" in leaf
    assert TOY in policy_cmd._ref_targets("planner", slots, {}, False)


def test_policy_show_bounds_and_leaf_line(toys, monkeypatch, capsys):
    _real_catalogs(monkeypatch)
    text = _out(capsys, ["policy", "show", "planner", "Partitioned"])
    bounds = next(line for line in text.splitlines() if "ref → planner (leaf)" in line)
    assert "GreedyNearest" in bounds and TOY not in bounds
    toy = _out(capsys, ["policy", "show", "planner", TOY])
    assert "top only" in toy and "--solver accepts it" not in toy


def test_scenario_cmd_with_solver_refuses_top_only(toys):
    with pytest.raises(ValueError, match="top_only"):
        scenario_cmd._with_solver({}, TOY, {})


# --- with no top_only registered: byte-identical to the old filter ------------------------

@pytest.fixture
def no_top_only():
    with isolated_registry():
        if any(s.top_only for s in list_policies("planner")):
            pytest.skip("a shipped top_only planner exists; the baseline is the toy-free registry")
        yield


def test_default_leaf_lists_unchanged(no_top_only):
    old = _old_leaf_names()
    assert list(so.known_solvers()) == old
    assert fss.known_solvers()["solvers"] == old
    assert policy_cmd._ref_targets("planner", policy_catalog(), {}, True) == old
    with pytest.raises(PolicyError) as exc:
        so._checked_solver("Partitioned")
    assert f"sets the inner solver ({', '.join(old)})" in str(exc.value)


def test_default_solver_list_json_names_unchanged(no_top_only, monkeypatch, capsys):
    _real_catalogs(monkeypatch)
    leaves = json.loads(_out(capsys, ["solver", "list", "--json"]))
    assert [p["name"] for p in leaves["planner"]["policies"]] == _old_leaf_names()


@pytest.mark.parametrize("argv", [
    ["solver", "list"],
    ["policy", "show", "planner", "Partitioned"],
    ["policy", "show", "planner", "GreedyNearest"],
])
def test_default_cli_output_ignores_the_top_only_key(no_top_only, monkeypatch, capsys, argv):
    """With every flag false, the rendered text equals what a catalog without the key renders
    (the ``--json`` dump passes catalog entries through verbatim, key included: its names
    are pinned by ``test_default_solver_list_json_names_unchanged``)."""
    _real_catalogs(monkeypatch)
    with_key = _out(capsys, argv)
    _real_catalogs(monkeypatch, strip_top_only=True)
    assert _out(capsys, argv) == with_key
