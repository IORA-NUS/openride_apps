"""Day-plan solve runner: child process, limits, typed errors (plan §4 ``runner.py``; addendum B2, M2, minor 8).

The crash / timeout / refusal paths use stand-in children (``child_args``), so no OR-Tools
is needed; the greedy end-to-end run uses the REAL child under this interpreter (the
greedy backend imports no OR-Tools).
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from apps.container_logistics.policies.solver_lib.day_plan import runner
from apps.container_logistics.policies.solver_lib.day_plan.instance import build_inputs
from apps.container_logistics.policies.solver_lib.day_plan.time_model import TimeModel

from tests._day_plan_fixtures import STEP_S, synthetic_day

TM = TimeModel.default()


def _inputs(n_orders=60, n_trucks=10, seed=11):
    trucks, orders = synthetic_day(seed, n_orders=n_orders, n_trucks=n_trucks)
    return build_inputs(trucks, {}, orders, None, TM, None, now_s=0.0, day=0, window_end_s=86400.0,
                        step_seconds=STEP_S, cooperation_arm={"acme": ["cargo"], "cargo": ["acme"]},
                        working_hours=None, max_wait_steps=360, cadence_steps=6)


def _stand_in(code: str) -> list[str]:
    return ["-c", code]


def test_greedy_end_to_end_through_the_real_child_without_ortools():
    inp = _inputs()
    out = runner.solve(inp, backend="greedy", time_limit_s=5)
    assert runner.validate_output(inp, out) == []
    st = out["stats"]
    assert st["backends"] == {"greedy": len(st["components"])} and st["deterministic"] is True
    assert st["green"] is True and st["wall_ms"] > 0
    planned = sum(len(c) for c in out["itineraries"].values())
    assert planned > 0 and planned + len(out["unplanned"]) == len(inp["jobs"])
    # deterministic: the same inputs give the same answer
    again = runner.solve(json.loads(json.dumps(inp)), backend="greedy", time_limit_s=5)
    assert again["itineraries"] == out["itineraries"] and again["unplanned"] == out["unplanned"]


def test_crash_is_typed_with_the_stderr_tail():
    with pytest.raises(runner.SolveCrashed) as ei:
        runner.solve(_inputs(), child_args=_stand_in("import sys; sys.stderr.write('boom-xyz'); sys.exit(1)"))
    assert ei.value.reason == "crashed" and ei.value.returncode == 1 and "boom-xyz" in ei.value.stderr


def test_out_of_memory_under_the_address_space_cap():
    code = "x = bytearray(1024 * 1024 * 1024)"
    with pytest.raises(runner.SolveCrashed) as ei:
        runner.solve(_inputs(), child_args=_stand_in(code), memory_cap_bytes=256 * 1024 * 1024)
    assert ei.value.reason == "out_of_memory"


def test_child_exit_code_3_is_out_of_memory():
    with pytest.raises(runner.SolveCrashed) as ei:
        runner.solve(_inputs(), child_args=_stand_in("import sys; sys.exit(3)"))
    assert ei.value.reason == "out_of_memory"


def test_timeout_kills_the_child_promptly():
    t0 = time.monotonic()
    with pytest.raises(runner.SolveTimeout) as ei:
        runner.solve(_inputs(), child_args=_stand_in("import time; time.sleep(60)"), timeout_s=1.0)
    assert time.monotonic() - t0 < 15
    assert ei.value.reason == "timeout" and ei.value.elapsed_s >= 1.0


def test_refusal_is_solve_rejected_with_the_child_reason():
    code = ("import json, sys; json.load(sys.stdin); "
            "print(json.dumps({'ok': False, 'reason': 'poor_incumbent', 'stats': {'x': 1}}))")
    with pytest.raises(runner.SolveRejected) as ei:
        runner.solve(_inputs(), child_args=_stand_in(code))
    assert ei.value.reason == "poor_incumbent" and ei.value.stats["x"] == 1


@pytest.mark.parametrize("answer, problem", [
    ({"ok": True, "itineraries": {"nope": []}, "unplanned": None}, "unknown truck"),
    ("dup", "on trucks"),
    ({"ok": True, "itineraries": {}, "unplanned": []}, "unplanned is not exactly"),
])
def test_invalid_answers_are_rejected(answer, problem):
    inp = _inputs()
    if answer == "dup":
        t = [x["id"] for x in inp["trucks"]]
        j = inp["jobs"][0]["id"]
        answer = {"ok": True, "itineraries": {t[0]: [j], t[1]: [j]},
                  "unplanned": sorted(x["id"] for x in inp["jobs"][1:])}
    code = f"import json, sys; json.load(sys.stdin); sys.stdout.write({json.dumps(answer)!r})"
    with pytest.raises(runner.SolveRejected) as ei:
        runner.solve(inp, child_args=_stand_in(code))
    assert ei.value.reason == "invalid_output" and problem in str(ei.value)


@pytest.mark.parametrize("code", [
    "import sys; sys.stdin.read(); print('not json')",
    "import sys; sys.stdin.read(); print('[1, 2]')",
])
def test_unreadable_answers_are_crashes(code):
    with pytest.raises(runner.SolveCrashed):
        runner.solve(_inputs(), child_args=_stand_in(code))


def test_the_child_receives_the_request_on_stdin():
    code = ("import json, sys; req = json.load(sys.stdin); "
            "print(json.dumps({'ok': False, 'reason': req['backend'] + ':' + str(req['time_limit_s']) "
            "+ ':' + str(len(req['inputs']['jobs']))}))")
    inp = _inputs()
    with pytest.raises(runner.SolveRejected) as ei:
        runner.solve(inp, backend="mip", time_limit_s=7.5, child_args=_stand_in(code))
    assert ei.value.reason == f"mip:7.5:{len(inp['jobs'])}"


def test_waiting_yields_to_other_greenlets():
    eventlet = pytest.importorskip("eventlet")
    ticks = []

    def ticker():
        for _ in range(50):
            ticks.append(time.monotonic())
            eventlet.sleep(0.02)

    g = eventlet.spawn(ticker)
    code = "import sys, time, json; sys.stdin.read(); time.sleep(1.0); print(json.dumps({'ok': False, 'reason': 'r'}))"
    with pytest.raises(runner.SolveRejected):
        runner.solve(_inputs(), child_args=_stand_in(code))
    g.wait()
    # the ticker kept running while the child slept (a blocking wait would starve it)
    assert len([t for t in ticks if t < ticks[0] + 1.0]) >= 10


def test_child_uses_this_interpreter_and_the_repo_on_its_path():
    code = ("import json, sys, os; sys.stdin.read(); "
            "print(json.dumps({'ok': False, 'reason': sys.executable + '|' + os.environ['PYTHONPATH'].split(os.pathsep)[0]}))")
    with pytest.raises(runner.SolveRejected) as ei:
        runner.solve(_inputs(), child_args=_stand_in(code))
    exe, root = ei.value.reason.split("|")
    assert exe == sys.executable and root == runner._REPO_ROOT
