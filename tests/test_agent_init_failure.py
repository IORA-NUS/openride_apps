"""An agent refused at init fails the run loudly, from the app side (round-2 review L-D).

Before: stock ``ORSimAgent`` caught the app's init exception, subscribed to nothing and
published ``ready`` anyway, so the scheduler pruned it as a slow agent and the run
finished -- with no assignments when it was the assignment agent -- reporting ``ok``.
orsim's ``init_failure_handler='hard'`` (passed by the container-logistics wiring) is a
no-op there. Reproduced OFFLINE (no celery, no broker) by
``$SP/ws/r3c/probes/probe_ld.py``: a real ``AssignmentAgent`` with a refusing profile
and a real ``OpenRideAgent`` whose app raises, fed into a real ``OpenRideScheduler``.

Pinned here:

- ``OpenRideAgent`` (``InitFailureReporting``) answers the boot handshake with
  ``init_error`` + the init traceback when its app failed; a healthy handshake is the
  same payload as before;
- ``OpenRideScheduler`` raises ``AgentInitFailure`` from ``step()`` on an ``init_error``
  (hard) or logs it (soft);
- it also raises for an agent that NEVER answered a step -- pruned and still silent
  ``BOOT_DEADLINE_STEPS`` steps later -- which is what a stock agent refused at init
  looks like; a pruned agent whose late reply arrives is not accused, nor is a healthy
  fleet.

Verified to have teeth (mutate / see it fail / restore):

- making ``InitFailureReporting.take_first_step`` always defer to ``super()`` failed
  ``test_a_failed_agent_answers_the_handshake_with_init_error``;
- removing the ``_note_participation`` call from ``_handle_reply`` failed
  ``test_an_init_error_fails_a_hard_run``, ``test_a_late_reply_clears_the_suspicion`` and
  ``test_an_agent_that_stepped_and_then_went_silent_is_not_an_init_failure`` (both
  accused a live agent);
- removing the ``_check_init_failures()`` call from ``step()`` failed both raise tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unittest.mock as mock

import pytest

from apps.container_logistics.openride_agent import InitFailureReporting, OpenRideAgent
from apps.simulation.openride_scheduler import AgentInitFailure, OpenRideScheduler

SETTINGS = {"DOMAIN": "d", "SIMULATION_LENGTH_IN_STEPS": 1000, "STEP_INTERVAL": 240,
            "AGENT_LAUNCH_TIMEOUT": 10, "STEP_TIMEOUT": 0.05, "STEP_TIMEOUT_TOLERANCE": 0.1,
            "REFERENCE_TIME": "2020-01-01 08:00:00", "HEARTBEAT_INTERVAL": 5, "STEP_SETTLE_SECONDS": 0,
            "ALLOW_POST_HORIZON_DRAIN": True, "STEP_TIMEOUT_ABORT_CONSECUTIVE": 10_000}
BEHAVIOR = {"steps_per_action": 1, "response_rate": 1.0, "profile": {}, "email": "e", "password": "p",
            "persona": {"domain": "d", "role": "r"}, "step_only_on_events": False}


class _Messenger:
    def __init__(self, *a, **k):
        self.published = []
        self.client = mock.Mock()
        self.client.publish.side_effect = lambda topic, data: self.published.append(json.loads(data))

    def disconnect(self):
        pass


def _agent(app_error=None):
    class Probe(OpenRideAgent):
        process_payload_on_init = False

        def _create_app(self):
            if app_error is not None:
                raise app_error
            return object()

        def entering_market(self, t):
            pass

        def step(self, t):
            return False

        def exiting_market(self):
            pass

        def estimate_next_event_time(self):
            return None

        def logout(self):
            pass

    import orsim.lifecycle.orsim_agent as OA
    from orsim.core.orsim_env import ORSimEnv

    with mock.patch.object(OA, "Messenger", _Messenger), mock.patch.object(ORSimEnv, "messenger_settings", {"MQTT_BROKER": "x"}), \
            mock.patch("eventlet.spawn"):
        agent = Probe(unique_id="facility_1", run_id="r", reference_time="20200101080000", init_time_step=0,
                      scheduler={"id": "agent_scheduler", "orsim_settings": dict(SETTINGS, STEP_TIMEOUT=1)},
                      behavior=BEHAVIOR)
        agent.start_listening()
    return agent


def test_a_failed_agent_answers_the_handshake_with_init_error():
    agent = _agent(RuntimeError("facility app init exploded"))
    assert agent.agent_failed is True
    [reply] = agent.messenger.published
    assert reply["action"] == "init_error" and reply["agent_id"] == "facility_1"
    assert "RuntimeError: facility app init exploded" in reply["details"]
    assert isinstance(agent, InitFailureReporting)


def test_a_healthy_agent_handshake_is_unchanged():
    agent = _agent()
    [reply] = agent.messenger.published
    assert agent.agent_failed is False
    assert set(reply) == {"agent_id", "time_step", "action", "did_step", "run_time"}
    assert reply["action"] == "ready" and reply["did_step"] is False and reply["time_step"] == 0


def _scheduler(handler="hard"):
    with mock.patch("orsim.core.orsim_scheduler.ORSimEnv") as env, \
            mock.patch("orsim.core.orsim_scheduler.Messenger"), \
            mock.patch("apps.simulation.openride_scheduler.start_agent"):
        env.messenger_settings = {"MQTT_BROKER": "localhost"}
        env.validate_orsim_settings = lambda s: s
        sched = OpenRideScheduler("r", "agent_scheduler", dict(SETTINGS), init_failure_handler=handler)
    return sched


def _drive(sched, steps, answering):
    """Run ``steps`` scheduler steps; the ids in ``answering`` reply to each one."""

    async def run():
        for _ in range(steps):
            task = asyncio.ensure_future(sched.step())
            await asyncio.sleep(0)
            for aid in answering:
                sched._handle_reply({"agent_id": aid, "time_step": sched.time, "action": "completed",
                                     "did_step": True})
            await task

    logging.disable(logging.CRITICAL)
    try:
        asyncio.new_event_loop().run_until_complete(run())
    finally:
        logging.disable(logging.NOTSET)


def _boot(sched, ids):
    for aid in ids:
        sched.add_agent({"unique_id": aid}, "/x", "m.C")
        sched._handle_reply({"agent_id": aid, "time_step": 0, "action": "ready", "did_step": False})


def test_an_init_error_fails_a_hard_run():
    sched = _scheduler()
    _boot(sched, ["truck_1"])
    sched.add_agent({"unique_id": "facility_1"}, "/x", "m.C")
    sched._handle_reply({"agent_id": "facility_1", "time_step": 0, "action": "init_error", "did_step": False,
                         "details": "Traceback ...\nPolicyError: refused"})
    with pytest.raises(AgentInitFailure, match="facility_1: init_error .*PolicyError: refused"):
        _drive(sched, 1, ["truck_1"])


def test_a_soft_run_only_logs_an_init_error():
    sched = _scheduler("soft")
    _boot(sched, ["truck_1"])
    sched._handle_reply({"agent_id": "facility_1", "time_step": 0, "action": "init_error", "details": "x"})
    _drive(sched, 3, ["truck_1"])
    assert sched.time == 3


def test_an_agent_that_never_answers_a_step_is_logged_not_fatal(monkeypatch):
    """A stock agent refused at init: ``ready``, then silence (the assignment agent of the finding).

    The silence signal is a heuristic, so even a ``'hard'`` run only logs it, once: a healthy
    agent pruned during a slow boot must never fail a good run (orchestrator decision, round 3).
    Verified to have teeth: making the overdue branch raise fails this test; dropping the log
    fails the log assertion.
    """
    sched = _scheduler()
    _boot(sched, ["truck_1", "assignment_main"])
    import apps.simulation.openride_scheduler as sched_mod
    seen = []
    monkeypatch.setattr(sched_mod.logging, "error", lambda msg, *a, **k: seen.append(msg % a if a else msg))
    _drive(sched, OpenRideScheduler.BOOT_DEADLINE_STEPS + 5, ["truck_1"])
    assert sched.time == OpenRideScheduler.BOOT_DEADLINE_STEPS + 5
    hits = [m for m in seen if "AGENT NEVER STEPPED" in m and "assignment_main" in m]
    assert len(hits) == 1


def test_a_late_reply_clears_the_suspicion():
    sched = _scheduler()
    _boot(sched, ["truck_1", "truck_2"])
    _drive(sched, 1, ["truck_1"])                     # truck_2 pruned on its first step
    assert "truck_2" not in sched.agent_collection
    sched._handle_reply({"agent_id": "truck_2", "time_step": 0, "action": "completed", "did_step": True})
    assert "truck_2" in sched.agent_collection        # reinstated
    _drive(sched, OpenRideScheduler.BOOT_DEADLINE_STEPS + 3, ["truck_1", "truck_2"])


def test_a_healthy_fleet_is_never_accused():
    sched = _scheduler()
    ids = [f"truck_{i}" for i in range(20)]
    _boot(sched, ids)
    _drive(sched, OpenRideScheduler.BOOT_DEADLINE_STEPS + 5, ids)
    assert sched.time == OpenRideScheduler.BOOT_DEADLINE_STEPS + 5


def test_an_agent_that_stepped_and_then_went_silent_is_not_an_init_failure():
    """Pruning a live agent mid-run is the step barrier's business (§6.22), not init's."""
    sched = _scheduler()
    _boot(sched, ["truck_1", "truck_2"])
    _drive(sched, 2, ["truck_1", "truck_2"])
    _drive(sched, OpenRideScheduler.BOOT_DEADLINE_STEPS + 3, ["truck_1"])
