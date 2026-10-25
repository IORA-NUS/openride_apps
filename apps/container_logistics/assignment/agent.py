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
       The queue stays exclusive through the drain, and a step backlog collapses to the
       newest step (see "the exclusive message queue" below).

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

    # --- the exclusive message queue (FX1 B1) --------------------------------------------
    #
    # Once a planning step has spawned its greenlet, the paho callback may deliver further
    # messages while ANOTHER greenlet is still working on the app (the plan, then every
    # message drained behind it). So every message is QUEUED while ``_busy()``: a planning
    # step is active, a drain is in progress, or the queue is non-empty. One greenlet at a
    # time owns the app; the queue is drained strictly in order on that greenlet.
    #
    # COLLAPSE. A backlog of ``step`` messages (only possible after a hold expiry, a hold the
    # scheduler ignored, or a plan slower than hold + grace) is collapsed to the NEWEST
    # step. The scheduler publishes step N+1 only after step N's barrier ended, so every
    # step but the newest queued one is already in the past for it: a reply to it is
    # never tallied (``_handle_reply`` counts ``time_step == self.time`` only), and replaying
    # it would only run a stale dispatch tick on a stale clock and push the newest step
    # past ``STEP_TIMEOUT`` again (review B1). A superseded step is still ANSWERED, in
    # order, with orsim's ``completed`` shape and ``did_step: False`` plus
    # ``superseded_by: <newest step>`` -- no ``bootstrap_step`` (the clock never moves
    # backwards), no app work, no cadence ``random()`` draw. The answer keeps one reply per
    # delivered step; it can only reinstate a pruned agent (as any late reply does), which
    # the newest step's reply would do anyway. A step at or below the newest step already
    # handled is likewise answered as superseded. ``shutdown`` (it also supersedes the
    # steps before it: the run is ending) and every other non-step message are NEVER
    # dropped; they are handled in order. A run that never declares planning never queues.

    _draining = 0  # drains in progress (a count, so an overlapping finally cannot clear it)
    _newest_step_handled = None

    def _busy(self):
        return bool(self._planning_active or self._draining or self._pending_messages)

    def _enqueue(self, payload):
        if self._pending_messages is None:
            self._pending_messages = deque()
        self._pending_messages.append(payload)

    def on_receive_message(self, client, userdata, message):
        if self._busy() and self.message_handlers.get(message.topic) == self.handle_orsim_agent_message:
            # Queue WITHOUT orsim's per-message bookkeeping (``start_time``,
            # ``message_processing_active``, ``step_log``): those belong to the step the
            # owning greenlet is processing, and resetting them would stretch its heartbeat
            # window and wipe its step log.
            try:
                payload = json.loads(message.payload.decode("utf-8"))
            except Exception:
                return super().on_receive_message(client, userdata, message)
            self._enqueue(payload)
            return None
        return super().on_receive_message(client, userdata, message)

    def handle_orsim_agent_message(self, payload):
        if self._busy():
            # Never two steps on one app: handled, in order, by the owning greenlet.
            self._enqueue(payload)
            self.add_step_log("queued behind a planning step")
            return
        self._dispatch_message(payload)

    def _dispatch_message(self, payload):
        """One message on the (exclusive) owner: a planning step, or orsim's handler."""
        time_step = payload.get("time_step") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and payload.get("action") == "step" and type(time_step) is int:
            if self._newest_step_handled is None or time_step > self._newest_step_handled:
                self._newest_step_handled = time_step
        if not (isinstance(payload, dict) and payload.get("action") == "step"
                and self._planning_due_for(time_step) is not None):
            return super().handle_orsim_agent_message(payload)
        self._start_planning_step(payload)

    def _start_planning_step(self, payload):
        """Publish the hold and hand the step to a greenlet; inline fallback on any failure.

        Review minor 7: an exception here (``bootstrap_step``, the spec, the import, the
        spawn) would otherwise escape the paho callback with ``_planning_active`` set, and the
        agent would queue every later message for good with its heartbeat suppressed. Every
        failure before the greenlet exists clears the flag and runs the step through
        orsim's own handler instead (which answers ``completed`` -- releasing any hold that
        did go out -- or orsim's ``error`` reply).
        """
        started = self.start_time
        try:
            import eventlet  # orsim's rule: never at module top (monkey patching)

            self.add_step_log("In handle_orsim_agent_message (planning step)")
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
            # V-m2: the scheduler's hold runs from (about) this instant; the planning tick
            # budgets its solve from here, not from after the app's reads.
            self._planning_hold_started_s = time.monotonic()
            try:
                self.messenger.client.publish(self._scheduler_topic(), json.dumps(hold))
            except Exception:
                logging.exception("AssignmentAgent %s: could not publish the planning hold for step %s",
                                  self.unique_id, self.current_time_step)
            self._planning_gt = eventlet.spawn(self._planning_step, payload, started)
        except Exception:
            logging.exception("AssignmentAgent %s: could not start the planning step for %r; "
                              "running it inline", self.unique_id, payload)
            self._planning_active = False
            self._planning_gt = None
            return super().handle_orsim_agent_message(payload)
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

    def _next_pending(self):
        """Pop the next queued message; ``(payload, newest)`` -- ``newest`` is ``None`` to handle
        it, else the step that supersedes it (a later queued step/shutdown, or one handled)."""
        queue = self._pending_messages
        payload = queue.popleft()
        step = payload.get("time_step") if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and payload.get("action") == "step") or type(step) is not int:
            return payload, None
        later = [m.get("time_step") for m in queue
                 if isinstance(m, dict) and m.get("action") in ("step", "shutdown")]
        newest = max((t for t in later if type(t) is int), default=None)
        if newest is not None and newest > step:
            return payload, newest
        if self._newest_step_handled is not None and step <= self._newest_step_handled:
            return payload, self._newest_step_handled
        return payload, None

    def _answer_superseded(self, payload, newest):
        """orsim's ``completed`` reply for a step collapsed into ``newest`` (no work, no draw)."""
        step = payload.get("time_step") if isinstance(payload, dict) else None
        response_payload = {
            "agent_id": self.unique_id,
            "time_step": step,
            "action": "completed" if self._shutdown == False else "shutdown",  # noqa: E712 (orsim's)
            "did_step": False,
            "run_time": 0.0,
            "superseded_by": newest,
        }
        logging.warning("AssignmentAgent %s: step %s superseded by step %s in the queue behind a "
                        "planning step; answered without work", self.unique_id, step, newest)
        try:
            self.messenger.client.publish(self._scheduler_topic(), json.dumps(response_payload))
        except Exception:
            logging.exception("AssignmentAgent %s: could not answer superseded step %s",
                              self.unique_id, step)

    def _drain_pending_messages(self):
        """Handle the queued messages in order on THIS greenlet; arrivals meanwhile queue.

        Stops when a drained message starts a new planning step (its greenlet drains the
        rest) or the queue is empty.
        """
        self._draining += 1
        try:
            while self._pending_messages and not self._planning_active:
                payload, newest = self._next_pending()
                if newest is not None:
                    self._answer_superseded(payload, newest)
                    continue
                self.start_time = time.time()
                self.message_processing_active = True
                self.payload_cache = payload
                self.reset_step_log()
                try:
                    self._dispatch_message(payload)
                except Exception:
                    # orsim's handler catches the step's own errors; this is a failure
                    # outside it (e.g. the reply publish). Keep the queue moving.
                    logging.exception("AssignmentAgent %s: a queued message failed: %r",
                                      self.unique_id, payload)
                    self.message_processing_active = False
                if self._planning_active:
                    break
        finally:
            self._draining -= 1

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
        held = getattr(self, "_planning_hold_started_s", None)
        self._planning_hold_started_s = None
        try:
            if held is None:
                app.plan(self.get_current_time_str(), self.current_time_step, due)
            else:
                app.plan(self.get_current_time_str(), self.current_time_step, due, hold_started_s=held)
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
