"""NetworkFlowDay WS-B, agent side: the planning step leaves the paho callback (critique B1, M1).

``AssignmentAgent.handle_orsim_agent_message`` runs inside paho's network-loop callback,
and paho's threaded mode only WRITES queued packets on the loop's next pass. The fake
client here behaves the same way (``publish`` only queues; the fake loop flushes after
the callback returns, and keeps flushing while greenlets run), so a plan run inside the
callback would see its ``hold`` still unsent. Pinned:

- a planning-due step publishes the WS-C hold (exact payload, scheduler topic), RETURNS
  from the callback, and the plan runs afterwards in a greenlet, i.e. after the hold was
  flushed; its reply is orsim's exact ``completed`` shape;
- a non-planning step is ``ORSimAgent.handle_orsim_agent_message`` unchanged (no hold,
  reply inside the callback);
- a step message arriving while the planning greenlet runs is queued and answered after
  it, never concurrently -- and the queue stays exclusive through the DRAIN (FX1 B1: a
  message delivered while a drained step is inside the app is queued too); a step backlog
  collapses to the newest step (superseded steps are answered ``completed``,
  ``did_step: False``, ``superseded_by``, with no work and no cadence draw); a queued
  ``shutdown`` is always kept;
- a failure before the planning greenlet exists (spawn / spec / bootstrap) clears the
  planning flags and runs the step through orsim's own handler (FX1 minor 7);
- M1: the heartbeat self-kill is suppressed until the reply is out, across a plan AND a
  dispatch together longer than ``STEP_TIMEOUT`` (teeth: orsim's own heartbeat check
  kills the same step);
- an exception outside the step yields orsim's ``error`` reply and clears
  ``message_processing_active``;
- no planning at or past the horizon;
- the RNG pin: exactly one ``random()`` per cadence step and none otherwise, with and
  without a planning declaration (planning steps that are not cadence steps included).
"""

from __future__ import annotations

import json
import time
import types
import unittest.mock as mock

import eventlet
import pytest

from orsim.lifecycle import ORSimAgent

from apps.container_logistics.assignment import agent as agent_mod
from apps.container_logistics.assignment import planning as P
from apps.container_logistics.assignment.agent import AssignmentAgent

SETTINGS = {"DOMAIN": "d", "SIMULATION_LENGTH_IN_STEPS": 2520, "STEP_INTERVAL": 240,
            "AGENT_LAUNCH_TIMEOUT": 10, "STEP_TIMEOUT": 30, "STEP_TIMEOUT_TOLERANCE": 0.1,
            "REFERENCE_TIME": "2020-01-01 08:00:00", "HEARTBEAT_INTERVAL": 5, "STEP_SETTLE_SECONDS": 0,
            "ALLOW_POST_HORIZON_DRAIN": True, "STEP_TIMEOUT_ABORT_CONSECUTIVE": 10_000}
DECL = {"at_day_start": True, "every_steps": 18, "foresight": "day", "max_hold_seconds": 30}
SCHED_TOPIC = "r/service_scheduler/ORSimScheduler"
AGENT_TOPIC = "r/service_scheduler/ORSimAgent"
REPLY_KEYS = {"agent_id", "time_step", "action", "did_step", "run_time"}


class _PahoLike:
    """``publish`` only QUEUES (paho ``loop_start`` mode); the loop's pass ``flush``es."""

    def __init__(self):
        self.queued, self.sent = [], []

    def publish(self, topic, payload):
        self.queued.append((topic, json.loads(payload)))

    def subscribe(self, *_a, **_k):
        pass

    def flush(self):
        self.sent.extend(self.queued)
        self.queued.clear()


class _Messenger:
    def __init__(self, *_a, **_k):
        self.client = _PahoLike()

    def disconnect(self):
        pass


