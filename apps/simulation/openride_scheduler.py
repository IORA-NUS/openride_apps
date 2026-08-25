"""
OpenRideScheduler — app-side subclass of orsim's ORSimScheduler.

This is where the container_logistics customizations of the step barrier live, so
that `orsim/` itself stays pristine (CLAUDE.md §7: never modify the external orsim
library — optimize *around* it). Three concerns are carried here:

1. **Performance.** Stock ORSimScheduler re-tallies the whole agent_collection on
   every MQTT reply and keeps every step's response for every agent forever. At
   10k+ in-market agents that made the scheduler process itself the wall-clock
   bottleneck (~0.12 ms of main-thread work per reply: one json.loads + one
   call_soon_threadsafe + one event wakeup each, plus several O(N) passes per
   step and unbounded step_response history driving GC pressure). The hot path
   here is flat: replies are parsed (orjson when available) and tallied entirely
   on the paho-mqtt thread with plain counter/set updates, the event loop is
   woken ONCE per step when the barrier resolves, per-agent history is capped at
   the last two steps (the perf slow-agent reader needs step-1), and the only
   remaining O(N) pass is the expected-set build at step start.

2. **Robustness.** ``pending_boot`` excludes async-Celery-booting agents from the
   barrier and the shutdown sweep; a step timeout no longer aborts the run on a
   single bad step but only after ``STEP_TIMEOUT_ABORT_CONSECUTIVE`` consecutive
   over-tolerance steps.

3. **Agent sleep protocol.** An agent whose reply carries ``sleep_steps: K``
   promises not to reply to the next K-1 step broadcasts (it still receives
   them and may wake itself early — e.g. an order woken by an assignment event
   — in which case its unsolicited reply clears the sleep here). Sleeping
   agents are excluded from the barrier AND from the unresponsive-agent sweep.
   This is what keeps a 30k-order run's per-step reply flood bounded by the
   *active* population instead of every order ever spawned. The agent side
   lives in OpenRideAgent.handle_orsim_agent_message / sleep_steps_hint.

Because stock orsim exposes no finer hooks, ``on_receive_message``, ``step``,
``confirm_responses``, ``_update_agent_stat`` and ``step_timeout_handler`` are
overridden wholesale. This duplicates orsim internals — keep the orsim version
pinned and rely on the scheduler tests to catch contract drift.
"""

import asyncio
import json
import logging
import threading
import time

try:  # ~10x faster reply parsing on the paho thread; stdlib fallback keeps deploys safe.
    import orjson as _fastjson
except ImportError:  # pragma: no cover
    _fastjson = None

from orsim import ORSimScheduler
from orsim.core import ORSimEnv
from orsim.tasks import start_agent


def _parse_reply(raw: bytes):
    if _fastjson is not None:
        return _fastjson.loads(raw)
    return json.loads(raw.decode("utf-8"))


