"""
Planning hold (OpenRideScheduler concern 5, NetworkFlowDay F2).

An agent announces ``{"action": "hold", "hold_seconds": H}`` on the reply topic
before a step that will outlast STEP_TIMEOUT. These tests drive the real
``step()`` with a mocked messenger whose publish schedules the agents' replies on
the event loop (the same ``_handle_reply`` the paho thread calls), using short
real timeouts so the whole module runs in a few seconds.
"""
import asyncio
import logging
import time
import unittest.mock as mock

import pytest

import apps.simulation.openride_scheduler as sched_mod
from apps.simulation.openride_scheduler import OpenRideScheduler


def _settings(step_timeout=0.3, cap=None, tolerance=0.0, settle=2):
    s = {
        "SIMULATION_LENGTH_IN_STEPS": 100,
        "STEP_TIMEOUT": step_timeout,
        "STEP_TIMEOUT_TOLERANCE": tolerance,
        "STEP_SETTLE_SECONDS": settle,
    }
    if cap is not None:
        s["PLANNING"] = {"max_hold_seconds": cap}
    return s


def _make(settings, agents=("a0", "a1", "a2"), time_=0):
    with mock.patch("orsim.core.orsim_scheduler.ORSimEnv") as env:
        env.messenger_settings = {"MQTT_BROKER": "localhost"}
        env.validate_orsim_settings = lambda s: s
        with mock.patch("orsim.core.orsim_scheduler.Messenger"):
            sched = OpenRideScheduler(run_id="run", scheduler_id="service", orsim_settings=settings)
    sched.time = time_
    sched.agent_messenger = mock.Mock()
    sched.agent_collection = {
        aid: {"pending_boot": False,
              "step_response": {time_: {"reaction": "waiting", "did_step": False, "run_time": 0}}}
        for aid in agents
    }
    return sched


_CURRENT = object()


def _payload(sched, aid, action="completed", step=_CURRENT, **extra):
    p = {"agent_id": aid, "time_step": sched.time if step is _CURRENT else step,
         "action": action, "did_step": action == "completed", "run_time": 0.01}
    p.update(extra)
    return p


def _hold(sched, aid, seconds, step=_CURRENT):
    return _payload(sched, aid, "hold", step=step, hold_seconds=seconds, reason="planning")


def _run_step(sched, script):
    """Run one step(); ``script`` = [(delay_s, payload_factory)] fired after the step publish."""
    async def go():
        loop = asyncio.get_running_loop()

        def on_publish(topic, message):
            if topic.endswith("/ORSimAgent"):
                for delay, make in script:
                    loop.call_later(delay, lambda m=make: sched._handle_reply(m()))

        sched.agent_messenger.client.publish.side_effect = on_publish
        t0 = time.monotonic()
        await sched.step(is_final=False)
        return time.monotonic() - t0

    return asyncio.run(go())


def _quick(sched, *aids):
    return [(0.0, lambda a=a: _payload(sched, a)) for a in aids]


# ── the hold extends the barrier ─────────────────────────────────────────────

def test_hold_extends_past_step_timeout_and_settle():
    # Tolerance 1/3 with a short settle would prune a2 at ~0.05 s; STEP_TIMEOUT at 0.3 s.
    sched = _make(_settings(step_timeout=0.3, cap=5, tolerance=0.5, settle=0.05))
    script = _quick(sched, "a0", "a1") + [
        (0.0, lambda: _hold(sched, "a2", 0.5)),
        (0.6, lambda: _payload(sched, "a2", step=0)),
    ]
    wall = _run_step(sched, script)
    assert wall >= 0.55
    assert "a2" in sched.agent_collection and sched._pruned_stash == {}
    stat = sched.agent_stat[0]
    assert stat["completed"] == 3 and stat["holds"] == 1 and stat["holds_expired"] == 0
    assert stat["hold_seconds_max"] == pytest.approx(0.5, abs=0.01)
    assert 500 <= sched.last_step_hold_ms <= wall * 1000 + 1
    assert stat["hold_ms"] == sched.last_step_hold_ms
    assert sched._holds == {}


def test_completed_releases_the_hold_and_the_step_ends_at_once():
    sched = _make(_settings(step_timeout=0.3, cap=10))
    script = _quick(sched, "a0", "a1") + [
        (0.0, lambda: _hold(sched, "a2", 8)),
        (0.1, lambda: _payload(sched, "a2")),
    ]
    wall = _run_step(sched, script)
    assert wall < 0.5
    assert sched._holds == {}
    assert "a2" in sched.agent_collection
    assert 50 <= sched.last_step_hold_ms <= 400


