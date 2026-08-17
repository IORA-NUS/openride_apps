"""
OpenRideAgent — app-side base for container_logistics agents.

Carries the MQTT-socket leak guards that previously lived as edits inside
``orsim/lifecycle/orsim_agent.py`` and ``orsim/tasks/agent_task.py``, so the orsim
library stays pristine (CLAUDE.md §7).

Two leaks are covered:

1. **Orphaned live agents.** Agents are pruned by the scheduler on a step timeout,
   and whole runs get killed/restarted, without the agent ever receiving a
   'shutdown' action — so ``_shutdown`` stays False and the heartbeat greenlet holds
   the broker connection forever. Across runs these accumulate until the host runs
   out of ephemeral source ports (connect() → EADDRINUSE). The scheduler publishes a
   step/shutdown to every live agent each step, so silence on the run topic past
   ``ORPHAN_TIMEOUT`` means the run is gone and the agent can reclaim itself. We hook
   this into ``handle_heartbeat_failure`` (called by the existing heartbeat greenlet
   every interval, which then sees ``_shutdown`` and stops listening) rather than
   editing the greenlet itself.

2. **Failed-init agents.** A successful agent is disconnected by its heartbeat
   greenlet; an agent that failed to initialise never spawns that greenlet, so
   nothing releases its broker connection. We tear it down right after
   ``start_listening`` returns.

It also carries the agent half of the **step-barrier sleep protocol** (the
scheduler half lives in ``apps/simulation/openride_scheduler.py``): an agent that
has provably nothing to do for a while (e.g. an unassigned order waiting on the
assignment service) announces a nap on its step reply and stops answering step
broadcasts, so neither the barrier nor the reply flood pays for it. Two nap
flavours:

- ``sleep_steps: K`` (K > 0) — timed nap. The agent stays subscribed to the
  step broadcast, swallows the next K-1 broadcasts locally (no processing, no
  reply) and wakes at the announced step, or earlier when ``wake_signal`` says
  a queued event arrived.
- ``sleep_steps: -1`` — indefinite, event-driven nap. The agent additionally
  UNSUBSCRIBES from the step-broadcast topic, so RabbitMQ stops fanning the
  per-step message out to it at all (with 30k cumulative order agents that
  fanout was itself a per-step cost that grew all run). It re-subscribes the
  moment anything lands on its own app topic — a workflow event (assignment,
  cancellation) or the scheduler's direct ``ctrl: scheduler_direct`` shutdown
  notice sent to sleepers on the final step — and answers the next broadcast;
  the reply also carries ``wake_topic`` so the scheduler knows where to send
  that direct notice.

Wake latency is one step in both flavours — identical to an awake agent's event
latency. ``shutdown`` broadcasts are never swallowed. Subclasses opt in by
overriding ``sleep_steps_hint`` (and ``wake_signal``); the default never sleeps.

Finally, ``handle_orsim_agent_message`` gates zombie steps: an agent that already
shut down (reaped, or finished its lifecycle) but still receives broadcasts while
its heartbeat greenlet tears the connection down used to KEEP PROCESSING them —
completed orders re-ran entering_market → app.launch → a doomed publish PATCH on
every remaining broadcast ("Can't publish when in completed" API error flood,
plus pointless load slowing live agents). Shut-down agents now ignore step
broadcasts entirely.

Because stock orsim publishes the step reply deep inside
``handle_orsim_agent_message`` with no hook to amend the payload, that method is
overridden wholesale here (same pattern as OpenRideScheduler) — keep the orsim
version pinned and rely on the agent tests to catch contract drift.
"""

import json
import logging
import time
import traceback

from orsim.lifecycle import ORSimAgent


