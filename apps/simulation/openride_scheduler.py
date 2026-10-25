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

4. **Init failures fail the run** (``init_failure_handler='hard'``, which the
   container-logistics wiring passes; stock orsim's handling of it is commented
   out, so it was a no-op -- round-2 review L-D). Two signals, both app-side:

   - an ``init_error`` reply: an ``OpenRideAgent`` whose app raised at init now
     answers the boot handshake with it instead of ``ready``
     (``openride_agent.InitFailureReporting``);
   - an agent that NEVER answered a single step: pruned as unresponsive and still
     silent ``BOOT_DEADLINE_STEPS`` steps later. This is how a stock ``ORSimAgent``
     whose app was refused at init looks (it says ``ready``, subscribes to nothing
     and never hears a step) -- e.g. the assignment agent refusing its policy, which
     used to leave a run that finished with no assignments and ``ok: true``.

   Only the explicit ``init_error`` raises :class:`AgentInitFailure` from ``step()``
   (``'hard'``), which aborts the run as FAILED (``SimulationRuntime.run_simulation``).
   The silence signal is a HEURISTIC and is only ever logged (``AGENT NEVER STEPPED``
   at ERROR, once per agent): a healthy agent can be pruned during a slow boot on a
   large live run, and failing a good run is worse than the defect it would catch.
   Promote it to fatal only after a live 500/1000-truck run shows it never fires on a
   healthy run. ``'soft'`` only logs either signal.

5. **Bounded planning hold** (opt-in, ``orsim_settings['PLANNING']['max_hold_seconds']``).
   An agent that is about to spend longer than ``STEP_TIMEOUT`` on one step (a
   day-plan solve) announces it on the ordinary reply topic::

       {"agent_id": ..., "time_step": T, "action": "hold",
        "hold_seconds": H, "reason": "planning"}

   The hold is accepted only for the CURRENT step, from an agent that is expected
   and has not yet answered it, and is capped at ``max_hold_seconds``. While it is
   live the settle/tolerance early-exit is suspended and the barrier deadline is
   ``max(start + STEP_TIMEOUT, max(accepted_at + min(H, cap)) + STEP_TIMEOUT)`` --
   the agent gets ``STEP_TIMEOUT`` of grace AFTER its hold, because a planning
   step may still have to dispatch before it answers. Any later reply from the
   agent for that step (``completed``/``error``/``shutdown``) releases the hold.
   On expiry an ERROR ``PLANNING HOLD EXPIRED: ...`` is logged, the hold dropped,
   and the agent takes the ordinary prune/stash path (its late ``completed``
   reinstates it). With the cap 0 or absent -- every run that does not declare
   planning -- a hold is logged at WARNING and ignored, and ``step()`` runs the
   literal pre-existing ``asyncio.wait_for`` path. Held wall time is reported as
   ``last_step_hold_ms`` (and ``hold_ms`` in ``agent_stat``) so the perf summary
   can subtract it instead of reading a planning run as a regression.

