"""Supervised background tasks with restart, backoff, stall detection and liveness reporting.

This module exists because of a concrete outage: ``apps/kpi_sink`` ran four unsupervised
threads, one of which guarded its loop with ``except KafkaException``. A DuckDB
``TransactionException`` is not a ``KafkaException``, so the thread died silently on
2026-07-01 and nothing noticed for five weeks — systemd still reported ``active (running)``
because the *process* was alive.

Three things are fixed here and all three are load-bearing:

1. The runner catches ``Exception`` — never one subclass — logs it, and restarts the task
   function with exponential backoff.
2. Every task reports liveness so a dead task can make ``/health`` fail rather than merely
   appear in the body. Liveness is **three** predicates, not one:

   * the thread object is alive,
   * the *task function* (not the runner) has made progress recently, and
   * the task is not in a restart storm.

   The third predicate is the fix for a real hole: a task that raised instantly on every
   attempt used to report ``healthy=True`` forever, because the runner heartbeated at the top
   of each attempt. A crash-loop is now unhealthy after ``unhealthy_after_failures``
   consecutive failures — the process ingests nothing and ``/health`` says so.
3. A task that *hangs* (alive, but stopped heartbeating) is detected by
   :meth:`Supervisor.watchdog_tick` and, for tasks that opt in with ``restart_on_stall``,
   abandoned and respawned. ``SupervisedTask`` previously only ever restarted tasks that
   *died*, so a thread wedged in a blocking call was never recovered.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from weakref import WeakKeyDictionary

logger = logging.getLogger(__name__)

# A task that has run this long without dying is considered "settled": its backoff and its
# consecutive-failure counter both reset.
DEFAULT_RESET_AFTER_S = 60.0
# Consecutive failed attempts before a task is called unhealthy (a restart storm ingests
# nothing, so reporting it healthy is exactly the five-week blindness in miniature).
DEFAULT_UNHEALTHY_AFTER_FAILURES = 3


@dataclass
class TaskStatus:
    """Immutable snapshot of one supervised task's liveness."""

    name: str
    alive: bool
    healthy: bool
    started_at: Optional[float]
    last_heartbeat: Optional[float]
    restarts: int
    last_error: Optional[str]
    consecutive_failures: int = 0
    stalled: bool = False


