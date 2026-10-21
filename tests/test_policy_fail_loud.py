"""Locks fail-loud dispatch-policy lookup at every surface (plan §4 P0.6, P0.7).

An unknown dispatch policy name used to be coerced to the default with a warning, so
a typo silently ran a different experiment. It must now be refused at compile time
(``SpecValidationError``), at run start (exit 2 before any agent spawns), by the
per-run ``ORSIM_SOLVER`` override, and agent-side (with the stale-worker hint).
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.container_logistics.datagen.preprocess import SpecValidationError
from apps.container_logistics.policies.dispatch.profile import (
    assert_assignment_profiles_resolvable,
)
from apps.container_logistics.policies.framework import PolicyError, UnknownPolicyError

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO_ROOT / "scenarios" / "collab_smoke_12t" / "spec.json"


def _assignment_collection(strategy):
    profile = {"cooperation": {}} if strategy is None else {"strategy": strategy}
    return {"assignment_0": {"profile": profile}}


def test_compile_rejects_a_typo_in_solver(tmp_path, monkeypatch):
    """A typo'd ``solver`` raises ``SpecValidationError`` naming the registered set.

    Verified to have teeth: removing the ``_assert_dispatch_policy_registered`` call
    from ``compile_spec_to_bundle`` let the typo compile into a bundle.
    """
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(tmp_path / "root"))
    spec = json.loads(SMOKE_SPEC.read_text())
    spec["solver"] = "GreedyNeerest"
    scenario_dir = tmp_path / "root" / spec["slug"]
    scenario_dir.mkdir(parents=True)
    with pytest.raises(SpecValidationError) as excinfo:
        compile_spec_to_bundle(spec, domain="container-logistics-sim",
                               scenario_dir=str(scenario_dir), slug=spec["slug"])
    message = str(excinfo.value)
    assert "GreedyNeerest" in message
    assert "GreedyNearest" in message and "RandomAssignment" in message
    assert not (scenario_dir / "scenario.json").exists(), "no bundle may be written"


def test_run_start_refuses_unknown_strategy_in_profile():
    """The run-start check raises the lookup's error, prefixed with the agent id.

    A missing strategy means the slot default (as ``get_solver`` reads it) and passes.
    Verified to have teeth: replacing the ``get_policy_spec`` call with a no-op made
    the typo pass; dropping ``or default`` made the missing-strategy case raise.
    """
    assert_assignment_profiles_resolvable(_assignment_collection("GreedyNearest"))
    assert_assignment_profiles_resolvable(_assignment_collection(None))
    assert_assignment_profiles_resolvable({})
    with pytest.raises(UnknownPolicyError) as excinfo:
        assert_assignment_profiles_resolvable(_assignment_collection("GreedyNeerest"))
    assert "assignment_0" in str(excinfo.value) and "GreedyNeerest" in str(excinfo.value)
    with pytest.raises(PolicyError, match="assignment_1"):
        assert_assignment_profiles_resolvable({"assignment_1": "not a mapping"})


def test_run_start_wiring_exits_2_before_the_cooperation_stamp(capsys):
    """``main()`` refuses an unresolvable profile with exit code 2, before agents spawn.

    ``main()`` itself needs Kafka and the preflight services, so this drives the helper
    it calls and checks, on ``main``'s source, that the call sits after the overrides
    block and before the cooperation stamp.
    Verified to have teeth: changing ``sys.exit(2)`` to ``sys.exit(1)`` in
    ``_refuse_invalid_policy_config`` failed the exit-code assertion; moving the call
    below the "Ship the EFFECTIVE cooperation" comment failed the ordering assertion.
    """
    import apps.simulation.run_container_logistics_simulation as launcher

    with pytest.raises(SystemExit) as excinfo:
        launcher._assert_policies_resolvable_or_exit(_assignment_collection("GreedyNeerest"))
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert out.startswith("Policy configuration invalid; refusing to start the run: ")
    assert "GreedyNeerest" in out
    launcher._assert_policies_resolvable_or_exit(_assignment_collection("RandomAssignment"))

    source = inspect.getsource(launcher.main)
    check = source.index("_assert_policies_resolvable_or_exit(")
    assert source.index("_apply_dispatch_overrides_or_exit(") < check < source.index(
        "Ship the EFFECTIVE cooperation structure"
    ), "the run-start check must run after the overrides and before the cooperation stamp"


def test_solver_override_unknown_name_raises():
    """``apply_solver_override`` raises on an unknown name and mutates nothing.

    Falsy still means "no override" (``None``). Verified to have teeth: removing the
    ``get_policy_spec`` check from ``apply_solver_override`` failed the ``pytest.raises``.
    """
    from apps.container_logistics.scenario.scenario_overrides import apply_solver_override

    collection = _assignment_collection("RandomAssignment")
    with pytest.raises(UnknownPolicyError, match="GreedyNeerest"):
        apply_solver_override(collection, "GreedyNeerest")
    assert collection["assignment_0"]["profile"]["strategy"] == "RandomAssignment"
    assert apply_solver_override(collection, "") is None
    assert apply_solver_override(collection, None) is None
    assert apply_solver_override(collection, "GreedyNearest") == "GreedyNearest"
    assert collection["assignment_0"]["profile"]["strategy"] == "GreedyNearest"


def test_assignment_app_wraps_unknown_strategy_with_the_celery_restart_hint(monkeypatch):
    """Agent-side, the lookup error keeps its class and gains the stale-worker hint.

    ``ORSimApp.__init__`` (which needs a messenger) is stubbed to only set ``behavior``.
    Verified to have teeth: re-raising the bare lookup error (no suffix) in
    ``AssignmentApp.__init__`` failed the hint assertion.
    """
    from apps.container_logistics.assignment import app as app_module

    def _stub(self, **kwargs):
        self.behavior = kwargs["behavior"]

    monkeypatch.setattr(app_module.ORSimApp, "__init__", _stub)
    with pytest.raises(UnknownPolicyError) as excinfo:
        app_module.AssignmentApp(run_id="r", sim_clock="c",
                                 behavior={"profile": {"strategy": "GreedyNeerest"}},
                                 messenger=None)
    message = str(excinfo.value)
    assert "GreedyNeerest" in message
    assert "systemctl --user restart openride-celery" in message
    assert isinstance(excinfo.value.__cause__, UnknownPolicyError)
    app = app_module.AssignmentApp(run_id="r", sim_clock="c",
                                   behavior={"profile": {}}, messenger=None)
    # Round 2: an empty profile is the slot default tree Partitioned/RandomAssignment.
    assert type(app._solver).__name__ == "Partitioned"
    assert type(app._solver.inner).__name__ == "RandomAssignmentSolver"


def test_profile_module_is_a_leaf():
    """``policies.dispatch.profile`` must import without ``assignment`` (plan §2.7).

    Verified to have teeth: adding ``import apps.container_logistics.assignment`` to the
    top of ``profile.py`` made the subprocess report the leak.
    """
    code = (
        "import sys\n"
        "import apps.container_logistics.policies.dispatch.profile\n"
        "leaked = sorted(m for m in sys.modules if m.startswith('apps.container_logistics.assignment'))\n"
        "print(leaked)\n"
        "sys.exit(1 if leaked else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"profile import pulled in assignment: {result.stdout}{result.stderr}"