Because stock orsim exposes no finer hooks, ``on_receive_message``, ``step``,
``confirm_responses``, ``_update_agent_stat`` and ``step_timeout_handler`` are
overridden wholesale. This duplicates orsim internals — keep the orsim version
pinned and rely on the scheduler tests to catch contract drift.
"""

import asyncio
import contextlib
import json
import logging
import math
import threading
import time

try:  # ~10x faster reply parsing on the paho thread; stdlib fallback keeps deploys safe.
    import orjson as _fastjson
except ImportError:  # pragma: no cover
    _fastjson = None

from orsim import ORSimScheduler
from orsim.core import ORSimEnv
from orsim.tasks import start_agent

from apps.agent_transport import require_seam, select_scheduler_transport


def _parse_reply(raw: bytes):
    if _fastjson is not None:
        return _fastjson.loads(raw)
    return json.loads(raw.decode("utf-8"))


class AgentInitFailure(RuntimeError):
    """An agent reported ``init_error`` at boot under ``init_failure_handler='hard'``."""


class OpenRideScheduler(ORSimScheduler):

    def __init__(self, run_id, scheduler_id, orsim_settings, init_failure_handler='soft', transport=None):
        """
        Args:
            transport: optional ``orsim.messenger.transport.MessengerTransport``
                for this scheduler's own messenger. Phase-1 review flagged that
                this subclass swallowed the parameter orsim had just grown, so
                the seam was unreachable from the app; it is forwarded now.

                Left as ``None`` (the default) the transport is chosen by
                ``apps.transport.build_agent_transport`` from
                ``messenger_settings['AGENT_TRANSPORT']`` -- which defaults to
                ``paho``, in which case it returns ``None`` and
                ``super().__init__`` is called WITHOUT the keyword. That
                omission is load-bearing: the INSTALLED orsim is v1.3.0 and has
                no ``transport`` parameter at all, so passing it unconditionally
                would break every run the moment this file shipped.
        """
        built_here = False
        if transport is None:
            transport = select_scheduler_transport(run_id, scheduler_id)
            built_here = transport is not None

        if transport is None:
            # Byte-identical to the pre-phase-2 call: no seam is touched.
            super().__init__(run_id, scheduler_id, orsim_settings, init_failure_handler=init_failure_handler)
        else:
            # The seam check must not strand the transport it was checking for:
            # a KafkaTransport RETAINS a process-level bus (one Consumer, one
            # native poll thread, lane-partition assignments) in its
            # constructor, and a raise between the build and this scheduler's
            # teardown leaves that bus pinned for the life of the process. See
            # apps/agent_transport.py::select_agent_transport for the measured
            # agent-side version of the same leak.
            try:
                require_seam('OpenRideScheduler', ORSimScheduler)
            except Exception:
                if built_here:
                    try:
                        transport.disconnect()
                    except Exception:
                        logging.warning(
                            'OpenRideScheduler: could not tear down the transport built '
                            'for a run whose orsim has no transport seam', exc_info=True)
                raise
            super().__init__(
                run_id, scheduler_id, orsim_settings,
                init_failure_handler=init_failure_handler,
                transport=transport,
            )

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
        # Agents that never completed their async celery boot (see
        # BOOT_DEADLINE_STEPS). Non-zero means the run acted with fewer agents
        # than it was configured for, which no other number reports.
        self._boot_timed_out: int = 0

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

    #: How many steps an agent may stay ``pending_boot`` before the scheduler
    #: says so.  ``pending_boot`` exists because celery boot is async and a
    #: freshly added agent cannot be expected to answer the barrier yet -- but
    #: it had NO deadline anywhere, so an agent whose boot raised (a transport
    #: ``subscribe`` that failed, say) was excluded from ``expected`` silently
    #: and for ever: it never participated, the run finished with fewer orders,
    #: and ``summary.json`` still said ``ok: true``.  Boot is normally one or
    #: two steps; this is deliberately generous so a slow worker queue is never
    #: accused, and the guard only LOGS and COUNTS -- removing the agent would
    #: not bring it back and would hide the evidence.
    BOOT_DEADLINE_STEPS = 20

    #: Class-level default as well as an instance field, because the barrier
    #: tests build a scheduler with ``__new__`` and set only the fields they
    #: exercise -- a counter that exists only in ``__init__`` breaks them.
    _boot_timed_out = 0

    #: Replies that prove an agent took part in a step (anything but the boot
    #: handshake). See ``_note_participation``.
    _BOOT_ONLY_ACTIONS = frozenset({'ready', 'init_error'})

    # Init-failure state (concern 4). ``None`` at class level and created on first
    # use, because the barrier tests build a scheduler with ``__new__``; mutable
    # class-level defaults would be shared between instances.
    #: agent ids that sent at least one step reply.
    _stepped_agents = None
    #: agent id -> the step it was pruned on, for agents pruned before any step reply.
    _never_stepped_pruned = None
    #: agent id -> details of an ``init_error`` reply not yet raised.
    _init_errors = None

    # Planning-hold state (concern 5). ``None`` at class level for the same
    # ``__new__`` reason; created by ``_hold_state()`` only on a scheduler whose
    # cap is > 0, so ``_holds is None`` means "planning holds are off here" and
    # every default-path structure (agent_stat shape included) is untouched.
    #: agent id -> live hold record for the current step.
    _holds = None
    _hold_lock = None
    _HOLD_INIT_LOCK = threading.Lock()
    #: Wall ms the last finished step spent with at least one hold live. Read by
    #: SimulationRuntime for the perf stream; stays 0.0 on every default run.
    last_step_hold_ms = 0.0
    #: Outer-loop polling cadence of the held barrier (expiry + deadline re-check).
    _HOLD_POLL_SECONDS = 0.1

    def _planning_hold_cap(self) -> float:
        """``PLANNING.max_hold_seconds`` as a positive float, else 0.0 (holds off)."""
        planning = (getattr(self, 'orsim_settings', None) or {}).get('PLANNING') or {}
        if not isinstance(planning, dict):
            return 0.0
        cap = planning.get('max_hold_seconds', 0)
        if isinstance(cap, bool) or not isinstance(cap, (int, float)):
            return 0.0
        cap = float(cap)
        if not math.isfinite(cap) or cap <= 0:
            return 0.0
        return cap

    def _hold_state(self) -> None:
        """Create the hold ledger on first use (thread-safe; both threads may call it)."""
        if self._holds is not None:
            return
        with self._HOLD_INIT_LOCK:
            if self._holds is not None:
                return
            self._hold_lock = threading.Lock()
            self._hold_window_start = None   # monotonic; set while >= 1 hold is live
            self._hold_ms_step = 0.0
            self._holds_total = 0
            self._holds_expired_total = 0
            self._hold_seconds_max = 0.0
            self._hold_ms_total = 0.0
            self._hold_expired_step = set()  # agents whose hold expired this step
            self._holds = {}

    def _handle_hold(self, payload: dict) -> None:
        """Paho thread: accept or ignore one ``hold`` announcement (never tallied)."""
        agent_id = payload.get('agent_id')
        step = payload.get('time_step')
        cap = self._planning_hold_cap()
        if cap <= 0:
            logging.warning(
                "%s: ignoring planning hold from %s for step %s (hold_seconds=%r, reason=%r): "
                "PLANNING.max_hold_seconds is 0/absent on this run, so the barrier keeps its "
                "ordinary STEP_TIMEOUT.", getattr(self, 'scheduler_id', '?'), agent_id, step,
                payload.get('hold_seconds'), payload.get('reason'))
            return
        requested = payload.get('hold_seconds')
        if (isinstance(requested, bool) or not isinstance(requested, (int, float))
                or not math.isfinite(float(requested)) or requested <= 0):
            logging.warning("%s: ignoring planning hold from %s for step %s: hold_seconds %r is "
                            "not a positive number.", self.scheduler_id, agent_id, step, requested)
            return
        if isinstance(step, bool) or not isinstance(step, int) or step != self.time:
            logging.warning("%s: ignoring planning hold from %s for step %r: the current step is %s.",
                            self.scheduler_id, agent_id, step, self.time)
            return
        if agent_id not in self._expected_this_step or agent_id in self._responded_this_step:
            logging.warning("%s: ignoring planning hold from %s for step %s: the agent is not "
                            "awaited on this step (not expected, or already answered).",
                            self.scheduler_id, agent_id, step)
            return
        self._hold_state()
        if agent_id in self._hold_expired_step:
            logging.warning("%s: ignoring planning hold from %s for step %s: its hold on this step "
                            "already expired (one hold budget per agent per step).",
                            self.scheduler_id, agent_id, step)
            return
        granted = min(float(requested), cap)
        step_timeout = float(self.orsim_settings['STEP_TIMEOUT'])
        now = time.monotonic()
        with self._hold_lock:
            rec = self._holds.get(agent_id)
            if rec is None or rec['step'] != step:
                rec = {'step': step, 'accepted_at': now, 'hold_deadline': 0.0,
                       'granted': 0.0, 'reason': payload.get('reason')}
                self._holds_total += 1
                if not self._holds:
                    self._hold_window_start = now
                self._holds[agent_id] = rec
            # A renewed hold may extend, never past cap seconds from the FIRST one.
            rec['hold_deadline'] = max(rec['hold_deadline'],
                                       min(now + granted, rec['accepted_at'] + cap))
            rec['granted'] = rec['hold_deadline'] - rec['accepted_at']
            rec['expires_at'] = rec['hold_deadline'] + step_timeout
            self._hold_seconds_max = max(self._hold_seconds_max, rec['granted'])
        logging.info("%s: planning hold accepted from %s for step %s: %.1f s (requested %s s, cap %s s, "
                     "reason=%r); +%s s STEP_TIMEOUT grace after it.", self.scheduler_id, agent_id, step,
                     rec['granted'], requested, cap, payload.get('reason'), step_timeout)

    def _release_hold_locked(self, agent_id, now) -> dict:
        rec = self._holds.pop(agent_id, None)
        if rec is not None and not self._holds and self._hold_window_start is not None:
            self._hold_ms_step += (now - self._hold_window_start) * 1000.0
            self._hold_window_start = None
        return rec

    def _release_hold(self, agent_id) -> None:
        """Paho thread: the holder answered its step; its hold is over."""
        if not self._holds or agent_id not in self._holds:
            return
        with self._hold_lock:
            self._release_hold_locked(agent_id, time.monotonic())

    def _expire_holds(self, now: float) -> None:
        """Event loop: drop every hold whose grace (hold + STEP_TIMEOUT) has run out."""
        if not self._holds:
            return
        cap = self._planning_hold_cap()
        with self._hold_lock:
            expired = [(aid, rec) for aid, rec in self._holds.items() if now >= rec['expires_at']]
            for aid, _ in expired:
                self._release_hold_locked(aid, now)
                self._hold_expired_step.add(aid)
            self._holds_expired_total += len(expired)
        for aid, rec in expired:
            logging.error(
                "PLANNING HOLD EXPIRED: %s held step %s for %.1f s (cap %s s); continuing without it "
                "(granted %.1f s + STEP_TIMEOUT %s s grace, reason=%r). It is pruned as unresponsive "
                "for this step; a late 'completed' reinstates it.",
                aid, rec['step'], now - rec['accepted_at'], cap, rec['granted'],
                self.orsim_settings['STEP_TIMEOUT'], rec['reason'])

    def _latest_hold_expiry(self) -> float:
        if not self._holds:
            return 0.0
        with self._hold_lock:
            return max((rec['expires_at'] for rec in self._holds.values()), default=0.0)

    def _current_hold_ms(self) -> float:
        ms = self._hold_ms_step
        start = self._hold_window_start
        if start is not None:
            ms += (time.monotonic() - start) * 1000.0
        return ms

    def _reset_step_holds(self) -> None:
        """Step start: no hold survives into a new step."""
        if self._holds is None:
            return
        with self._hold_lock:
            self._holds.clear()
            self._hold_expired_step.clear()
            self._hold_window_start = None
            self._hold_ms_step = 0.0

    def _finish_step_holds(self) -> None:
        """Barrier end: close the held window, publish it, drop leftovers."""
        if self._holds is None:
            self.last_step_hold_ms = 0.0
            return
        with self._hold_lock:
            now = time.monotonic()
            for aid in list(self._holds):
                self._release_hold_locked(aid, now)
            ms = round(self._hold_ms_step, 2)
            self._hold_ms_total += ms
        self.last_step_hold_ms = ms
        stat = self.agent_stat.get(self.time)
        if isinstance(stat, dict) and 'hold_ms' in stat:
            # The barrier's final tally ran while the window was still open.
            stat['hold_ms'] = ms
            stat['hold_ms_total'] = round(self._hold_ms_total, 2)
            stat['holds_live'] = 0
            stat['holds_expired'] = self._holds_expired_total

    async def _confirm_responses_held(self) -> None:
        """``confirm_responses`` under a deadline that honours live planning holds.

        Raises ``asyncio.TimeoutError`` exactly where ``wait_for`` would have, except
        that the deadline is ``max(start + STEP_TIMEOUT, latest hold + STEP_TIMEOUT)``.
        """
        step_timeout = float(self.orsim_settings['STEP_TIMEOUT'])
        start = time.monotonic()
        task = asyncio.ensure_future(self.confirm_responses())
        try:
            while True:
                now = time.monotonic()
                self._expire_holds(now)
                deadline = max(start + step_timeout, self._latest_hold_expiry())
                remaining = deadline - now
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                done, _ = await asyncio.wait({task}, timeout=min(remaining, self._HOLD_POLL_SECONDS))
                if task in done:
                    task.result()
                    return
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _init_failure_state(self):
        if self._stepped_agents is None:
            self._stepped_agents = set()
            self._never_stepped_pruned = {}
            self._init_errors = {}

    def _note_participation(self, agent_id, action, payload) -> None:
        """Record a reply's init-failure meaning (paho thread; GIL-atomic set/dict ops)."""
        self._init_failure_state()
        if action == 'init_error':
            self._init_errors[agent_id] = str(payload.get('details') or 'no details')[-2000:]
            logging.error(
                "AGENT INIT FAILED: %s answered the boot handshake with init_error: %s",
                agent_id, self._init_errors[agent_id])
        elif action not in self._BOOT_ONLY_ACTIONS and agent_id is not None:
            self._stepped_agents.add(agent_id)
            self._never_stepped_pruned.pop(agent_id, None)

    def _check_init_failures(self) -> None:
        """Raise :class:`AgentInitFailure` (``'hard'``) or log (``'soft'``) for concern 4's two signals."""
        self._init_failure_state()
        overdue = sorted(aid for aid, pruned_at in self._never_stepped_pruned.items()
                         if self.time - pruned_at >= self.BOOT_DEADLINE_STEPS
                         and aid not in self._stepped_agents)
        for aid in overdue:
            # Heuristic signal: logged, never fatal (see concern 4).
            logging.error(
                "AGENT NEVER STEPPED: %s: %s was pruned at step %s and has been silent for %s+ "
                "steps without ever answering one; if its app was refused at init the run will "
                "finish short. Check `journalctl --user -u openride-celery` for 'Failed to launch app'.",
                self.scheduler_id, aid, self._never_stepped_pruned[aid], self.BOOT_DEADLINE_STEPS)
            self._never_stepped_pruned.pop(aid, None)
        problems = [f"{aid}: init_error ({details.strip().splitlines()[-1] if details.strip() else details})"
                    for aid, details in sorted(self._init_errors.items())]
        if not problems:
            return
        message = (f"{self.scheduler_id}: {len(problems)} agent(s) failed to initialise and will never act; "
                   "the run would finish short and report 'completed'. " + "; ".join(problems[:10])
                   + (" ..." if len(problems) > 10 else "")
                   + ". Check `journalctl --user -u openride-celery` for 'Failed to launch app'.")
        if getattr(self, 'init_failure_handler', 'soft') == 'hard':
            logging.error(message)
            raise AgentInitFailure(message)
        logging.error(message + " (init_failure_handler='soft': continuing)")
        self._init_errors.clear()

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
            # The step this agent was added on, so a boot that never completes
            # becomes visible instead of being excluded from the barrier for
            # ever. See BOOT_DEADLINE_STEPS.
            'boot_step': self.time,
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
        action = payload.get('action')
        if action == 'hold':
            # Concern 5: an announcement, not an answer -- never tallied.
            self._handle_hold(payload)
            return
        agent_id = payload.get('agent_id')
        response_time_step = payload.get('time_step')
        if response_time_step == -1:
            response_time_step = self.time
        self._note_participation(agent_id, action, payload)

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
                if self._holds:
                    self._release_hold(agent_id)
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
                self._check_boot_deadline(aid, item)
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
        self._reset_step_holds()

    def _check_boot_deadline(self, agent_id, item) -> None:
        """Complain ONCE about an agent that has been ``pending_boot`` too long.

        The silent-loss shape this closes: ``add_agent`` marks an agent
        ``pending_boot`` and ``_begin_step_barrier`` excludes those from
        ``expected``.  Nothing ever timed that out, so an agent whose boot
        raised -- most reachably a transport ``subscribe`` that failed, which
        ``KafkaTransport`` raises on by design -- was excluded for the whole
        run.  It never replied, it was never pruned (pruning only touches
        agents that WERE expected), the run completed with fewer orders, and
        the only trace was ``agent_stat.booting`` staying non-zero in
        ``sim.log``, which nobody reads per-step.  Counted in
        ``boot_timed_out`` so the number rides ``agent_stat`` into the perf
        summary.
        """
        boot_step = item.get('boot_step')
        if boot_step is None:
            item['boot_step'] = self.time
            return
        if item.get('boot_timeout_logged'):
            return
        if (self.time - boot_step) < self.BOOT_DEADLINE_STEPS:
            return
        item['boot_timeout_logged'] = True
        self._boot_timed_out += 1
        logging.error(
            "AGENT BOOT NEVER COMPLETED: %s has been pending_boot for %d steps "
            "(added at step %s, now %s). It is excluded from every step barrier, so "
            "it will never act and the run will finish short with status 'completed'. "
            "%d agent(s) in this run are in that state. Check "
            "`journalctl --user -u openride-celery` for a start_listening failure.",
            agent_id, self.time - boot_step, boot_step, self.time, self._boot_timed_out)

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
            # Rides agent_stat so a short run cannot report clean: an agent
            # stuck in pending_boot is excluded from every barrier for ever.
            'boot_timed_out': self._boot_timed_out,
        }
        if self._holds is not None:
            # Only on a scheduler that accepted a planning hold: default runs keep
            # the exact agent_stat shape.
            self.agent_stat[self.time].update({
                'holds': self._holds_total,
                'holds_live': len(self._holds),
                'holds_expired': self._holds_expired_total,
                'hold_seconds_max': round(self._hold_seconds_max, 3),
                'hold_ms': round(self._current_hold_ms(), 2),
                'hold_ms_total': round(self._hold_ms_total, 2),
            })
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
            if self._holds:
                # A live planning hold suspends the settle/tolerance early-exit.
                return False
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

        if self._planning_hold_cap() <= 0:
            # Default path (no planning holds on this run): the literal barrier wait.
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
        else:
            # Concern 5: same barrier, deadline extended by live planning holds.
            self._hold_state()
            try:
                await self._confirm_responses_held()
                self._consecutive_step_timeouts = 0
                logging.info(f'{self.agent_stat[self.time] = }')

            except asyncio.TimeoutError as e:
                logging.exception(f'Scheduler {self.scheduler_id} timeout beyond {self.orsim_settings["STEP_TIMEOUT"] = } (and any planning hold) while waiting for confirm_responses.')
                self.step_timeout_handler(e)
            finally:
                self._finish_step_holds()

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
                    # Never answered a step at all: watch it (concern 4).
                    self._init_failure_state()
                    if agent_id not in self._stepped_agents:
                        self._never_stepped_pruned.setdefault(agent_id, self.time)
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

        # Concern 4: an init failure fails the run (hard) instead of shrinking it.
        self._check_init_failures()

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