def test_plan_then_dispatch_reply_after_hold_but_inside_grace_is_not_pruned(caplog):
    # Hold 0.2 s; the reply lands at 0.45 s: after the hold, before hold + STEP_TIMEOUT (0.6 s).
    sched = _make(_settings(step_timeout=0.4, cap=5, tolerance=0.5, settle=0.01))
    script = _quick(sched, "a0", "a1") + [
        (0.0, lambda: _hold(sched, "a2", 0.2)),
        (0.45, lambda: _payload(sched, "a2")),
    ]
    with caplog.at_level(logging.INFO):
        _run_step(sched, script)
    assert "a2" in sched.agent_collection and sched._pruned_stash == {}
    assert sched.agent_stat[0]["holds_expired"] == 0
    assert "PLANNING HOLD EXPIRED" not in caplog.text


# ── expiry ───────────────────────────────────────────────────────────────────

def test_expired_hold_logs_prunes_and_late_completed_reinstates(caplog):
    sched = _make(_settings(step_timeout=0.3, cap=5))
    script = _quick(sched, "a0", "a1") + [(0.0, lambda: _hold(sched, "a2", 0.2))]
    with caplog.at_level(logging.INFO):
        wall = _run_step(sched, script)
    # Expires at hold (0.2) + STEP_TIMEOUT (0.3), not at the plain STEP_TIMEOUT.
    assert 0.45 <= wall < 1.5
    errors = [r for r in caplog.records if r.levelno == logging.ERROR
              and r.getMessage().startswith("PLANNING HOLD EXPIRED: a2 held step 0 for ")]
    assert len(errors) == 1
    assert "(cap 5.0 s); continuing without it" in errors[0].getMessage()
    assert "a2" not in sched.agent_collection and "a2" in sched._pruned_stash
    assert sched.agent_stat[0]["holds_expired"] == 1
    assert sched._holds == {}

    # The late answer for step 0 lands during step 1 and re-registers the agent.
    late = _quick(sched, "a0", "a1") + [(0.0, lambda: _payload(sched, "a2", step=0))]
    _run_step(sched, late)
    assert "a2" in sched.agent_collection and sched._pruned_stash == {}


def test_hold_seconds_are_capped(caplog):
    sched = _make(_settings(step_timeout=0.2, cap=0.2))
    script = _quick(sched, "a0", "a1") + [(0.0, lambda: _hold(sched, "a2", 100))]
    with caplog.at_level(logging.ERROR):
        wall = _run_step(sched, script)
    assert wall < 1.0                       # 0.2 cap + 0.2 grace, not 100 s
    assert sched.agent_stat[0]["hold_seconds_max"] == pytest.approx(0.2, abs=0.01)
    assert "PLANNING HOLD EXPIRED: a2" in caplog.text


def test_renewed_hold_never_extends_past_cap_from_the_first():
    sched = _make(_settings(step_timeout=30, cap=1))
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "a2", 0.5))
    first = dict(sched._holds["a2"])
    sched._handle_reply(_hold(sched, "a2", 0.9))
    sched._handle_reply(_hold(sched, "a2", 50))
    rec = sched._holds["a2"]
    assert rec["accepted_at"] == first["accepted_at"]
    assert rec["hold_deadline"] == pytest.approx(first["accepted_at"] + 1, abs=1e-6)
    assert sched._holds_total == 1


def test_one_hold_budget_per_agent_per_step(caplog):
    """Review V-m5: once an agent's hold EXPIRED on a step, a new hold from it on that same
    step is ignored (the barrier is not re-extended); the next step grants a fresh budget."""
    sched = _make(_settings(step_timeout=0.01, cap=5))
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "a2", 0.01))
    assert "a2" in sched._holds
    sched._expire_holds(time.monotonic() + 10.0)
    assert sched._holds == {} and sched._holds_expired_total == 1
    assert "a2" in sched._expected_this_step and "a2" not in sched._responded_this_step
    with caplog.at_level(logging.WARNING):
        sched._handle_reply(_hold(sched, "a2", 1))
    assert sched._holds == {}
    assert sched._holds_total == 1
    assert "one hold budget per agent per step" in caplog.text
    # another agent of the same step still gets its own budget
    sched._handle_reply(_hold(sched, "a1", 1))
    assert set(sched._holds) == {"a1"}
    # a new step: a2's budget is back
    sched.time = 1
    for rec in sched.agent_collection.values():
        rec["step_response"][1] = {"reaction": "waiting", "did_step": False, "run_time": 0}
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "a2", 1))
    assert set(sched._holds) == {"a2"} and sched._holds_total == 3


# ── holds that must be ignored ───────────────────────────────────────────────

