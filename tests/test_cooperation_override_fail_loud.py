"""The per-run cooperation-structure override fails loud; the run-time sharing override is gone.

What is locked (plan ``plan_contract_final.md`` §7.10, WS7; §5.2 G17, item 34):

- ``scenario_overrides.apply_cooperation_override(collection, structure)`` returns
  ``(applied_id, patched_world_keys)``. An id no structure declares, or a structure on a
  profile with no cooperation block, is **message 26** (``PolicyError`` naming the
  declared ids) instead of a warning followed by a run of the scenario's own arm (the
  base oracle: ``structure-typo`` ran silently as ``pair``). The slug matching (F6) stays.
- ``dispatch.bind.refuse_unread_patches`` (**message 25**): a patched world key that the
  bound planner tree never read at bind is refused, because every value of it runs the
  same experiment. Exercised with an in-test leaf that reads nothing, under
  ``isolated_registry``; the four built-ins all read ``cooperation`` on a cooperative bundle.
- The refusals reach every entry point before anything spawns: the sim main (exit 2),
  ``validate-run-policies --cooperation-structure`` (the dashboard's synchronous dry run),
  ``run-simulation`` and ``run-preflight``.
- ``ORSIM_SHARING_POLICY`` / ``--sharing-policy`` are refused with the reason
  (``planner.sharing`` is read only at compile time, so the override was inert).
"""

from __future__ import annotations

import argparse
import copy
import inspect
import json
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.bind import bind_profile, refuse_unread_patches
from apps.container_logistics.policies.dispatch.profile import assert_assignment_profiles_resolvable
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, policy
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.scenario.scenario_overrides import apply_cooperation_override
from openride_control import command
from openride_control.policies import run_preflight, validate_run_policy_args
from tests.test_legacy_run_env_overrides import _run_name, smoke_assignment  # noqa: F401 (fixture)
from tests.test_policy_run_overrides import _compiled_smoke_scenario

BUILTINS = ("GreedyNearest", "RandomAssignment", "Partitioned", "PooledMarket")


def _collection(cooperation=None):
    profile = {"strategy": "GreedyNearest"}
    if cooperation is not None:
        profile["cooperation"] = cooperation
    return {"assignment_main": {"profile": profile}}


def _coop():
    return {"active": "pair", "structures": [{"id": "no-coop"}, {"id": "port-alliance"}, {"id": "pair"}]}