class SupervisedTask:
    """One named background thread that is restarted forever until asked to stop.

    ``fn`` is called as ``fn(task)``; it is expected to loop until ``task.stopping`` is true
    and to call ``task.heartbeat()`` at least every ``heartbeat_timeout`` seconds.

    ``task.heartbeat()`` means *the task function made progress*. The runner never calls it —
    it uses a private liveness mark instead — so "the runner is spinning" can never be
    mistaken for "the work is happening".
    """

    def __init__(
        self,
        name: str,
        fn: Callable[["SupervisedTask"], None],
        *,
        stop_event: Optional[threading.Event] = None,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
        heartbeat_timeout: float = 30.0,
        reset_after: float = DEFAULT_RESET_AFTER_S,
        unhealthy_after_failures: int = DEFAULT_UNHEALTHY_AFTER_FAILURES,
        stall_timeout: Optional[float] = None,
        restart_on_stall: bool = False,
    ) -> None:
        self.name = name
        self._fn = fn
        self._stop = stop_event if stop_event is not None else threading.Event()
        self.backoff_initial = float(backoff_initial)
        self.backoff_max = float(backoff_max)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.reset_after = float(reset_after)
        self.unhealthy_after_failures = int(unhealthy_after_failures)
        self.stall_timeout = (
            float(stall_timeout) if stall_timeout is not None else max(3.0 * self.heartbeat_timeout, 1.0)
        )
        self.restart_on_stall = bool(restart_on_stall)

        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        # The thread OBJECT of the current generation. Object identity is what discriminates
        # generations; a thread *ident* must never be used for this, because CPython recycles
        # idents aggressively (a replacement thread routinely gets a dead thread's ident).
        self._active_thread: Optional[threading.Thread] = None
        # Which generation each runner thread belongs to, keyed by thread OBJECT. A
        # WeakKeyDictionary so a dead thread's entry disappears with it, and never
        # ``get_ident()``: CPython recycles idents, so an ident-keyed map routinely
        # mislabels a healthy replacement as an abandoned generation.
        self._thread_generation: "WeakKeyDictionary[threading.Thread, int]" = WeakKeyDictionary()
        self._generation = 0
        self._started_at: Optional[float] = None
        self._round_started: Optional[float] = None    # start of the current attempt
        self._last_heartbeat: Optional[float] = None   # runner liveness OR fn progress
        self._last_progress: Optional[float] = None    # fn progress only
        self._restarts = 0
        self._consecutive_failures = 0
        self._abandoned = 0
        self._last_error: Optional[str] = None
        self._join_failed = False
        self._finished = False
        # Observable backoff history — the restart storm is testable, not asserted.
        self.backoff_history: List[float] = []

    # ------------------------------------------------------------------ liveness

    def _is_current_generation(self) -> bool:
        """True when the calling thread IS the generation that currently owns this task.

        Compared by thread *object*, never by ``threading.get_ident()``: idents are recycled
        (a replacement thread frequently gets a just-exited thread's ident), so an
        ident-keyed abandoned set silently mutes a perfectly healthy generation and puts the
        task into a permanent restart storm. Caller holds the lock.
        """
        return threading.current_thread() is self._active_thread

    def heartbeat(self) -> None:
        """Called by the task function: 'I made progress'.

        Calls from an abandoned (stall-restarted) thread are ignored — otherwise a wedged
        thread that eventually wakes up would refresh the liveness of its replacement.
        """
        with self._lock:
            if not self._is_current_generation():
                return
            now = time.monotonic()
            self._last_heartbeat = now
            self._last_progress = now

    def _mark_alive(self) -> None:
        """Runner-only liveness mark. Deliberately does NOT count as progress."""
        with self._lock:
            if not self._is_current_generation():
                return
            self._last_heartbeat = time.monotonic()

    @property
    def stopping(self) -> bool:
        """True when the service is stopping OR the CALLING thread has been superseded.

        Each generation gets its own stop signal. Abandoning a stalled thread used to mean
        only that its heartbeats were ignored: ``_runner`` re-checks ``generation`` solely
        *after* ``self._fn(self)`` returns, and task bodies loop on ``while not
        task.stopping`` — the shared service event — so a stall-restarted thread ran forever
        beside its replacement. Measured: after one watchdog restart, two live ``dp-flush``
        threads both kept capturing frames, producing two ``frame_idx`` values for one
        ``sim_time_ms``, ``/health`` green throughout.

        A caller that is not a runner thread of this task (every embedding that holds the
        task object directly) is not in the map at all and sees the plain stop event.
        """
        if self._stop.is_set():
            return True
        with self._lock:
            generation = self._thread_generation.get(threading.current_thread())
            return generation is not None and generation != self._generation

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds, waking early on stop. True if it should stop.

        A caller that is *already* superseded does not sleep at all: it is being asked to
        leave, and every second it spends here is a second its replacement is running beside
        it.
        """
        if self.stopping:
            return True
        return self._stop.wait(timeout) or self.stopping

    @property
    def restarts(self) -> int:
        with self._lock:
            return self._restarts

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def abandoned(self) -> int:
        with self._lock:
            return self._abandoned

    def alive(self) -> bool:
        with self._lock:
            return bool(self._thread is not None and self._thread.is_alive())

    def stalled(self) -> bool:
        """Alive, not asked to stop, and no liveness mark for ``stall_timeout`` seconds."""
        with self._lock:
            if self._stop.is_set():
                return False
            thread = self._thread
            if thread is None or not thread.is_alive():
                return False
            ref = self._last_progress if self._last_progress is not None else self._last_heartbeat
            if ref is None:
                return False
            return (time.monotonic() - ref) > self.stall_timeout

    def _current_attempt_settled_locked(self) -> bool:
        """True when the running attempt has lasted ``reset_after``. Caller holds the lock."""
        started = self._round_started
        return started is not None and (time.monotonic() - started) >= self.reset_after

    def healthy(self) -> bool:
        with self._lock:
            if self._join_failed:
                return False
            if self._stop.is_set() and self._finished:
                # Cleanly stopped tasks are not "unhealthy" — they were asked to go.
                return True
            thread = self._thread
            if thread is None or not thread.is_alive():
                return False
            if (
                self.unhealthy_after_failures > 0
                and self._consecutive_failures >= self.unhealthy_after_failures
                and not self._current_attempt_settled_locked()
            ):
                # Restart storm: the thread exists, but no work is getting done. The task is
                # only considered recovered once the *current* attempt has survived
                # ``reset_after`` seconds — a storm is not over just because the latest
                # attempt has not crashed yet.
                return False
            # Freshness is measured against *task function* progress when the function has
            # ever reported any; the runner's own mark is only a fallback for tasks that
            # have not reached their first heartbeat yet.
            ref = self._last_progress if self._last_progress is not None else self._last_heartbeat
            if ref is None:
                return False
            return (time.monotonic() - ref) <= self.heartbeat_timeout

    def status(self) -> TaskStatus:
        with self._lock:
            return TaskStatus(
                name=self.name,
                alive=self.alive(),
                healthy=self.healthy(),
                started_at=self._started_at,
                last_heartbeat=self._last_heartbeat,
                restarts=self._restarts,
                last_error=self._last_error,
                consecutive_failures=self._consecutive_failures,
                stalled=self.stalled(),
            )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._finished = False
            self._join_failed = False
            self._started_at = time.monotonic()
            self._last_heartbeat = self._started_at
            self._last_progress = None
            self._round_started = None
            self._consecutive_failures = 0
            self._spawn_locked()

    def _spawn_locked(self) -> None:
        """Start a runner thread for a fresh generation. Caller holds the lock."""
        self._generation += 1
        generation = self._generation
        thread = threading.Thread(
            target=self._runner, args=(generation,), name=f"dp-{self.name}", daemon=True
        )
        self._thread = thread
        # Publish the owning thread OBJECT before starting it, so the very first
        # _mark_alive() from inside the runner is already recognised as current, and so the
        # thread can ask "am I still the current generation?" from its first instruction.
        self._active_thread = thread
        self._thread_generation[thread] = generation
        thread.start()

    def restart_stalled(self) -> bool:
        """Abandon a wedged thread and start a replacement. Returns True if it did.

        Only tasks that opted in with ``restart_on_stall=True`` are respawned: a task whose
        work is not safe to run twice concurrently (the Kafka consumer, which would end up
        with two threads polling the same consumer object) must be reported, not duplicated.
        """
        if not self.restart_on_stall:
            return False
        with self._lock:
            if self._stop.is_set() or not self.stalled():
                return False
            # The wedged thread is abandoned simply by ceasing to be ``_active_thread``:
            # _spawn_locked() replaces it below, so any late beat from the old thread object
            # fails the identity check. Nothing is remembered about it — remembering its
            # ident is what poisoned the replacement.
            self._abandoned += 1
            self._restarts += 1
            self._last_error = f"stalled for >{self.stall_timeout:.1f}s — thread abandoned and restarted"
            self._last_heartbeat = time.monotonic()
            self._last_progress = None
            self._finished = False
            logger.error("supervised task %s stalled — abandoning thread and restarting", self.name)
            self._spawn_locked()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            # Do not block shutdown on a wedged task; report it instead.
            with self._lock:
                self._join_failed = True
                self._last_error = f"task did not stop within {timeout}s"
            logger.error("supervised task %s did not join within %.1fs", self.name, timeout)

    # ------------------------------------------------------------------ runner

    def _runner(self, generation: int) -> None:
        backoff = self.backoff_initial
        while not self._stop.is_set():
            with self._lock:
                if generation != self._generation:
                    return  # abandoned by restart_stalled(); this thread owns nothing anymore
            round_started = time.monotonic()
            with self._lock:
                if generation == self._generation:
                    self._round_started = round_started
            self._mark_alive()
            failed = True
            try:
                self._fn(self)
            except Exception as exc:  # noqa: BLE001 — the whole point: never one subclass
                with self._lock:
                    if generation == self._generation:
                        self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("supervised task %s crashed", self.name)
            else:
                if self._stop.is_set():
                    failed = False
                    break
                with self._lock:
                    if generation == self._generation:
                        self._last_error = "task function returned while stop was not requested"
                logger.warning(
                    "supervised task %s returned without a stop request — restarting", self.name
                )

            if self._stop.is_set():
                break

            settled = (time.monotonic() - round_started) >= self.reset_after
            with self._lock:
                if generation != self._generation:
                    return
                self._restarts += 1
                self._round_started = None  # sleeping in backoff is not "settled"
                if settled:
                    self._consecutive_failures = 0
                elif failed:
                    self._consecutive_failures += 1
            if settled:
                backoff = self.backoff_initial

            self._mark_alive()
            self.backoff_history.append(backoff)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2.0, self.backoff_max)

        with self._lock:
            if generation == self._generation:
                self._finished = True


class Supervisor:
    """A flat registry of :class:`SupervisedTask` objects with an all-or-nothing health verdict."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, SupervisedTask] = {}

    def add(self, name: str, fn, **kwargs) -> SupervisedTask:
        task = SupervisedTask(name, fn, **kwargs)
        with self._lock:
            self._tasks[name] = task
        return task

    def get(self, name: str) -> Optional[SupervisedTask]:
        with self._lock:
            return self._tasks.get(name)

    def tasks(self) -> List[SupervisedTask]:
        with self._lock:
            return list(self._tasks.values())

    def start_all(self) -> None:
        for task in self.tasks():
            task.start()

    def stop_all(self, timeout: float = 5.0) -> None:
        tasks = self.tasks()
        # Signal everyone first so the joins overlap instead of serialising.
        for task in tasks:
            task._stop.set()
        for task in tasks:
            task.stop(timeout=timeout)

    def watchdog_tick(self) -> List[str]:
        """Respawn any opted-in task whose heartbeat has gone stale. Returns their names."""
        restarted: List[str] = []
        for task in self.tasks():
            try:
                if task.restart_stalled():
                    restarted.append(task.name)
                elif task.stalled():
                    logger.warning(
                        "supervised task %s is stalled (no progress for >%.1fs)",
                        task.name,
                        task.stall_timeout,
                    )
            except Exception:  # noqa: BLE001 - the watchdog must never take the process down
                logger.exception("watchdog tick failed for task %s", task.name)
        return restarted

    def stalled_tasks(self) -> List[str]:
        return [task.name for task in self.tasks() if task.stalled()]

    def statuses(self) -> List[TaskStatus]:
        return [task.status() for task in self.tasks()]

    def healthy(self) -> bool:
        return all(task.healthy() for task in self.tasks())