class _App:
    """Stand-in for ``AssignmentApp``: the REAL ``PlanningSchedule``, recorded calls."""

    def __init__(self, decl, client, *, plan_seconds=0.0, assign_seconds=0.0):
        self.decl, self.client = decl, client
        self.sched = P.PlanningSchedule()
        self.plan_seconds, self.assign_seconds = plan_seconds, assign_seconds
        self.calls = []
        self.busy = False
        self._sim_horizon_steps = None
        self._step_seconds = None

    def planning_spec(self):
        return self.decl

    def planning_due(self, time_step, *, step_seconds=None, horizon_steps=None):
        if not self.decl:
            return None
        return self.sched.due(self.decl, time_step, step_seconds=step_seconds, horizon_steps=horizon_steps)

    def _enter(self):
        assert not self.busy, "two steps ran concurrently on one app"
        self.busy = True

    def plan(self, sim_clock, step, due, **kw):
        self._enter()
        self.calls.append(("plan", step, [m for _t, m in self.client.sent]))
        self.plan_kwargs = getattr(self, "plan_kwargs", []) + [kw]
        self.sched.mark(due, self.decl, step_seconds=240)
        if self.plan_seconds:
            eventlet.sleep(self.plan_seconds)
        self.busy = False

    def assign(self, sim_clock, step):
        self._enter()
        self.calls.append(("assign", step))
        if self.assign_seconds:
            eventlet.sleep(self.assign_seconds)
        self.busy = False
        return []

    def publish(self, result):
        pass

    def update_current(self, sim_clock):
        pass

    def launch(self, sim_clock):
        pass

    def close(self, sim_clock):
        pass


def _agent(monkeypatch, decl=DECL, *, steps_per_action=6, **app_kw):
    import orsim.lifecycle.orsim_agent as OA
    from orsim.core.orsim_env import ORSimEnv

    monkeypatch.delenv("OPENRIDE_AGENT_TRANSPORT", raising=False)
    behavior = {"steps_per_action": steps_per_action, "response_rate": 1.0, "profile": {}, "email": "e",
                "password": "p", "persona": {"domain": "d", "role": "r"}, "step_only_on_events": False}
    holder = {}

    def create_app(self):
        holder["app"] = _App(decl, self.messenger.client, **app_kw)
        return holder["app"]

    with mock.patch.object(OA, "Messenger", _Messenger), \
            mock.patch.object(ORSimEnv, "messenger_settings", {"MQTT_BROKER": "x"}), \
            mock.patch.object(AssignmentAgent, "_create_app", create_app):
        agent = AssignmentAgent(unique_id="assignment_main", run_id="r", reference_time="20200101080000",
                                init_time_step=0, scheduler={"id": "service_scheduler",
                                                             "orsim_settings": dict(SETTINGS)},
                                behavior=behavior)
    agent.take_first_step({"action": "init", "time_step": 0})
    agent.messenger.client.flush()
    agent.messenger.client.sent.clear()
    return agent, holder["app"]


def _deliver(agent, payload):
    """paho's loop: run the on_message callback, then the loop's next pass writes the socket."""
    msg = types.SimpleNamespace(topic=AGENT_TOPIC, payload=json.dumps(payload).encode())
    agent.on_receive_message(agent.messenger.client, None, msg)
    agent.messenger.client.flush()


def _spin(agent, until, timeout=5.0):
    """Keep the fake loop running (yield + flush) until ``until()`` or the timeout."""
    end = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < end, "timed out waiting on the planning greenlet"
        eventlet.sleep(0.001)
        agent.messenger.client.flush()
    agent.messenger.client.flush()


def _replies(agent):
    return [m for t, m in agent.messenger.client.sent if t == SCHED_TOPIC]


def _step(t):
    return {"action": "step", "time_step": t}


# --- B1 ---------------------------------------------------------------------------------------

def test_the_hold_leaves_before_the_plan_runs(monkeypatch):
    agent, app = _agent(monkeypatch)
    _deliver(agent, _step(0))
    # The callback has RETURNED: the hold is on the wire, the plan has not run yet.
    assert app.calls == [] and agent._planning_active is True
    assert _replies(agent) == [{"agent_id": "assignment_main", "time_step": 0, "action": "hold",
                                "hold_seconds": 30, "reason": "planning"}]
    _spin(agent, lambda: not agent._planning_active)
    [plan, assign] = app.calls
    assert plan[0] == "plan" and plan[1] == 0
    assert plan[2] and plan[2][0]["action"] == "hold"  # the plan saw the hold already SENT
    assert assign == ("assign", 0)  # step 0 is also a cadence step: plan, then dispatch
    hold, reply = _replies(agent)
    assert set(reply) == REPLY_KEYS and reply["action"] == "completed" and reply["time_step"] == 0
    assert reply["did_step"] is True and reply["agent_id"] == "assignment_main"
    assert agent.message_processing_active is False