def _payload(argv, capsys):
    rc = command._run(argv)
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def non_reader():
    """``NonReader``: a planner whose ``bind`` reads no world key (so no ``cooperation``)."""
    with isolated_registry():
        @policy(slot="planner", name="NonReader", version="1", description="reads no world key")
        class NonReader(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def solve_tick(self, tick, params):
                return Result(pairs=())

        yield NonReader


# --- message 26: apply_cooperation_override --------------------------------------------


def test_unknown_structure_is_refused_naming_the_declared_ids_and_mutates_nothing():
    """Verified to have teeth: restoring the old ``logging.warning`` branch for an
    undeclared id let the call return and left ``active`` at ``pair``."""
    collection = _collection(_coop())
    before = copy.deepcopy(collection)
    with pytest.raises(PolicyError, match=r"'pair-typo' is not declared .*no-coop, port-alliance, pair"):
        apply_cooperation_override(collection, "pair-typo")
    assert collection == before


def test_a_structure_on_a_scenario_without_a_cooperation_block_is_refused():
    with pytest.raises(PolicyError, match="has no cooperation block"):
        apply_cooperation_override(_collection(), "pair")
    with pytest.raises(PolicyError, match="has no cooperation block"):
        apply_cooperation_override(_collection(cooperation="not a block"), "pair")


def test_slug_matching_is_kept_and_the_patched_key_is_returned():
    collection = _collection(_coop())
    assert apply_cooperation_override(collection, "Port Alliance") == ("port-alliance", frozenset({"cooperation"}))
    assert collection["assignment_main"]["profile"]["cooperation"]["active"] == "port-alliance"
    assert apply_cooperation_override(collection, "  NO_COOP ") == ("no-coop", frozenset({"cooperation"}))


def test_no_structure_changes_nothing_and_needs_no_block():
    collection = _collection()
    assert apply_cooperation_override(collection, None) == (None, frozenset())
    assert apply_cooperation_override(collection, "") == (None, frozenset())
    assert collection == _collection()


def test_the_sharing_parameter_is_removed():
    with pytest.raises(TypeError):
        apply_cooperation_override(_collection(_coop()), sharing="transfer-when-cheaper")  # noqa


# --- message 25: refuse_unread_patches -------------------------------------------------


def test_a_tree_that_reads_no_cooperation_refuses_a_structure_patch(non_reader):
    """Verified to have teeth: making ``refuse_unread_patches`` return unconditionally
    failed both ``raises`` blocks."""
    profile = {"policies": {"planner": {"type": "NonReader"}}, "policies_format": 2, "cooperation": _coop()}
    tree = bind_profile(profile)
    assert "cooperation" not in tree.world_reads
    refuse_unread_patches(tree, frozenset())
    with pytest.raises(PolicyError, match=r"patched world key\(s\) 'cooperation'.*NonReader"):
        refuse_unread_patches(tree, frozenset({"cooperation"}))
    collection = {"assignment_main": {"profile": profile}}
    assert set(assert_assignment_profiles_resolvable(collection)) == {"assignment_main"}
    with pytest.raises(PolicyError, match=r"^assignment agent 'assignment_main': a per-run override patched"):
        assert_assignment_profiles_resolvable(collection, patched_world_keys={"cooperation"})


@pytest.mark.parametrize("name", BUILTINS)
def test_every_builtin_reads_cooperation_so_a_structure_patch_passes(name, tmp_path, monkeypatch):
    slug = _compiled_smoke_scenario(tmp_path, monkeypatch)
    payload = run_preflight(slug, policies=[f"planner={name}"], cooperation_structure="no-coop")
    assert payload["ok"] is True, payload
    assert "cooperation" in payload["world_reads"], payload
    assert payload["structures"] == {"active": "no-coop", "declared": ["no-coop", "pair"]}


# --- the control plane: validate-run-policies, run-preflight, run-simulation ----------


def test_validate_run_policies_refuses_a_typo_and_an_unread_structure(tmp_path, monkeypatch, capsys,
                                                                       non_reader):
    """The dashboard's synchronous dry run (HTTP 400 on ``ok: false``).

    Verified to have teeth: dropping ``cooperation_structure`` from
    ``validate_run_policy_args``' dry run returned ``ok: true`` for the typo."""
    slug = _compiled_smoke_scenario(tmp_path, monkeypatch)
    rc, payload = _payload(["validate-run-policies", "--scenario", slug,
                            "--cooperation-structure", "pare"], capsys)
    assert rc == 1 and payload["ok"] is False and payload["action"] == "validate_run_policies"
    assert "'pare' is not declared" in payload["message"] and "no-coop, pair" in payload["message"]
    rc, payload = _payload(["validate-run-policies", "--scenario", slug,
                            "--cooperation-structure", "No Coop"], capsys)
    assert rc == 0 and payload["ok"] is True and payload["overrides"] == {}, payload
    msg = validate_run_policy_args(slug, None, ["planner=NonReader"], [], cooperation_structure="pair")
    assert isinstance(msg, str) and "patched world key(s) 'cooperation'" in msg, msg
    # Names are still checked before the bundle: a structure does not change that order.
    msg = validate_run_policy_args("nope", "Nope", [], [], cooperation_structure="pair")
    assert msg.startswith("--solver 'Nope': Unknown planner policy 'Nope'"), msg
    assert validate_run_policy_args("nope", None, [], [], cooperation_structure="pair").startswith(
        "scenario 'nope' not found")


def test_run_preflight_reports_world_reads_and_structures(tmp_path, monkeypatch, capsys, non_reader):
    slug = _compiled_smoke_scenario(tmp_path, monkeypatch)
    rc, payload = _payload(["run-preflight", "--scenario", slug], capsys)
    assert rc == 0, payload
    assert set(payload) == {"ok", "action", "world_reads", "structures", "commandId"}
    assert payload["action"] == "run_preflight" and "cooperation" in payload["world_reads"]
    assert payload["world_reads"] == sorted(payload["world_reads"])
    assert payload["structures"] == {"active": "pair", "declared": ["no-coop", "pair"]}
    # A tree that reads no cooperation: ok, and world_reads says so (the dropdown gate).
    rc, payload = _payload(["run-preflight", "--scenario", slug, "--policy", "planner=NonReader"], capsys)
    assert rc == 0 and payload["ok"] is True and "cooperation" not in payload["world_reads"], payload
    rc, payload = _payload(["run-preflight", "--scenario", slug, "--policy", "planner=NonReader",
                            "--cooperation-structure", "pair"], capsys)
    assert rc == 1 and payload == {"ok": False, "action": "run_preflight", "message": payload["message"],
                                   "commandId": payload["commandId"]}
    assert "patched world key(s) 'cooperation'" in payload["message"]
    rc, payload = _payload(["run-preflight", "--scenario", "nope"], capsys)
    assert rc == 1 and payload["ok"] is False and "not found" in payload["message"]


def test_run_simulation_refuses_a_typo_and_the_sharing_flag_without_spawning(tmp_path, monkeypatch, capsys):
    from openride_control.manager import ServiceManager

    slug = _compiled_smoke_scenario(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr(ServiceManager, "run_simulation_background", lambda self, **kw: spawned.append(kw))
    rc, payload = _payload(["run-simulation", "--scenario", slug, "--cooperation-structure", "pare"], capsys)
    assert rc == 1 and not spawned and "'pare' is not declared" in payload["message"]
    rc, payload = _payload(["run-simulation", "--scenario", slug, "--sharing-policy", "x"], capsys)
    assert rc == 1 and not spawned
    assert payload["message"].startswith("--sharing-policy removed: inert at run time")


def test_manager_and_runner_no_longer_carry_a_sharing_policy():
    from openride.runner import SimulationRunner
    from openride_control.manager import ServiceManager

    assert "sharing_policy" not in inspect.signature(ServiceManager.run_simulation_background).parameters
    assert "sharing_policy" not in inspect.signature(SimulationRunner).parameters


def test_openride_run_refuses_the_sharing_flag():
    from openride import run_cmd, run_group

    parser = argparse.ArgumentParser()
    run_group.register(parser.add_subparsers())
    args = parser.parse_args(["run", "--scenario", "s", "--sharing-policy", "x", "--json"])
    assert run_cmd.run_vanilla(args, {"s"}) == run_cmd.EXIT_BAD_ARGS


# --- the sim main: exit 2 before any agent spawns ------------------------------------


@pytest.mark.parametrize("env, needle", [
    ({"ORSIM_COOP_STRUCTURE": "pare"}, "'pare' is not declared"),
    ({"ORSIM_COOP_STRUCTURE": "pair", "ORSIM_POLICY_OVERRIDES": json.dumps({"planner": {"type": "NonReader"}})},
     "patched world key(s) 'cooperation'"),
    ({"ORSIM_SHARING_POLICY": "transfer-when-cheaper"},
     "set planner.sharing in spec.json and recompile; the run-time override was inert"),
], ids=["typo", "unread", "sharing"])
def test_sim_main_exits_2(env, needle, monkeypatch, tmp_path, smoke_assignment, capsys, non_reader):  # noqa: F811
    """Verified to have teeth: calling ``apply_cooperation_override`` without the
    ``_or_exit`` wrapper surfaced the typo as a traceback (PolicyError), not exit 2."""
    with pytest.raises(SystemExit) as stop:
        _run_name(monkeypatch, tmp_path, smoke_assignment, env)
    assert stop.value.code == 2
    out = capsys.readouterr().out
    assert "Policy configuration invalid; refusing to start the run:" in out and needle in out, out


def test_sim_main_keeps_the_structure_segment_on_a_valid_override(monkeypatch, tmp_path,
                                                                  smoke_assignment):  # noqa: F811
    """G10: the override path's ``· <id>`` segment, byte-identical position (the slug)."""
    assert _run_name(monkeypatch, tmp_path, smoke_assignment, {"ORSIM_COOP_STRUCTURE": "No Coop"}) == (
        "base · no-coop · lifecycle:agents")