class OpenRideScheduler(ORSimScheduler):

    def __init__(self, run_id, scheduler_id, orsim_settings, init_failure_handler='soft'):
        super().__init__(run_id, scheduler_id, orsim_settings, init_failure_handler=init_failure_handler)

        # Number of consecutive steps whose unresponsive-agent fraction exceeded
        # STEP_TIMEOUT_TOLERANCE. A single bad step no longer aborts the run; we
        # only give up after STEP_TIMEOUT_ABORT_CONSECUTIVE such steps in a row.
        self._consecutive_step_timeouts = 0

        # Reply topic, precomputed for the hot on_receive_message check.
        self._reply_topic = f"{self.run_id}/{self.scheduler_id}/ORSimScheduler"

        # ── Step-barrier state ───────────────────────────────────────────────
        # All of these are written by the paho-mqtt thread in _handle_reply and
        # read by the event-loop thread. Individual set/dict/int operations are
        # GIL-atomic; step() swaps in fresh objects before publishing the step
        # message, so replies for step T only ever touch step T's objects
        # (stragglers are filtered by the time_step == self.time check).
        self._expected_this_step: set = set()
        self._responded_this_step: set = set()
        self._outstanding: int = 0
        self._reaction_counts: dict = {}
        self._did_step_count: int = 0
        self._shutdown_replied: set = set()
        self._error_replied: set = set()
        self._booting_count_this_step: int = 0
        self._sleeping_count_this_step: int = 0

        # ── Sleep protocol state ─────────────────────────────────────────────
        # agent_id -> wake step (float('inf') = indefinite, event-driven nap).
        # Written by the paho thread (replies carrying sleep_steps / early-wake
        # replies), iterated+pruned by step(); the lock covers those structural
        # mutations. _sleep_wake_topics holds, per indefinitely-sleeping agent,
        # its own topic — used for the direct shutdown notice on the final step
        # (those agents unsubscribed from the step broadcast).
        self._sleep_lock = threading.Lock()
        self._sleeping_until: dict = {}
        self._sleep_wake_topics: dict = {}

        # ── Straggler reinstatement ──────────────────────────────────────────
        # Agents pruned by the tolerance early-exit / step timeout are usually
        # ALIVE, just late (boot storms, a busy worker) — removal used to lose
        # ~5% of the fleet on a cold start, silently shrinking the experiment.
        # Stash pruned entries briefly; a late 'completed'/'ready' reply
        # re-registers the agent (a real corpse never replies again).
        self._pruned_stash: dict = {}

    _PRUNED_STASH_MAX = 4096

    @property
    def sleeping_agent_count(self) -> int:
        """Registered agents currently in an announced nap (see sleep protocol)."""
        return len(self._sleeping_until)

    def add_agent(self, spec, project_path, agent_class):
        """
        Adds a new agent and launches it as a Celery task.

        Identical to ORSimScheduler.add_agent except the agent is marked
        ``pending_boot`` until its first MQTT reply — Celery boot is async, so a
        freshly added agent is not yet expected to answer the step barrier.
        """
        self.agent_collection[spec['unique_id']] = {
            'spec': spec,
            # Celery boot is async; exclude from confirm_responses until first MQTT reply.
            'pending_boot': True,
            'step_response': {
                self.time: {
                    'reaction': 'waiting',
                    'did_step': False,
                    'run_time': 0,
                }
            }
        }

        module_comp = agent_class.split('.')
        module_name, agent_class_name = str.join('.', module_comp[:-1]), module_comp[-1]

        kwargs = spec.copy()
        kwargs['scheduler'] = {
            'id': self.scheduler_id,
            'orsim_settings': self.orsim_settings
        }
        start_agent.delay(project_path, module_name, agent_class_name, ORSimEnv.messenger_settings, **kwargs)

        logging.info(f"agent {spec['unique_id']} entering market")
        print(f"agent {spec['unique_id']} entering market")

    def remove_agent(self, unique_id):
        super().remove_agent(unique_id)
        if unique_id in self._sleeping_until or unique_id in self._sleep_wake_topics:
            with self._sleep_lock:
                self._sleeping_until.pop(unique_id, None)
                self._sleep_wake_topics.pop(unique_id, None)

    def on_receive_message(self, client, userdata, message):
        """
        Paho-thread entry point for agent replies. Parses and hands off to
        _handle_reply; deliberately does NO event-loop hops (see _handle_reply).
        """
        if message.topic != self._reply_topic:
            return
        try:
            payload = _parse_reply(message.payload)
        except Exception:
            logging.warning(f"{self.__class__.__name__} received unparseable reply payload")
            return
        self._handle_reply(payload)

    def _handle_reply(self, payload: dict):
        """
        Records one agent reply and feeds the barrier counters. Runs on the
        paho-mqtt thread; the ONLY cross-thread signal is a single
        call_soon_threadsafe(event.set) when the barrier resolves, so the event
        loop is woken once per step instead of once per reply.
        """
        agent_id = payload.get('agent_id')
        response_time_step = payload.get('time_step')
        if response_time_step == -1:
            response_time_step = self.time
        action = payload.get('action')

        entry = self.agent_collection.get(agent_id)
        if entry is None and action in ('completed', 'ready'):
            stashed = self._pruned_stash.pop(agent_id, None)
            if stashed is not None:
                entry = stashed
                self.agent_collection[agent_id] = entry
                logging.info(f"agent {agent_id} reinstated after late reply (was pruned as unresponsive)")
        if entry is not None:
            entry['pending_boot'] = False
            if isinstance(response_time_step, int):
                sr = entry['step_response']
                if len(sr) > 1:
                    # Cap history at {previous, current}: perf's slow-agent reader
                    # needs step-1; anything older is dead weight (unbounded growth
                    # was ~GBs of dicts + GC pressure on multi-day runs).
                    latest = max(sr)
                    entry['step_response'] = sr = {latest: sr[latest]}
                sr[response_time_step] = {
                    'reaction': action,
                    'did_step': payload.get('did_step'),
                    'run_time': payload.get('run_time'),
                }

        if (action == 'error') or (response_time_step is None):
            logging.warning(f'{self.__class__.__name__} received {payload = }')

        # ── Sleep protocol bookkeeping ───────────────────────────────────────
        sleep_steps = payload.get('sleep_steps')
        if sleep_steps:
            sleep_steps = int(sleep_steps)
            base = response_time_step if isinstance(response_time_step, int) else self.time
            with self._sleep_lock:
                if sleep_steps < 0:
                    # Indefinite, event-driven nap: the agent unsubscribed from
                    # the step broadcast; remember where to send the final-step
                    # direct shutdown notice.
                    self._sleeping_until[agent_id] = float('inf')
                    wake_topic = payload.get('wake_topic')
                    if wake_topic:
                        self._sleep_wake_topics[agent_id] = wake_topic
                else:
                    self._sleeping_until[agent_id] = base + sleep_steps
        elif agent_id in self._sleeping_until:
            # Unsolicited reply from a sleeping agent — it woke itself early
            # (queued event). Not counted for this step's barrier (it wasn't
            # expected); it becomes a normal responder from the next step on.
            with self._sleep_lock:
                self._sleeping_until.pop(agent_id, None)
                self._sleep_wake_topics.pop(agent_id, None)

        if response_time_step == self.time:
            if action == 'shutdown':
                # Reap even when the reply was not expected this step (e.g. a
                # woken sleeper that reached a terminal state).
                self._shutdown_replied.add(agent_id)
            if action == 'error':
                self._error_replied.add(agent_id)

            if (
                agent_id in self._expected_this_step
                and agent_id not in self._responded_this_step
            ):
                self._responded_this_step.add(agent_id)
                if payload.get('did_step'):
                    self._did_step_count += 1
                self._reaction_counts[action] = self._reaction_counts.get(action, 0) + 1
                self._outstanding -= 1
                if self._outstanding <= 0:
                    self._signal_barrier_done()

    def _signal_barrier_done(self):
        loop = self._loop
        event = self._response_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # Event loop already closed (run teardown) — nothing to wake.
                pass

    def _begin_step_barrier(self, is_final: bool = False):
        """
        Resets the per-step barrier state for the current self.time. Excluded
        from the expected set:
          - booting agents (async Celery boot not yet acknowledged), and
          - sleeping agents (announced sleep_steps; they will not reply).
        Sleeping agents stay excluded even on the final step — indefinite
        sleepers unsubscribed from the broadcast, so waiting on them would
        always run into STEP_TIMEOUT. step() direct-notifies them instead, and
        their (unexpected) shutdown replies reap them via _shutdown_replied.
        """
        with self._sleep_lock:
            if self._sleeping_until:
                expired = [aid for aid, wake in self._sleeping_until.items() if wake <= self.time]
                for aid in expired:
                    del self._sleeping_until[aid]
                    self._sleep_wake_topics.pop(aid, None)
            sleeping_now = set(self._sleeping_until)

        expected = set()
        booting = 0
        for aid, item in self.agent_collection.items():
            if item.get('pending_boot'):
                booting += 1
            elif aid not in sleeping_now:
                expected.add(aid)

        self._booting_count_this_step = booting
        self._sleeping_count_this_step = len(sleeping_now)
        self._responded_this_step = set()
        self._reaction_counts = {}
        self._did_step_count = 0
        self._shutdown_replied = set()
        self._error_replied = set()
        self._expected_this_step = expected
        self._outstanding = len(expected)

    def _update_agent_stat(self) -> int:
        """
        Publishes current step counters into self.agent_stat and returns the
        waiting count. O(1): every number is maintained incrementally by
        _handle_reply / the step-start baseline.
        """
        waiting = self._outstanding if self._outstanding > 0 else 0
        self.agent_stat[self.time] = {
            'completed': self._reaction_counts.get('completed', 0),
            'ready': self._reaction_counts.get('ready', 0),
            'error': self._reaction_counts.get('error', 0),
            'shutdown': self._reaction_counts.get('shutdown', 0),
            'waiting': waiting,
            'booting': self._booting_count_this_step,
            'sleeping': self._sleeping_count_this_step,
            'stepping_agents': self._did_step_count,
            'total_agents': len(self.agent_collection),
        }
        return waiting

    async def confirm_responses(self):
        """
        Waits for all expected agents to respond for the current time step.

        The paho thread maintains ``self._outstanding`` and wakes this coroutine
        exactly once, when the count hits zero; a short timeout on the wait acts
        as a safety net (missed wakeup, and the tolerance-based early exit for
        straggler steps re-checks on that cadence).

        The expected/responded baseline for this step is established in step()
        before the step message is published (so replies are tallied against a
        fresh set), not here.
        """
        start_time = time.time()
        confirm_start = start_time
        log_base = 0

        total = len(self.agent_collection)
        tolerance = self.orsim_settings.get('STEP_TIMEOUT_TOLERANCE', 0.0)
        settle_seconds = self.orsim_settings.get('STEP_SETTLE_SECONDS', 2)

        def _done():
            if self._outstanding <= 0:
                return True
            if total > 0 and (self._outstanding / total) <= tolerance and (time.time() - confirm_start) >= settle_seconds:
                logging.info(
                    f"confirm_responses: {self._outstanding}/{total} agents still waiting after "
                    f"{settle_seconds}s settle ({self._outstanding / total:.1%} <= tolerance "
                    f"{tolerance:.1%}); pruning stragglers and continuing early."
                )
                return True
            return False

        while not _done():
            elapsed = time.time() - start_time
            if elapsed >= 5:
                self._update_agent_stat()
                logging.info(f"Waiting for Agent Response... {self.agent_stat[self.time]}: {log_base + elapsed:0.0f} sec")
                log_base += elapsed
                start_time = time.time()

            self._response_event.clear()
            # Re-check after clearing: the final reply may have landed between
            # the check and the clear.
            if _done():
                break

            # Short timeout fallback: re-check if the single barrier-done wakeup
            # was missed, and give the tolerance early-exit a polling cadence.
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                continue

        # Authoritative tally once the barrier resolves (O(1) now).
        self._update_agent_stat()

    async def step(self, is_final: bool = False):
        """
        Advances the simulation by one step, coordinating agent actions and handling timeouts.

        Args:
            is_final: When True, sends action='shutdown' to agents instead of action='step'.
                      Determined externally by the TerminationCondition so the scheduler does
                      not need to know the total simulation length.
        """

        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._response_event = asyncio.Event()

        logging.info(f"{self.scheduler_id} Step: {self.time}")
        start_time = time.time()

        self._response_event.clear()

        # Fallback: honour SIMULATION_LENGTH_IN_STEPS if the caller did not set is_final.
        # Disabled when post-horizon drain is enabled — the runtime owns is_final then.
        if (
            not is_final
            and not self.orsim_settings.get("ALLOW_POST_HORIZON_DRAIN", False)
            and self.time == self.orsim_settings['SIMULATION_LENGTH_IN_STEPS'] - 1
        ):
            is_final = True

        if is_final:
            message = {'action': 'shutdown', 'time_step': self.time}
        else:
            message = {'action': 'step', 'time_step': self.time}

        # Establish the incremental barrier baseline BEFORE publishing — replies
        # for this step only start arriving after the publish below, so they are
        # tallied against a fresh, correct expected-set.
        self._begin_step_barrier(is_final=is_final)

        if is_final:
            # Indefinite sleepers unsubscribed from the step broadcast — send
            # each its direct shutdown notice so it logs out and releases its
            # MQTT connection now instead of via the orphan-timeout backstop.
            with self._sleep_lock:
                wake_topics = dict(self._sleep_wake_topics)
            if wake_topics:
                logging.info(
                    f"{self.scheduler_id}: direct-notifying {len(wake_topics)} sleeping agents of shutdown"
                )
                notice = json.dumps(
                    {'action': 'shutdown', 'time_step': self.time, 'ctrl': 'scheduler_direct'}
                )
                for topic in wake_topics.values():
                    try:
                        self.agent_messenger.client.publish(topic, notice)
                    except Exception:
                        logging.exception(f"direct shutdown notice publish failed for {topic}")

        self.agent_messenger.client.publish(f'{self.run_id}/{self.scheduler_id}/ORSimAgent', json.dumps(message))

        try:
            await asyncio.wait_for(self.confirm_responses(), timeout=self.orsim_settings['STEP_TIMEOUT'])
            # A clean step clears the consecutive-timeout streak used by the abort guard.
            self._consecutive_step_timeouts = 0
            logging.info(f'{self.agent_stat[self.time] = }')

        except asyncio.TimeoutError as e:
            logging.exception(f'Scheduler {self.scheduler_id} timeout beyond {self.orsim_settings["STEP_TIMEOUT"] = } while waiting for confirm_responses.')
            # Decides whether to prune-and-continue or abort. Unresponsive agents are
            # removed by the shutdown sweep below whenever this does not re-raise.
            self.step_timeout_handler(e)

        # Reap agents that asked to shut down or went silent this step. Booting
        # agents keep pending_boot until their first reply and sleeping agents
        # announced their silence — neither is in the expected set, so neither
        # can be reaped here.
        non_responders = self._expected_this_step - self._responded_this_step
        for agent_id in (self._shutdown_replied | non_responders):
            if agent_id in self.agent_collection:
                if agent_id in non_responders and agent_id not in self._shutdown_replied:
                    # Pruned as unresponsive, not shut down — stash for
                    # reinstatement if a late reply proves it alive.
                    self._pruned_stash[agent_id] = self.agent_collection[agent_id]
                    while len(self._pruned_stash) > self._PRUNED_STASH_MAX:
                        self._pruned_stash.pop(next(iter(self._pruned_stash)))
                self.remove_agent(agent_id)

        if is_final:
            # Sleeping agents were direct-notified above; deregister whatever is
            # left of them regardless of reply timing — the run is over.
            with self._sleep_lock:
                leftover_sleepers = list(self._sleeping_until)
            for agent_id in leftover_sleepers:
                if agent_id in self.agent_collection:
                    self.remove_agent(agent_id)

        self.time += 1

        sim_stat = {
            'status': 'success',
            'end_sim': is_final,
        }

        logging.info(f'{self.scheduler_id} Runtime: {(time.time()-start_time):0.2f} sec')
        return sim_stat

    def _non_responding_agent_ids(self):
        """IDs of agents that did not respond ('waiting') or errored on the current step."""
        return sorted((self._expected_this_step - self._responded_this_step) | self._error_replied)

    def step_timeout_handler(self, e):
        """
        Handles a step timeout (agents did not all respond within STEP_TIMEOUT).

        A single bad step is no longer fatal: agents that failed to respond are pruned
        by the shutdown sweep in ``step()`` whenever this method returns without raising,
        so the run continues with the remaining agents. The run is only aborted after
        ``STEP_TIMEOUT_ABORT_CONSECUTIVE`` (default 3) consecutive steps whose
        unresponsive fraction exceeded ``STEP_TIMEOUT_TOLERANCE``.
        """
        # confirm_responses was cancelled mid-await by the STEP_TIMEOUT, so its
        # final tally never ran — refresh agent_stat here so the tolerance check
        # below sees this step's real 'waiting' count rather than a stale one.
        self._update_agent_stat()

        total = len(self.agent_collection)
        stat = self.agent_stat.get(self.time, {})
        non_responders = self._non_responding_agent_ids()

        if total == 0:
            # Nothing to wait on — never fatal, and avoids a ZeroDivisionError.
            logging.warning(
                f"Step timeout at {self.time=} with an empty agent collection; continuing."
            )
            self._consecutive_step_timeouts = 0
            return

        waiting = stat.get('waiting', 0)
        waiting_fraction = waiting / total
        tolerance = self.orsim_settings['STEP_TIMEOUT_TOLERANCE']  # Max fraction unresponsive

        if waiting_fraction <= tolerance:
            self._consecutive_step_timeouts = 0
            logging.warning(
                f"Step timeout at {self.time=}: {waiting}/{total} agents unresponsive "
                f"({waiting_fraction:.1%} <= tolerance {tolerance:.1%}). "
                f"Pruning unresponsive agents and continuing. "
                f"agent_stat={stat} non_responders={non_responders}"
            )
            return

        # Over tolerance: count it. Only abort on a sustained run of bad steps.
        self._consecutive_step_timeouts += 1
        abort_after = max(
            1, int(self.orsim_settings.get('STEP_TIMEOUT_ABORT_CONSECUTIVE', 3))
        )
        logging.error(
            f"Step timeout at {self.time=}: {waiting}/{total} agents unresponsive "
            f"({waiting_fraction:.1%} > tolerance {tolerance:.1%}). "
            f"consecutive over-tolerance steps={self._consecutive_step_timeouts}/{abort_after}. "
            f"agent_stat={stat} non_responders={non_responders}"
        )

        if self._consecutive_step_timeouts >= abort_after:
            logging.error(
                f"Aborting run: {self._consecutive_step_timeouts} consecutive over-tolerance "
                f"step timeouts at {self.time=}. Final agent_collection follows."
            )
            logging.error(f'{self.pp.pformat(self.agent_collection)}')
            raise e

        logging.error(
            "Over tolerance but below the consecutive-abort threshold; pruning "
            "unresponsive agents and continuing."
        )