def test_a_non_planning_step_is_orsims_handler_unchanged(monkeypatch):
    agent, app = _agent(monkeypatch)
    _deliver(agent, _step(0))
    _spin(agent, lambda: not agent._planning_active)
    agent.messenger.client.sent.clear()
    orig, seen = ORSimAgent.handle_orsim_agent_message, []

    def spy(self, payload):
        seen.append(payload)
        return orig(self, payload)

    monkeypatch.setattr(ORSimAgent, "handle_orsim_agent_message", spy)
    _deliver(agent, _step(6))
    assert seen == [_step(6)]
    [reply] = _replies(agent)  # answered inside the callback: no greenlet, no hold
    assert set(reply) == REPLY_KEYS and reply["action"] == "completed" and reply["time_step"] == 6
    assert app.calls[-1] == ("assign", 6) and agent._planning_active is False


def test_a_step_arriving_during_the_planning_step_is_answered_after_it(monkeypatch):
    agent, app = _agent(monkeypatch, plan_seconds=0.05, steps_per_action=1)
    _deliver(agent, _step(0))
    eventlet.sleep(0.001)  # the greenlet is now inside the plan
    _deliver(agent, _step(1))  # e.g. the scheduler moved on after a hold expiry
    assert [c[:2] for c in app.calls] == [("plan", 0)]
    _spin(agent, lambda: len(_replies(agent)) == 3)
    assert [(m["action"], m["time_step"]) for m in _replies(agent)] == [
        ("hold", 0), ("completed", 0), ("completed", 1)]
    assert [c[:2] for c in app.calls] == [("plan", 0), ("assign", 0), ("assign", 1)]


def test_a_queued_planning_step_runs_its_own_planning_step(monkeypatch):
    agent, app = _agent(monkeypatch, {"at_day_start": False, "every_steps": 1, "foresight": "none",
                                      "max_hold_seconds": 5}, plan_seconds=0.02)
    _deliver(agent, _step(0))
    eventlet.sleep(0.001)
    _deliver(agent, _step(1))
    _spin(agent, lambda: len(_replies(agent)) == 4)
    assert [(m["action"], m["time_step"]) for m in _replies(agent)] == [
        ("hold", 0), ("completed", 0), ("hold", 1), ("completed", 1)]


# --- M1 ---------------------------------------------------------------------------------------

def _with_heartbeat(agent, interval=0.005):
    """orsim's ``run_forever`` heartbeat greenlet, at a test interval."""
    stop = []

    def run():
        while not stop:
            eventlet.sleep(interval)
            agent.handle_heartbeat_failure()

    gt = eventlet.spawn(run)
    return stop, gt


def test_heartbeat_does_not_self_kill_a_long_plan_plus_dispatch(monkeypatch):
    agent, app = _agent(monkeypatch, plan_seconds=0.12, assign_seconds=0.06)
    agent.orsim_settings["STEP_TIMEOUT"] = 0.05  # both halves together are > 3x this
    stop, gt = _with_heartbeat(agent)
    try:
        _deliver(agent, _step(0))
        _spin(agent, lambda: not agent._planning_active)
        eventlet.sleep(0.03)  # heartbeats after the reply: nothing is processing any more
    finally:
        stop.append(1)
        gt.wait()
    assert agent._shutdown is False
    assert [m["action"] for m in _replies(agent)] == ["hold", "completed"]


def test_heartbeat_teeth_orsims_check_kills_the_same_step(monkeypatch):
    agent, app = _agent(monkeypatch, plan_seconds=0.12, assign_seconds=0.06)
    agent.orsim_settings["STEP_TIMEOUT"] = 0.05
    monkeypatch.setattr(AssignmentAgent, "handle_heartbeat_failure", ORSimAgent.handle_heartbeat_failure)
    stop, gt = _with_heartbeat(agent)
    try:
        _deliver(agent, _step(0))
        _spin(agent, lambda: not agent._planning_active)
    finally:
        stop.append(1)
        gt.wait()
    assert agent._shutdown is True and _replies(agent)[-1]["action"] == "shutdown"