class OpenRideAgent(ORSimAgent):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Last time a message was received from the scheduler on this agent's run
        # topic. Used as a liveness signal: if it goes stale the run has ended or
        # this agent was pruned, and we tear down to release the MQTT connection.
        self._last_message_time = time.time()
        # Step (exclusive) until which this agent promised the scheduler it will
        # not answer step broadcasts. None = awake; math.inf = indefinite nap.
        self._sleep_until_step = None
        self._broadcast_topic = f"{self.run_id}/{self.scheduler_id}/ORSimAgent"
        self._broadcast_subscribed = True
        # Wrap the app-topic handler(s) so an event can wake an indefinitely
        # sleeping agent (resubscribe to the broadcast) and so the scheduler's
        # direct control notice to sleepers is processed immediately.
        self._app_topic = None
        for topic, method in list(getattr(self, "message_handlers", {}).items()):
            if topic == self._broadcast_topic:
                continue
            self._app_topic = topic
            self.message_handlers[topic] = self._wrap_app_topic_handler(method)

    def _wrap_app_topic_handler(self, inner):
        def _handler(payload):
            if isinstance(payload, dict) and payload.get('ctrl') == 'scheduler_direct':
                # Direct scheduler notice (only sent to sleeping agents, e.g.
                # the final-step shutdown): treat exactly like a broadcast.
                self._resubscribe_broadcast()
                self.handle_orsim_agent_message(payload)
                return
            inner(payload)
            if self._sleep_until_step is not None and not self._broadcast_subscribed:
                self._resubscribe_broadcast()
        return _handler

    def _resubscribe_broadcast(self):
        if not self._broadcast_subscribed:
            try:
                self.messenger.client.subscribe(self._broadcast_topic, qos=0)
                self._broadcast_subscribed = True
            except Exception:
                logging.exception(f"Agent {self.unique_id}: broadcast resubscribe failed")

    def _unsubscribe_broadcast(self):
        if self._broadcast_subscribed:
            try:
                self.messenger.client.unsubscribe(self._broadcast_topic)
                self._broadcast_subscribed = False
            except Exception:
                logging.exception(f"Agent {self.unique_id}: broadcast unsubscribe failed")

    # ── Sleep protocol hooks ────────────────────────────────────────────────
    def sleep_steps_hint(self) -> int:
        """
        Nap request, announced to the scheduler via ``sleep_steps`` on the reply:
        0 = stay awake, K > 0 = timed nap of K steps, -1 = indefinite
        event-driven nap (also unsubscribes from the step broadcast). Called
        after a step is fully processed. Override in subclasses that can prove
        idleness (see OrderAgent).
        """
        return 0

    def wake_signal(self) -> bool:
        """True when something arrived that justifies waking before the timer."""
        return False

    def wake_signal_for_step(self, time_step: int) -> bool:
        """
        Step-aware wake check used by the sleep gate — ``time_step`` is the
        broadcast's step, needed because a sleeping agent's own clock is stale.
        Defaults to the plain wake_signal; subclasses add step-based conditions
        (e.g. an order's bounded unassigned wait).
        """
        return self.wake_signal()

    def handle_orsim_agent_message(self, payload):
        """
        Wholesale override of ORSimAgent.handle_orsim_agent_message (orsim==1.2.1)
        adding the sleep protocol; the try/except structure, response payloads and
        publish are otherwise byte-equivalent to the orsim version.
        """
        action = payload.get('action')

        # Zombie gate: an agent that already shut down (reaped, or finished its
        # lifecycle) still receives broadcasts until its heartbeat greenlet tears
        # the connection down. Re-processing them re-ran entering_market →
        # app.launch → doomed publish PATCHes every step (API error flood).
        if action == 'step' and self._shutdown:
            self.message_processing_active = False
            return

        # Sleep gate: swallow step broadcasts while sleeping, unless a queued
        # event wakes us early. Shutdown broadcasts are always processed.
        if action == 'step' and self._sleep_until_step is not None:
            time_step = payload.get('time_step')
            if (
                isinstance(time_step, int)
                and time_step < self._sleep_until_step
                and not self.wake_signal_for_step(time_step)
            ):
                self.add_step_log('sleeping — step swallowed')
                # An indefinite sleeper only hears a broadcast because the
                # heartbeat resubscribed it for a liveness refresh (see
                # handle_heartbeat_failure); receiving this one did the job —
                # drop back out of the fanout.
                if self._sleep_until_step == float('inf') and self._broadcast_subscribed:
                    self._unsubscribe_broadcast()
                # on_receive_message flagged us busy; clear it so the heartbeat
                # greenlet doesn't count sleep as stuck-processing time.
                self.message_processing_active = False
                return
            self._sleep_until_step = None

        self.add_step_log('In handle_orsim_agent_message')

        response_payload = None
        try:
            self.bootstrap_step(payload['time_step'])

            if action == 'init':
                ''' NOTE This is unused block of code at the moment'''
                did_step = self.process_payload(payload)
                self.next_event_time = self.estimate_next_event_time()

                response_payload = {
                    'agent_id': self.unique_id,
                    'time_step': self.current_time_step,
                    'action': 'ready',
                    'did_step': did_step,
                    'run_time': time.time() - self.start_time,
                }
            elif action == 'step':

                did_step = self.process_payload(payload)
                self.next_event_time = self.estimate_next_event_time()

                response_payload = {
                    'agent_id': self.unique_id,
                    'time_step': self.current_time_step,
                    'action': 'completed' if self._shutdown == False else 'shutdown',
                    'did_step': did_step,
                    'run_time': time.time() - self.start_time,
                }

                # Sleep protocol: announce a nap when the subclass can prove
                # idleness. Never on a shutdown reply.
                if not self._shutdown:
                    try:
                        sleep_steps = int(self.sleep_steps_hint() or 0)
                    except Exception:
                        sleep_steps = 0
                    if sleep_steps > 0:
                        self._sleep_until_step = self.current_time_step + sleep_steps
                        response_payload['sleep_steps'] = sleep_steps
                    elif sleep_steps < 0:
                        # Indefinite event-driven nap: drop out of the step
                        # broadcast fanout entirely; the app-topic wrapper (or
                        # the scheduler's direct notice to wake_topic) brings
                        # us back.
                        self._sleep_until_step = float('inf')
                        response_payload['sleep_steps'] = -1
                        if self._app_topic:
                            response_payload['wake_topic'] = self._app_topic
                        self._unsubscribe_broadcast()
            elif action == 'shutdown':
                ''' '''
                response_payload = {
                    'agent_id': self.unique_id,
                    'time_step': self.current_time_step,
                    'action': 'shutdown',
                    'did_step': True,
                    'run_time': time.time() - self.start_time,
                }
        except Exception:
            response_payload = {
                'agent_id': self.unique_id,
                'time_step': self.current_time_step,
                'action': 'error',
                'did_step': False,
                'run_time': time.time() - self.start_time,
                'details': traceback.format_exc(),
            }

        if response_payload is not None:
            self.messenger.client.publish(f'{self.run_id}/{self.scheduler_id}/ORSimScheduler', json.dumps(response_payload))

        if action == 'shutdown':
            self.shutdown()

        self.end_time = time.time()
        self.message_processing_active = False

    def on_receive_message(self, client, userdata, message):
        super().on_receive_message(client, userdata, message)
        self._last_message_time = time.time()

    def handle_heartbeat_failure(self):
        super().handle_heartbeat_failure()

        orphan_timeout = self.orsim_settings.get('ORPHAN_TIMEOUT', 180)

        # An indefinitely-sleeping agent unsubscribed from the step broadcast, so
        # nothing refreshes _last_message_time on a quiet order topic and the
        # orphan guard below would tear down every sleeper after ORPHAN_TIMEOUT
        # (20k+ falsely-orphaned orders on the first full-scale run: their docs
        # stayed 'unassigned' and the assignment service re-matched them forever).
        # Refresh liveness by resubscribing halfway to the deadline: the next
        # broadcast bumps _last_message_time (the sleep gate still swallows it —
        # no reply, the scheduler's ledger is untouched) and the gate then drops
        # the subscription again. A genuinely dead run delivers no broadcast, so
        # the orphan teardown still fires at the full deadline.
        if (
            not self._shutdown
            and self._sleep_until_step is not None
            and not self._broadcast_subscribed
            and (time.time() - self._last_message_time) > orphan_timeout / 2
        ):
            self._resubscribe_broadcast()

        # Backstop against MQTT connection leaks (see module docstring). The heartbeat
        # greenlet calls this every HEARTBEAT_INTERVAL and breaks/stop_listening once
        # _shutdown flips True, so calling shutdown() here is enough to reclaim us.
        if not self._shutdown and (time.time() - self._last_message_time) > orphan_timeout:
            logging.warning(
                f"Agent {self.unique_id}: no scheduler message for "
                f"{orphan_timeout}s — assuming run ended, tearing down."
            )
            try:
                self.shutdown()
            except Exception as e:
                logging.exception(f"Agent {self.unique_id} orphan shutdown error: {e}")
                self._shutdown = True

    def start_listening(self):
        super().start_listening()

        # An agent that failed to initialise never spawned the heartbeat greenlet that
        # would otherwise disconnect it, so release its broker connection explicitly.
        if getattr(self, "agent_failed", False):
            try:
                self.stop_listening()
            except Exception:
                logging.exception(
                    f"Agent {getattr(self, 'unique_id', '?')}: failed to disconnect messenger for failed agent"
                )
