"""Supervisor regression tests — the direct test for the five-week blindness.

Everything here races real threads; nothing asserts "a lock exists".
"""

from __future__ import annotations

import itertools
import threading
import time

import pytest

from apps.dataplane.supervisor import SupervisedTask, Supervisor, TaskStatus


def _wait_until(pred, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class TestRestartAndBackoff:
    def test_task_that_always_raises_is_restarted_with_growing_backoff(self):
        calls = []

        def boom(task):
            calls.append(time.monotonic())
            raise RuntimeError("nope")

        task = SupervisedTask(
            "boom", boom, backoff_initial=0.01, backoff_max=0.08, heartbeat_timeout=5.0
        )
        task.start()
        try:
            assert _wait_until(lambda: task.restarts >= 4)
        finally:
            task.stop(timeout=2.0)

        assert len(calls) >= 4
        history = list(task.backoff_history)
        assert history[0] == pytest.approx(0.01)
        # Monotone non-decreasing, doubling, capped.
        assert all(b <= a * 2 + 1e-9 for a, b in zip(history, history[1:]))
        assert all(a <= b for a, b in zip(history, history[1:]))
        assert max(history) <= 0.08
        assert task.status().last_error.startswith("RuntimeError")

    def test_non_kafka_exception_is_caught(self):
        """A DuckDB-style TransactionException killed the old thread. Not here."""

        class TransactionException(Exception):
            pass

        seen = threading.Event()

        def fn(task):
            if task.restarts == 0:
                raise TransactionException("write-write conflict")
            seen.set()
            while not task.stopping:
                task.heartbeat()
                task.wait(0.01)

        task = SupervisedTask("duck", fn, backoff_initial=0.01, heartbeat_timeout=5.0)
        task.start()
        try:
            assert seen.wait(3.0)
            assert _wait_until(lambda: task.healthy())
            assert task.restarts == 1
        finally:
            task.stop(timeout=2.0)

    def test_returning_without_stop_counts_as_a_restart(self):
        runs = []

        def fn(task):
            runs.append(1)
            return  # returns immediately, stop was never requested

        task = SupervisedTask("returner", fn, backoff_initial=0.01, backoff_max=0.02)
        task.start()
        try:
            assert _wait_until(lambda: task.restarts >= 3)
            assert "returned while stop was not requested" in (task.status().last_error or "")
        finally:
            task.stop(timeout=2.0)
        assert len(runs) >= 3

    def test_backoff_resets_after_a_clean_run(self):
        def fn(task):
            time.sleep(0.05)
            raise RuntimeError("late failure")

        # reset_after is below the run duration, so every failure resets the backoff.
        task = SupervisedTask(
            "resetter", fn, backoff_initial=0.01, backoff_max=1.0, reset_after=0.02
        )
        task.start()
        try:
            assert _wait_until(lambda: task.restarts >= 3, timeout=4.0)
        finally:
            task.stop(timeout=2.0)
        assert set(task.backoff_history[:3]) == {0.01}


class TestLiveness:
    def test_task_that_stops_heartbeating_goes_unhealthy(self):
        gate = threading.Event()

        def fn(task):
            task.heartbeat()
            gate.wait(10.0)  # deliberately never heartbeats again

        task = SupervisedTask("stale", fn, heartbeat_timeout=0.1, backoff_initial=0.01)
        task.start()
        try:
            assert _wait_until(lambda: task.alive())
            assert task.healthy() is True
            assert _wait_until(lambda: not task.healthy(), timeout=2.0)
            assert task.alive() is True  # alive but stale -> unhealthy
        finally:
            gate.set()
            task.stop(timeout=2.0)

    def test_unstarted_task_is_unhealthy(self):
        task = SupervisedTask("never", lambda t: None)
        assert task.healthy() is False
        assert task.alive() is False

    def test_status_is_a_taskstatus_with_the_pinned_fields(self):
        def fn(task):
            while not task.stopping:
                task.heartbeat()
                task.wait(0.01)

        task = SupervisedTask("s", fn, heartbeat_timeout=5.0)
        task.start()
        try:
            st = task.status()
            assert isinstance(st, TaskStatus)
            assert st.name == "s"
            assert st.alive is True
            assert st.healthy is True
            assert st.restarts == 0
            assert st.started_at is not None
            assert st.last_heartbeat is not None
            assert st.last_error is None
        finally:
            task.stop(timeout=2.0)


class TestRestartStorm:
    """Regression: a task that crashes on *every* attempt used to report healthy forever.

    The runner heartbeated at the top of each attempt, so `healthy()` — which only checked
    'thread alive' + heartbeat freshness — stayed True through an unbounded restart storm and
    /health kept answering 200 while the process ingested nothing.
    """

    def test_permanent_crash_loop_is_unhealthy_even_though_the_thread_exists(self):
        def boom(task):
            raise RuntimeError("kafka broker unreachable")

        task = SupervisedTask(
            "crashloop",
            boom,
            backoff_initial=0.01,
            backoff_max=0.02,
            heartbeat_timeout=30.0,   # generous: freshness alone would never catch this
            reset_after=5.0,
            unhealthy_after_failures=3,
        )
        task.start()
        try:
            assert _wait_until(lambda: task.restarts >= 4, timeout=4.0)
            status = task.status()
            assert status.alive is True                  # the thread is there…
            assert status.last_heartbeat is not None     # …and recently marked…
            assert status.healthy is False               # …but nothing is getting done.
            assert status.consecutive_failures >= 3
        finally:
            task.stop(timeout=2.0)

    def test_the_old_liveness_rule_alone_would_have_missed_it(self):
        """Causality check: with the storm rule disabled, the crash loop reports healthy.

        `unhealthy_after_failures=0` reproduces the pre-fix predicate (thread alive +
        heartbeat freshness). This is the exact behaviour the fix removes.
        """

        def boom(task):
            raise RuntimeError("kafka broker unreachable")

        task = SupervisedTask(
            "old-rule",
            boom,
            backoff_initial=0.01,
            backoff_max=0.02,
            heartbeat_timeout=30.0,
            unhealthy_after_failures=0,
        )
        task.start()
        try:
            assert _wait_until(lambda: task.restarts >= 4, timeout=4.0)
            assert task.healthy() is True  # <- the five-week blindness, reproduced
        finally:
            task.stop(timeout=2.0)

    def test_one_or_two_failures_do_not_flip_the_verdict(self):
        state = {"failures": 0}

        def fn(task):
            if state["failures"] < 2:
                state["failures"] += 1
                raise RuntimeError("transient")
            while not task.stopping:
                task.heartbeat()
                task.wait(0.01)

        task = SupervisedTask(
            "flaky", fn, backoff_initial=0.01, heartbeat_timeout=5.0, unhealthy_after_failures=3
        )
        task.start()
        try:
            assert _wait_until(lambda: state["failures"] == 2)
            assert _wait_until(lambda: task.healthy(), timeout=3.0)
        finally:
            task.stop(timeout=2.0)

    def test_a_storm_that_ends_becomes_healthy_again_once_the_attempt_settles(self):
        state = {"fail": True}

        def fn(task):
            if state["fail"]:
                raise RuntimeError("still broken")
            while not task.stopping:
                task.heartbeat()
                task.wait(0.01)

        task = SupervisedTask(
            "recover",
            fn,
            backoff_initial=0.01,
            backoff_max=0.02,
            heartbeat_timeout=5.0,
            reset_after=0.2,
            unhealthy_after_failures=3,
        )
        task.start()
        try:
            assert _wait_until(lambda: not task.healthy(), timeout=3.0)
            state["fail"] = False
            assert _wait_until(lambda: task.healthy(), timeout=3.0)
        finally:
            task.stop(timeout=2.0)

    def test_runner_marks_do_not_count_as_task_progress(self):
        """A function that never heartbeats cannot look alive just because the runner spins."""
        started = threading.Event()

        def never_heartbeats(task):
            started.set()
            task.wait(10.0)  # blocks without ever calling heartbeat()

        task = SupervisedTask("silent", never_heartbeats, heartbeat_timeout=0.1)
        task.start()
        try:
            assert started.wait(2.0)
            assert _wait_until(lambda: not task.healthy(), timeout=2.0)
            assert task.alive() is True
        finally:
            task.stop(timeout=2.0)


class TestStallWatchdog:
    """Regression: SupervisedTask only ever restarted tasks that *died*, never ones that hung."""

    def test_watchdog_restarts_a_stalled_task_that_opted_in(self):
        gate = threading.Event()
        attempts = []

        def wedged(task):
            attempts.append(time.monotonic())
            task.heartbeat()
            gate.wait(10.0)  # blocks forever, like a dump against a hung mongod

        sup = Supervisor()
        task = sup.add(
            "wedged",
            wedged,
            heartbeat_timeout=0.05,
            stall_timeout=0.1,
            restart_on_stall=True,
        )
        sup.start_all()
        try:
            assert _wait_until(lambda: task.stalled(), timeout=2.0)
            assert task.restarts == 0  # nothing died, so nothing was restarted before
            assert sup.watchdog_tick() == ["wedged"]
            assert _wait_until(lambda: len(attempts) >= 2, timeout=2.0)
            assert task.abandoned == 1
        finally:
            gate.set()
            sup.stop_all(timeout=2.0)

    def test_watchdog_reports_but_does_not_respawn_a_task_that_did_not_opt_in(self):
        gate = threading.Event()
        attempts = []

        def wedged(task):
            attempts.append(1)
            task.heartbeat()
            gate.wait(10.0)

        sup = Supervisor()
        task = sup.add("consumer-like", wedged, heartbeat_timeout=0.05, stall_timeout=0.1)
        sup.start_all()
        try:
            assert _wait_until(lambda: task.stalled(), timeout=2.0)
            assert sup.watchdog_tick() == []
            assert sup.stalled_tasks() == ["consumer-like"]
            assert sup.healthy() is False
            assert len(attempts) == 1
        finally:
            gate.set()
            sup.stop_all(timeout=2.0)

    def test_an_abandoned_thread_cannot_refresh_the_replacements_liveness(self):
        gate = threading.Event()
        done = threading.Event()
        replacement_running = threading.Event()
        attempts = itertools.count()
        lock = threading.Lock()

        def fn(task):
            with lock:
                nth = next(attempts)
            if nth == 0:
                task.heartbeat()
                gate.wait(10.0)
                # Released at last, the abandoned thread heartbeats hard. If those marks
                # counted, the wedged generation would keep its replacement looking healthy.
                while not done.is_set():
                    task.heartbeat()
                    time.sleep(0.005)
                return
            replacement_running.set()
            while not task.stopping:
                task.wait(0.01)  # replacement deliberately never heartbeats

        sup = Supervisor()
        task = sup.add("abandoned", fn, heartbeat_timeout=0.1, stall_timeout=0.15, restart_on_stall=True)
        sup.start_all()
        try:
            assert _wait_until(lambda: task.stalled(), timeout=2.0)
            assert task.restart_stalled() is True
            gate.set()
            assert replacement_running.wait(2.0)
            time.sleep(0.3)
            assert task.healthy() is False  # the old thread's heartbeats were ignored
        finally:
            done.set()
            gate.set()
            sup.stop_all(timeout=2.0)

    def test_an_abandoned_thread_stops_doing_work_not_just_beating(self):
        """The abandonment must END the old generation, not merely mute it.

        Muting the heartbeats was the whole of the previous fix, and it left the wedged
        thread running: ``_runner`` re-checks ``generation`` only *after* ``fn`` returns, and
        every task body loops on ``while not task.stopping`` — the SHARED stop event. So when
        the blocker cleared, the abandoned thread resumed doing real work beside its
        replacement. Measured before this fix with this exact body: two live ``dp-flush``
        threads, BOTH incrementing the work counter forever (28 and 20 units); in the service
        that is two frame captures per instant, i.e. two ``frame_idx`` values for one
        ``sim_time_ms`` in DuckDB and two ``_id``s in Mongo.

        ``task.stopping`` is now per-generation, so the abandoned thread returns at its next
        check. Nothing here asserts on heartbeats: this test only cares about WORK.
        """
        work = {}
        lock = threading.Lock()
        blocker = threading.Event()
        wedged_running = threading.Event()
        attempts = itertools.count()

        def flush_like(task):
            with lock:
                nth = next(attempts)
            me = threading.current_thread()
            while not task.stopping:
                if nth == 0:
                    wedged_running.set()
                    blocker.wait(20.0)      # the 225 s rehydrate holding the store lock
                    if task.stopping:       # per-generation: this generation is over
                        return
                with lock:
                    work[me] = work.get(me, 0) + 1
                task.heartbeat()
                if task.wait(0.01):
                    return

        sup = Supervisor()
        task = sup.add(
            "flush", flush_like, heartbeat_timeout=0.1, stall_timeout=0.15,
            restart_on_stall=True, backoff_initial=0.01, backoff_max=0.02,
        )
        sup.start_all()
        try:
            assert wedged_running.wait(2.0)
            wedged = [t for t in threading.enumerate() if t.name == "dp-flush"]
            assert len(wedged) == 1
            wedged_thread = wedged[0]

            assert _wait_until(lambda: task.stalled(), timeout=3.0)
            assert task.restart_stalled() is True
            assert _wait_until(
                lambda: len([t for t in threading.enumerate() if t.name == "dp-flush"]) == 2,
                timeout=3.0,
            )
            time.sleep(0.2)
            with lock:
                baseline = dict(work)

            blocker.set()                    # the blocker clears; the old thread wakes up
            assert _wait_until(lambda: not wedged_thread.is_alive(), timeout=3.0), (
                "the abandoned generation is still alive and running its loop"
            )
            time.sleep(0.2)
            with lock:
                after = dict(work)

            resumed = [t for t in after if after[t] > baseline.get(t, 0)]
            assert len(resumed) == 1, (
                f"{len(resumed)} generations are doing work at once: "
                f"{[(t.name, after[t], baseline.get(t, 0)) for t in after]}"
            )
            assert resumed[0] is not wedged_thread
            # The abandoned thread may complete at most the one iteration it was inside.
            assert after.get(wedged_thread, 0) <= baseline.get(wedged_thread, 0) + 1
            assert task.alive() is True and task.abandoned == 1
        finally:
            blocker.set()
            sup.stop_all(timeout=2.0)

    def test_wait_returns_true_for_a_superseded_generation(self):
        """``task.wait()`` is how every task body sleeps; it must wake an abandoned one."""
        results = {}
        started = threading.Event()
        gate = threading.Event()
        attempts = itertools.count()

        def fn(task):
            nth = next(attempts)
            if nth == 0:
                started.set()
                gate.wait(10.0)
                # abandoned by now: a 10 s wait must return immediately and say "stop"
                t0 = time.monotonic()
                results["waited"] = task.wait(10.0)
                results["elapsed"] = time.monotonic() - t0
                results["stopping"] = task.stopping
                return
            while not task.stopping:
                task.heartbeat()
                time.sleep(0.005)

        sup = Supervisor()
        task = sup.add("waiter", fn, heartbeat_timeout=0.1, stall_timeout=0.15,
                       restart_on_stall=True, backoff_initial=0.01, backoff_max=0.02)
        sup.start_all()
        try:
            assert started.wait(2.0)
            assert _wait_until(lambda: task.stalled(), timeout=3.0)
            assert task.restart_stalled() is True
            gate.set()
            assert _wait_until(lambda: "waited" in results, timeout=3.0)
            assert results["waited"] is True
            assert results["stopping"] is True
            assert results["elapsed"] < 1.0
        finally:
            gate.set()
            sup.stop_all(timeout=2.0)


class TestGenerationIdentity:
    """Regression: the stall-restart fix keyed 'abandoned' on ``threading.get_ident()``.

    CPython recycles idents aggressively — a replacement thread routinely gets a just-exited
    thread's ident. An ident-keyed abandoned set therefore muted a *healthy* generation: every
    heartbeat was dropped, ``stalled()`` went True, ``healthy()`` went False, and the watchdog
    restarted the task forever. The only two tasks with ``restart_on_stall=True`` are ``flush``
    (kpi flush + frame capture) and ``reconcile`` (the archive drain), so the storm loses data
    and hides unarchived runs while /health sits permanently at 503.

    Discrimination is by thread OBJECT now; these tests run the real ``SupervisedTask``.
    """

    def test_a_recycled_thread_ident_does_not_mute_a_healthy_generation(self):
        counter = itertools.count()
        lock = threading.Lock()
        gates = [threading.Event(), threading.Event()]
        threads = {}
        gen2_running = threading.Event()

        def fn(task):
            with lock:
                nth = next(counter)
            threads[nth] = threading.current_thread()
            if nth < 2:
                task.heartbeat()
                gates[nth].wait(20.0)
                return  # thread exits, so its ident becomes available for reuse
            gen2_running.set()
            while not task.stopping:
                task.heartbeat()
                time.sleep(0.002)

        sup = Supervisor()
        task = sup.add(
            "flush-like",
            fn,
            heartbeat_timeout=0.2,
            stall_timeout=0.2,
            restart_on_stall=True,
            backoff_initial=0.01,
            backoff_max=0.02,
        )
        sup.start_all()
        try:
            # generation 0 wedges and is abandoned…
            assert _wait_until(lambda: task.stalled(), timeout=3.0)
            assert task.restart_stalled() is True
            assert _wait_until(lambda: 1 in threads, timeout=3.0)
            # …then exits, freeing its ident for the next thread CPython creates.
            gates[0].set()
            threads[0].join(timeout=5.0)
            assert threads[0].is_alive() is False
            ident_a = threads[0].ident

            # generation 1 wedges and is abandoned; generation 2 is spawned in its place.
            assert _wait_until(lambda: task.stalled(), timeout=3.0)
            assert task.restart_stalled() is True
            gates[1].set()
            assert gen2_running.wait(5.0)

            if threads[2].ident != ident_a:
                pytest.skip(
                    "platform did not recycle the thread ident "
                    f"(gen0={ident_a}, gen2={threads[2].ident}) — this test only has teeth "
                    "when the ident is reused, and must not silently become a no-op"
                )

            # Generation 2 is a perfectly healthy heartbeating loop wearing generation 0's
            # ident. Observe it for ~0.6s: it must stay healthy and the watchdog must not fire.
            restarts_before = task.restarts
            fired = []
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                fired.extend(sup.watchdog_tick())
                assert task.stalled() is False
                assert task.healthy() is True
                time.sleep(0.02)
            assert fired == []
            assert task.restarts == restarts_before
            assert task.abandoned == 2
        finally:
            for gate in gates:
                gate.set()
            sup.stop_all(timeout=2.0)

    def test_a_late_beat_from_an_abandoned_thread_cannot_revive_a_dead_replacement(self):
        """Identity discrimination must still work in the direction it was added for.

        The replacement heartbeats normally (so the task is genuinely healthy), then stops.
        The abandoned thread wakes up and hammers ``heartbeat()``. Those beats must not keep
        the task looking alive — and this must hold even though the two threads are distinct
        objects that may share an ident lineage.
        """
        gate = threading.Event()
        done = threading.Event()
        stop_beating = threading.Event()
        replacement_healthy = threading.Event()
        counter = itertools.count()
        lock = threading.Lock()
        threads = {}

        def fn(task):
            with lock:
                nth = next(counter)
            threads[nth] = threading.current_thread()
            if nth == 0:
                task.heartbeat()
                gate.wait(20.0)
                while not done.is_set():
                    task.heartbeat()  # abandoned thread, beating as hard as it can
                    time.sleep(0.002)
                return
            while not task.stopping:
                if stop_beating.is_set():
                    replacement_healthy.set()
                else:
                    task.heartbeat()
                task.wait(0.005)

        sup = Supervisor()
        task = sup.add(
            "abandoned-late",
            fn,
            heartbeat_timeout=0.15,
            stall_timeout=0.15,
            restart_on_stall=True,
        )
        sup.start_all()
        try:
            assert _wait_until(lambda: task.stalled(), timeout=3.0)
            assert task.restart_stalled() is True
            assert _wait_until(lambda: 1 in threads, timeout=3.0)
            assert _wait_until(lambda: task.healthy(), timeout=2.0)  # replacement is fine
            assert threads[0] is not threads[1]

            gate.set()               # abandoned thread starts hammering heartbeat()
            stop_beating.set()       # replacement stops reporting progress
            assert replacement_healthy.wait(2.0)
            assert _wait_until(lambda: not task.healthy(), timeout=2.0)
            assert task.alive() is True
        finally:
            done.set()
            gate.set()
            sup.stop_all(timeout=2.0)


class TestSupervisor:
    def _looper(self):
        def fn(task):
            while not task.stopping:
                task.heartbeat()
                task.wait(0.01)

        return fn

    def test_healthy_is_false_while_any_task_is_unhealthy(self):
        """The five-week blindness in one assertion."""
        gate = threading.Event()

        def wedged(task):
            task.heartbeat()
            gate.wait(10.0)

        sup = Supervisor()
        sup.add("good", self._looper(), heartbeat_timeout=5.0)
        sup.add("bad", wedged, heartbeat_timeout=0.1, backoff_initial=0.01)
        sup.start_all()
        try:
            assert _wait_until(lambda: sup.healthy())
            assert _wait_until(lambda: not sup.healthy(), timeout=2.0)
            statuses = {s.name: s for s in sup.statuses()}
            assert statuses["good"].healthy is True
            assert statuses["bad"].healthy is False
            assert statuses["bad"].alive is True  # the process looks fine; the task does not
        finally:
            gate.set()
            sup.stop_all(timeout=2.0)

    def test_stop_all_joins_promptly(self):
        sup = Supervisor()
        for i in range(4):
            sup.add(f"t{i}", self._looper(), heartbeat_timeout=5.0)
        sup.start_all()
        assert _wait_until(lambda: all(t.alive() for t in sup.tasks()))
        started = time.monotonic()
        sup.stop_all(timeout=2.0)
        elapsed = time.monotonic() - started
        assert elapsed < 1.5, f"stop_all took {elapsed:.2f}s"
        assert all(not t.alive() for t in sup.tasks())

    def test_wedged_task_does_not_block_shutdown_and_is_reported_unhealthy(self):
        gate = threading.Event()

        def wedged(task):
            while not gate.wait(0.05):
                task.heartbeat()  # heartbeats fine, just refuses to notice `stopping`

        sup = Supervisor()
        sup.add("wedged", wedged, heartbeat_timeout=5.0)
        sup.start_all()
        try:
            assert _wait_until(lambda: sup.tasks()[0].alive())
            started = time.monotonic()
            sup.stop_all(timeout=0.2)
            assert time.monotonic() - started < 1.0
            assert sup.healthy() is False
            assert "did not stop" in (sup.statuses()[0].last_error or "")
        finally:
            gate.set()

    def test_many_threads_hammering_heartbeat_and_status_do_not_corrupt_state(self):
        sup = Supervisor()
        for i in range(3):
            sup.add(f"t{i}", self._looper(), heartbeat_timeout=5.0)
        sup.start_all()
        errors = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    sts = sup.statuses()
                    assert len(sts) == 3
                    sup.healthy()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(8)]
        for r in readers:
            r.start()
        time.sleep(0.3)
        stop.set()
        for r in readers:
            r.join(timeout=2.0)
        sup.stop_all(timeout=2.0)
        assert errors == []