# --- errors, horizon ------------------------------------------------------------------------

def test_an_exception_outside_the_step_is_orsims_error_reply(monkeypatch):
    agent, app = _agent(monkeypatch)
    monkeypatch.setattr(agent, "estimate_next_event_time", mock.Mock(side_effect=RuntimeError("boom")))
    _deliver(agent, _step(0))
    _spin(agent, lambda: not agent._planning_active)
    hold, reply = _replies(agent)
    assert set(reply) == REPLY_KEYS | {"details"} and reply["action"] == "error" and reply["did_step"] is False
    assert "RuntimeError: boom" in reply["details"] and agent.message_processing_active is False


def test_no_planning_at_or_past_the_horizon(monkeypatch):
    agent, app = _agent(monkeypatch)
    agent.orsim_settings["SIMULATION_LENGTH_IN_STEPS"] = 12  # step 12 would catch up day 0; 18 is an interval boundary
    for t in (12, 18, 24):
        _deliver(agent, _step(t))
    assert agent._planning_active is False
    assert [m["action"] for m in _replies(agent)] == ["completed"] * 3
    assert [c[0] for c in app.calls] == ["assign", "assign", "assign"]


# --- the RNG pin --------------------------------------------------------------------------------

@pytest.mark.parametrize("decl", [None, DECL, {"at_day_start": False, "every_steps": 15, "foresight": "none",
                                               "max_hold_seconds": 30}])
def test_rng_pin_one_draw_per_cadence_step_only(monkeypatch, decl):
    agent, app = _agent(monkeypatch, decl)
    draws = []
    monkeypatch.setattr(agent_mod, "random", lambda: draws.append(agent.current_time_step) or 0.0)
    for t in range(0, 41):
        _deliver(agent, _step(t))
        _spin(agent, lambda: not agent._planning_active)
    assert draws == [t for t in range(0, 41) if t % 6 == 0]
    plans = [c[1] for c in app.calls if c[0] == "plan"]
    expected = {None: [], "day": [0, 18, 36], "none": [0, 15, 30]}
    assert plans == expected[None if decl is None else decl["foresight"]]
    replies = [m for m in _replies(agent) if m["action"] == "completed"]
    assert [m["time_step"] for m in replies] == list(range(0, 41))
    did = {m["time_step"]: m["did_step"] for m in replies}
    assert all(did[t] for t in plans)  # a planning step that is not a cadence step did step
    assert not any(did[t] for t in range(41) if t % 6 and t not in plans)


def test_a_failing_plan_still_dispatches_and_draws(monkeypatch):
    agent, app = _agent(monkeypatch)
    draws = []
    monkeypatch.setattr(agent_mod, "random", lambda: draws.append(1) or 0.0)
    monkeypatch.setattr(app, "plan", mock.Mock(side_effect=RuntimeError("solver down")))
    _deliver(agent, _step(0))
    _spin(agent, lambda: not agent._planning_active)
    assert draws == [1] and app.calls == [("assign", 0)]
    assert _replies(agent)[-1]["action"] == "completed"


def test_end_to_end_real_app_plans_then_dispatches_through_the_greenlet(monkeypatch):
    """The real ``AssignmentApp`` on the planning toy behind the real agent protocol."""
    from apps.container_logistics.policies.framework.registry import isolated_registry
    from tests import test_assignment_planning_app as A

    with isolated_registry():
        A.register_toys()
        mgr = A._Manager([A._truck("t1")], [A._order("o1")], open_orders=[A._order("o1")])
        real = A._app(mgr, A._profile(), run_id="r")
        real._step_seconds = real._sim_horizon_steps = None  # the agent hands both over
        agent, _fake = _agent(monkeypatch, steps_per_action=6)
        agent.app = real
        _deliver(agent, _step(0))
        assert [n for n in mgr.names()] == []  # nothing read inside the callback
        _spin(agent, lambda: not agent._planning_active)
        assert mgr.names()[:4] == ["list_trucks", "open_haul_rows", "list_open_orders_until", "list_trucks"]
        assert [(m["truck_id"], m["order"]["_id"]) for _t, m in real.messenger.published] == [("t1", "o1")]
        assert [m["action"] for m in _replies(agent)] == ["hold", "completed"]
        assert real._step_seconds == 240 and real._sim_horizon_steps == 2520