@pytest.mark.parametrize("cap", [None, 0])
def test_cap_zero_or_absent_ignores_the_hold(caplog, cap):
    sched = _make(_settings(step_timeout=0.3, cap=cap))
    script = _quick(sched, "a0", "a1") + [(0.0, lambda: _hold(sched, "a2", 5))]
    with caplog.at_level(logging.WARNING):
        wall = _run_step(sched, script)
    assert wall < 0.45                      # plain STEP_TIMEOUT, no extension
    assert "ignoring planning hold from a2" in caplog.text
    assert "a2" not in sched.agent_collection  # pruned as usual
    assert sched._holds is None
    assert "holds" not in sched.agent_stat[0]  # default agent_stat shape
    assert sched.last_step_hold_ms == 0.0


def test_hold_is_never_tallied_as_a_response():
    sched = _make(_settings(cap=5))
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "a2", 1))
    assert sched._outstanding == 3 and "a2" not in sched._responded_this_step
    assert sched.agent_collection["a2"]["step_response"][0]["reaction"] == "waiting"


def test_hold_from_unexpected_agent_is_ignored():
    sched = _make(_settings(cap=5))
    sched.agent_collection["boot"] = {"pending_boot": True, "step_response": {}}
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "boot", 1))
    sched._handle_reply(_hold(sched, "stranger", 1))
    assert not sched._holds


def test_hold_from_agent_that_already_answered_is_ignored():
    sched = _make(_settings(cap=5))
    sched._begin_step_barrier()
    sched._handle_reply(_payload(sched, "a2"))
    sched._handle_reply(_hold(sched, "a2", 1))
    assert not sched._holds


@pytest.mark.parametrize("step", [2, 4, -1, None, "3"])
def test_hold_for_a_stale_or_future_step_is_ignored(step):
    sched = _make(_settings(cap=5), time_=3)
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "a2", 1, step=step))
    assert not sched._holds


@pytest.mark.parametrize("seconds", [0, -1, "5", None, True, float("nan"), float("inf")])
def test_hold_with_bad_seconds_is_ignored(seconds):
    sched = _make(_settings(cap=5))
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "a2", seconds))
    assert not sched._holds


def test_no_hold_survives_into_the_next_step():
    sched = _make(_settings(cap=5))
    sched._begin_step_barrier()
    sched._handle_reply(_hold(sched, "a2", 1))
    assert sched._holds
    sched.time = 1
    sched._begin_step_barrier()
    assert sched._holds == {}


# ── the default path is the literal wait_for ─────────────────────────────────

def _spy_wait_for():
    real = asyncio.wait_for
    calls = []

    async def spy(aw, timeout):
        calls.append((getattr(aw, "__qualname__", None), timeout))
        return await real(aw, timeout)

    return spy, calls


@pytest.mark.parametrize("cap", [None, 0])
def test_default_path_runs_the_literal_wait_for(cap):
    sched = _make(_settings(step_timeout=0.37, cap=cap))
    spy, calls = _spy_wait_for()
    held = mock.AsyncMock()
    with mock.patch.object(sched_mod.asyncio, "wait_for", spy), \
            mock.patch.object(OpenRideScheduler, "_confirm_responses_held", held):
        _run_step(sched, _quick(sched, "a0", "a1", "a2"))
    assert ("OpenRideScheduler.confirm_responses", 0.37) in calls
    held.assert_not_called()
    assert sched._holds is None
    assert set(sched.agent_stat[0]) == {
        "completed", "ready", "error", "shutdown", "waiting", "booting", "sleeping",
        "stepping_agents", "total_agents", "boot_timed_out"}


def test_planning_path_does_not_use_the_step_timeout_wait_for():
    sched = _make(_settings(step_timeout=0.37, cap=5))
    spy, calls = _spy_wait_for()
    with mock.patch.object(sched_mod.asyncio, "wait_for", spy):
        _run_step(sched, _quick(sched, "a0", "a1", "a2"))
    assert all(t != 0.37 for _, t in calls)
    assert sched.agent_stat[0]["holds"] == 0 and sched.last_step_hold_ms == 0.0


# ── runtime: hold_ms rides the perf stream as its own field ──────────────────

def test_runtime_adds_hold_ms_only_when_a_step_was_held():
    from apps.simulation.simulation_runtime import SimulationRuntime

    rt = SimulationRuntime.__new__(SimulationRuntime)
    assert rt._with_hold_ms({"step_wall_ms": 12.0}) == {"step_wall_ms": 12.0}
    rt._last_step_hold_ms = 4000.0
    assert rt._with_hold_ms({"step_wall_ms": 4012.0}) == {"step_wall_ms": 4012.0, "hold_ms": 4000.0}
