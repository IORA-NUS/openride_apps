import json
import logging
import time
import traceback
from collections import deque
from random import random

from orsim.lifecycle import ORSimAgent

from apps.agent_transport import TransportSelectingAgent

from .app import AssignmentApp
from .planning import Due


# TransportSelectingAgent (a no-op while OPENRIDE_AGENT_TRANSPORT is unset) is what
# keeps this agent on the SAME broker as the scheduler. It subclasses ORSimAgent
# directly rather than through OpenRideAgent, so it would otherwise stay on MQTT
# during a Kafka run and hang the step barrier as a permanently silent agent.
class AssignmentAgent(TransportSelectingAgent, ORSimAgent):
    """The assignment service agent.

    PLANNING STEPS (NetworkFlowDay F1/F2; reached ONLY when the bound top declared
    ``planning``, which no shipped scenario does): ``handle_orsim_agent_message`` runs in
    paho's network-loop callback, and paho (threaded ``loop_start``, a greenthread under
    celery's eventlet pool) only writes queued packets on that loop's next pass. A plan
    run inside the callback would therefore hold the ``hold`` message (and the MQTT
    keepalive) until the plan finished (critique B1). So a planning-due step:

    1. bootstraps the step as orsim would, publishes the ``hold``
       (``{agent_id, time_step, action: "hold", hold_seconds, reason: "planning"}``,
       WS-C protocol) and ``eventlet.spawn``s :meth:`_planning_step`, then RETURNS from
       the callback, so the loop flushes the hold and keeps the connection alive;
    2. the greenlet runs the ordinary step (plan, then the dispatch tick if the step is
       also a cadence step) and publishes the ordinary orsim ``completed`` / ``error``
       reply (orsim's exact payload shape), clearing ``message_processing_active`` in
       ``finally``;
    3. a message arriving meanwhile (only after a hold expiry, or a shutdown) is queued
       and handled, in order, after the greenlet; it never runs concurrently on the app.

    The heartbeat self-kill (``STEP_TIMEOUT`` while processing) is suppressed while the
    greenlet is alive, i.e. until the reply is published (critique M1). Every other
    step goes to ``ORSimAgent.handle_orsim_agent_message`` unchanged.
    """

    #: True from the hold until the planning step's reply is published.
    _planning_active = False
    _planning_gt = None
    _pending_messages = None

    def _create_app(self):
        return AssignmentApp(
            run_id=self.run_id,
            sim_clock=self.get_current_time_str(),
            behavior=self.behavior,
            messenger=self.messenger,
            agent_helper=self,
        )

    @property
    def process_payload_on_init(self):
        return False

    def entering_market(self, time_step):
        app = getattr(self, "app", None)
        if self.active is False and app is not None:
            # Hand the app the simulation horizon so its market provenance can
            # separate in-horizon ticks from post-horizon drain ticks, putting the
            # tick count on the same window as the KPI block beside it (plan R3-5).
            try:
                app._sim_horizon_steps = (self.orsim_settings or {}).get(
                    "SIMULATION_LENGTH_IN_STEPS"
                )
            except Exception:
                app._sim_horizon_steps = None
            try:
                app._step_seconds = (self.orsim_settings or {}).get("STEP_INTERVAL")
            except Exception:
                app._step_seconds = None
            app.launch(sim_clock=self.get_current_time_str())
        self.active = True

    def exiting_market(self):
        self.active = False

    def logout(self):
        app = getattr(self, "app", None)
        if app is not None:
            app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        return self.current_time

    # --- planning steps (B1 / M1) -------------------------------------------------------

    def _scheduler_topic(self):
        return f"{self.run_id}/{self.scheduler_id}/ORSimScheduler"

    def _planning_due_for(self, time_step):
        """The app's ``planning.Due`` for ``time_step``, or ``None`` (never without a declaration)."""
        app = getattr(self, "app", None)
        if app is None or self._shutdown or getattr(self, "agent_failed", False):
            return None
        planning_due = getattr(app, "planning_due", None)
        if planning_due is None:
            return None
        settings = self.orsim_settings or {}
        due = planning_due(time_step, step_seconds=settings.get("STEP_INTERVAL"),
                           horizon_steps=settings.get("SIMULATION_LENGTH_IN_STEPS"))
        return due if isinstance(due, Due) else None

    def handle_orsim_agent_message(self, payload):
        if self._planning_active:
            # Never two steps on one app: answered after the planning step's reply.
            if self._pending_messages is None:
                self._pending_messages = deque()
            self._pending_messages.append(payload)
            self.add_step_log("queued behind a planning step")
            return
        time_step = payload.get("time_step") if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and payload.get("action") == "step"
                and self._planning_due_for(time_step) is not None):
            return super().handle_orsim_agent_message(payload)
        self._start_planning_step(payload)

    def _start_planning_step(self, payload):
        import eventlet  # orsim's rule: never at module top (monkey patching)

        self.add_step_log("In handle_orsim_agent_message (planning step)")
        started = self.start_time
        self.bootstrap_step(payload["time_step"])
        spec = self.app.planning_spec() or {}
        hold = {
            "agent_id": self.unique_id,
            "time_step": self.current_time_step,
            "action": "hold",
            "hold_seconds": spec.get("max_hold_seconds"),
            "reason": "planning",
        }
        self._planning_active = True
        try:
            self.messenger.client.publish(self._scheduler_topic(), json.dumps(hold))
        except Exception:
            logging.exception("AssignmentAgent %s: could not publish the planning hold for step %s",
                              self.unique_id, self.current_time_step)
        self._planning_gt = eventlet.spawn(self._planning_step, payload, started)
        # on_receive_message logs end_time - start_time after this returns.
        self.end_time = time.time()

    def _planning_step(self, payload, started):
        """The planning step's body (a greenlet): orsim's ``step`` branch, then its reply."""
        try:
            try:
                did_step = self.process_payload(payload)
                self.next_event_time = self.estimate_next_event_time()
                response_payload = {
                    "agent_id": self.unique_id,
                    "time_step": self.current_time_step,
                    "action": "completed" if self._shutdown == False else "shutdown",  # noqa: E712 (orsim's)
                    "did_step": did_step,
                    "run_time": time.time() - started,
                }
            except Exception:
                response_payload = {
                    "agent_id": self.unique_id,
                    "time_step": self.current_time_step,
                    "action": "error",
                    "did_step": False,
                    "run_time": time.time() - started,
                    "details": traceback.format_exc(),
                }
            try:
                self.messenger.client.publish(self._scheduler_topic(), json.dumps(response_payload))
            except Exception:
                logging.exception("AssignmentAgent %s: could not publish the planning step's reply",
                                  self.unique_id)
        finally:
            self.end_time = time.time()
            self.start_time = time.time()
            self._planning_gt = None
            self._planning_active = False
            self.message_processing_active = False
        self._drain_pending_messages()

    def _drain_pending_messages(self):
        """Handle the messages queued behind a planning step, in order (stops at the next planning step)."""
        queue = self._pending_messages
        while queue and not self._planning_active:
            payload = queue.popleft()
            self.start_time = time.time()
            self.message_processing_active = True
            self.reset_step_log()
            self.handle_orsim_agent_message(payload)

    def handle_heartbeat_failure(self):
        if self._planning_active:
            # M1: a planning step is processing until its reply is out; the scheduler's
            # hold (capped) bounds the barrier, not this self-kill.
            return
        return super().handle_heartbeat_failure()

    def _maybe_plan(self, app):
        """Run the planning tick when this step is planning-due; ``True`` when it did."""
        due = self._planning_due_for(self.current_time_step)
        if due is None:
            return False
        try:
            app.plan(self.get_current_time_str(), self.current_time_step, due)
        except Exception:
            # A failed plan must not skip this step's dispatch (nor its cadence draw, the
            # module-RNG pin): log it; the next boundary plans again.
            logging.exception("AssignmentAgent %s: planning tick at step %s failed",
                              self.unique_id, self.current_time_step)
        # M1: the dispatch that may follow is a fresh STEP_TIMEOUT window.
        self.start_time = time.time()
        return True

    def step(self, time_step):
        if getattr(self, "app", None) is None:
            return False
        self.app.update_current(self.get_current_time_str())
        planned = self._maybe_plan(self.app)
        step_only_on_events = bool(self.behavior.get("step_only_on_events", False))
        if (
            self.current_time_step % self.behavior.get("steps_per_action", 1) == 0
            and random() <= self.behavior.get("response_rate", 1.0)
        ):
            if step_only_on_events and self.estimate_next_event_time() > self.current_time:
                return planned
            result = self.app.assign(self.get_current_time_str(), self.current_time_step)
            self.app.publish(result)
            return True
        return planned