# --- FX1 B1: the queue stays exclusive through the drain; a step backlog collapses ----------

def _overlap_probe(app, agent):
    """Record every app entry made while another step is still inside the app."""
    overlaps = []

    def enter():
        if app.busy:
            overlaps.append(agent.current_time_step)
        app.busy = True

    app._enter = enter
    return overlaps


def _completed(agent):
    return [m for m in _replies(agent) if m["action"] in ("completed", "shutdown")]


def test_a_message_delivered_during_the_drain_is_queued_not_run_concurrently(monkeypatch):
    """Review B1's repro: a step arriving while the DRAIN is inside the app (5003012 ran it
    concurrently: assign(2) inside assign(1), step 2 answered before step 1)."""
    agent, app = _agent(monkeypatch, plan_seconds=0.05, assign_seconds=0.05, steps_per_action=1)
    overlaps = _overlap_probe(app, agent)
    _deliver(agent, _step(0))
    eventlet.sleep(0.001)
    _deliver(agent, _step(1))  # queued behind the planning step
    _spin(agent, lambda: any(c[:2] == ("assign", 1) for c in app.calls))
    assert agent._planning_active is False and app.busy  # the drain is inside assign(1)
    _deliver(agent, _step(2))  # arrives DURING the drain
    assert [c[:2] for c in app.calls][-1] == ("assign", 1), "step 2 must not start inside step 1"
    _spin(agent, lambda: len(_completed(agent)) >= 3)
    eventlet.sleep(0.01)
    assert not overlaps, overlaps
    assert [(m["action"], m["time_step"]) for m in _replies(agent)] == [
        ("hold", 0), ("completed", 0), ("completed", 1), ("completed", 2)]
    assert [c[:2] for c in app.calls] == [("plan", 0), ("assign", 0), ("assign", 1), ("assign", 2)]
    assert agent._pending_messages is not None and not agent._pending_messages
    assert agent._draining == 0 and agent.message_processing_active is False
    # Idle again: the next step is orsim's inline handler (no queue, no greenlet).
    _deliver(agent, _step(3))
    assert _replies(agent)[-1]["time_step"] == 3 and app.calls[-1] == ("assign", 3)


def test_a_step_backlog_collapses_to_the_newest_step(monkeypatch):
    agent, app = _agent(monkeypatch, plan_seconds=0.05, steps_per_action=1)
    overlaps = _overlap_probe(app, agent)
    draws = []
    monkeypatch.setattr(agent_mod, "random", lambda: draws.append(agent.current_time_step) or 0.0)
    _deliver(agent, _step(0))
    eventlet.sleep(0.001)
    for t in (1, 2, 3):  # e.g. the hold expired and the scheduler stepped on three times
        _deliver(agent, _step(t))
    _spin(agent, lambda: len(_completed(agent)) == 4)
    replies = _replies(agent)
    assert [(m["action"], m["time_step"]) for m in replies] == [
        ("hold", 0), ("completed", 0), ("completed", 1), ("completed", 2), ("completed", 3)]
    # Superseded steps: orsim's shape, no work, pointing at the step that replaced them.
    for m in replies[2:4]:
        assert set(m) == REPLY_KEYS | {"superseded_by"}
        assert m["did_step"] is False and m["superseded_by"] == 3
    assert set(replies[4]) == REPLY_KEYS and replies[4]["did_step"] is True
    assert [c[:2] for c in app.calls] == [("plan", 0), ("assign", 0), ("assign", 3)]
    assert draws == [0, 3]  # no cadence draw for a superseded step
    assert agent.current_time_step == 3 and not overlaps


def test_a_queued_shutdown_is_kept_and_supersedes_the_steps_before_it(monkeypatch):
    agent, app = _agent(monkeypatch, plan_seconds=0.05, steps_per_action=1)
    _deliver(agent, _step(0))
    eventlet.sleep(0.001)
    _deliver(agent, _step(1))
    _deliver(agent, {"action": "shutdown", "time_step": 2})
    _spin(agent, lambda: len(_completed(agent)) == 3)
    assert [(m["action"], m["time_step"], m.get("superseded_by")) for m in _replies(agent)] == [
        ("hold", 0, None), ("completed", 0, None), ("completed", 1, 2), ("shutdown", 2, None)]
    assert agent._shutdown is True
    assert [c[:2] for c in app.calls] == [("plan", 0), ("assign", 0)]


def test_a_queued_step_older_than_one_handled_is_never_processed(monkeypatch):
    agent, app = _agent(monkeypatch, plan_seconds=0.05, steps_per_action=1)
    _deliver(agent, _step(0))
    eventlet.sleep(0.001)
    _deliver(agent, _step(0))  # a duplicate of the planning step itself
    _spin(agent, lambda: len(_completed(agent)) == 2)
    assert [(m["action"], m["time_step"], m.get("superseded_by")) for m in _replies(agent)] == [
        ("hold", 0, None), ("completed", 0, None), ("completed", 0, 0)]
    assert [c[:2] for c in app.calls] == [("plan", 0), ("assign", 0)]


def test_queueing_keeps_the_owning_steps_bookkeeping(monkeypatch):
    """A queued arrival must not reset the step in flight's heartbeat clock or its log."""
    agent, app = _agent(monkeypatch, plan_seconds=0.05)
    _deliver(agent, _step(0))
    eventlet.sleep(0.001)
    before = agent.start_time
    agent.add_step_log("in flight")
    eventlet.sleep(0.01)
    _deliver(agent, _step(1))
    assert agent.start_time == before
    assert any("in flight" in str(v) for v in agent.step_log.values())
    _spin(agent, lambda: len(_completed(agent)) == 2)


# --- FX1 minor 7: a failure before the planning greenlet exists ------------------------------

@pytest.mark.parametrize("where", ["spawn", "spec", "bootstrap"])
def test_a_failure_before_the_greenlet_falls_back_to_orsims_handler(monkeypatch, where):
    agent, app = _agent(monkeypatch)
    if where == "spawn":
        monkeypatch.setattr(eventlet, "spawn", mock.Mock(side_effect=RuntimeError("no hub")))
    elif where == "spec":
        monkeypatch.setattr(app, "planning_spec", mock.Mock(side_effect=RuntimeError("bad spec")))
    else:
        real = agent.bootstrap_step
        calls = []

        def bootstrap(t):
            calls.append(t)
            if len(calls) == 1:
                raise RuntimeError("bad clock")
            return real(t)

        monkeypatch.setattr(agent, "bootstrap_step", bootstrap)
    _deliver(agent, _step(0))  # must not raise out of the paho callback
    assert agent._planning_active is False and agent._planning_gt is None
    assert agent.message_processing_active is False and not agent._busy()
    replies = _replies(agent)
    assert replies[-1]["action"] == "completed" and replies[-1]["time_step"] == 0
    assert [m["action"] for m in replies] == (["hold", "completed"] if where == "spawn" else ["completed"])
    assert [c[:2] for c in app.calls] == [("plan", 0), ("assign", 0)]  # the step ran, inline
    _deliver(agent, _step(6))  # and the agent keeps answering
    assert _replies(agent)[-1]["time_step"] == 6 and app.calls[-1] == ("assign", 6)


def test_a_failure_the_inline_fallback_cannot_survive_is_orsims_error_reply(monkeypatch):
    agent, app = _agent(monkeypatch)
    monkeypatch.setattr(agent, "bootstrap_step", mock.Mock(side_effect=RuntimeError("bad clock")))
    _deliver(agent, _step(0))
    [reply] = _replies(agent)
    assert reply["action"] == "error" and "bad clock" in reply["details"]
    assert agent._planning_active is False and agent.message_processing_active is False


def test_the_plan_is_handed_the_hold_publish_instant(monkeypatch):
    """Review V-m2: the planning tick's budget anchor is the hold publish, not the plan start."""
    agent, app = _agent(monkeypatch)
    before = time.monotonic()
    _deliver(agent, _step(0))
    _spin(agent, lambda: not agent._planning_active)
    [kw] = app.plan_kwargs
    assert before <= kw["hold_started_s"] <= time.monotonic()
    assert agent._planning_hold_started_s is None  # consumed: never reused by a later plan
