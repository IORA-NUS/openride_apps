"""
Unit tests for the step barrier in OpenRideScheduler.

These exercise the paho-thread reply path (_handle_reply) and the barrier
counters without standing up Celery/MQTT: the scheduler is built via __new__ so
the Messenger in __init__ is skipped, and agent "replies" are injected by
calling _handle_reply with the same payload dicts on_receive_message would parse.

Covers the incremental barrier, pending_boot exclusion, duplicate replies,
tolerance early-exit, and the sleep protocol (sleep_steps announcements,
early-wake replies, final-step ledger clear).
"""
import asyncio
import threading

from apps.simulation.openride_scheduler import OpenRideScheduler


def _make_scheduler(num_agents, *, booting=0, time=0, settings=None):
    """A bare scheduler with `num_agents` expected + `booting` not-yet-booted agents."""
    sched = OpenRideScheduler.__new__(OpenRideScheduler)
    sched.time = time
    sched.scheduler_id = "test"
    sched.run_id = "run"
    sched.agent_stat = {}
    sched._loop = None
    sched._response_event = None
    sched.orsim_settings = settings or {
        "STEP_TIMEOUT": 30,
        "STEP_TIMEOUT_TOLERANCE": 0.0,
        "STEP_SETTLE_SECONDS": 2,
    }
    sched.agent_collection = {}
    sched._consecutive_step_timeouts = 0
    sched._reply_topic = "run/test/ORSimScheduler"
    sched._sleep_lock = threading.Lock()
    sched._sleeping_until = {}
    sched._sleep_wake_topics = {}
    for i in range(num_agents):
        sched.agent_collection[f"a{i}"] = {
            "pending_boot": False,
            "step_response": {time: {"reaction": "waiting", "did_step": False, "run_time": 0}},
        }
    for i in range(booting):
        sched.agent_collection[f"b{i}"] = {
            "pending_boot": True,
            "step_response": {time: {"reaction": "waiting", "did_step": False, "run_time": 0}},
        }
    return sched


def _reply(sched, agent_id, reaction="completed", did_step=True, time_step=None,
           sleep_steps=None, wake_topic=None):
    """Simulate an agent's MQTT reply landing on the paho thread."""
    payload = {
        "agent_id": agent_id,
        "time_step": sched.time if time_step is None else time_step,
        "action": reaction,
        "did_step": did_step,
        "run_time": 1,
    }
    if sleep_steps is not None:
        payload["sleep_steps"] = sleep_steps
    if wake_topic is not None:
        payload["wake_topic"] = wake_topic
    sched._handle_reply(payload)


def test_barrier_resolves_when_all_expected_respond():
    async def run():
        sched = _make_scheduler(5)
        sched._loop = asyncio.get_running_loop()
        sched._response_event = asyncio.Event()
        sched._begin_step_barrier()
        assert sched._outstanding == 5

        # Drip the replies in from a background task while confirm_responses awaits.
        async def drip():
            for i in range(5):
                await asyncio.sleep(0)
                _reply(sched, f"a{i}")

        await asyncio.gather(
            asyncio.wait_for(sched.confirm_responses(), timeout=5),
            drip(),
        )
        assert sched._outstanding == 0
        # Final authoritative tally ran.
        assert sched.agent_stat[sched.time]["completed"] == 5
        assert sched.agent_stat[sched.time]["waiting"] == 0
        assert sched.agent_stat[sched.time]["stepping_agents"] == 5

    asyncio.run(run())


def test_booting_agents_are_not_awaited():
    async def run():
        sched = _make_scheduler(3, booting=4)
        sched._loop = asyncio.get_running_loop()
        sched._response_event = asyncio.Event()
        sched._begin_step_barrier()
        # Only the 3 non-booting agents are expected.
        assert sched._outstanding == 3

        async def drip():
            for i in range(3):
                await asyncio.sleep(0)
                _reply(sched, f"a{i}")

        await asyncio.gather(
            asyncio.wait_for(sched.confirm_responses(), timeout=5),
            drip(),
        )
        assert sched._outstanding == 0
        assert sched.agent_stat[sched.time]["booting"] == 4

    asyncio.run(run())


def test_duplicate_replies_do_not_over_decrement():
    async def run():
        sched = _make_scheduler(2)
        sched._loop = asyncio.get_running_loop()
        sched._response_event = asyncio.Event()
        sched._begin_step_barrier()

        _reply(sched, "a0")
        _reply(sched, "a0")  # duplicate — must be ignored
        assert sched._outstanding == 1  # not -0 or below

        async def finish():
            await asyncio.sleep(0)
            _reply(sched, "a1")

        await asyncio.gather(
            asyncio.wait_for(sched.confirm_responses(), timeout=5),
            finish(),
        )
        assert sched._outstanding == 0

    asyncio.run(run())


def test_tolerance_allows_early_exit_with_stragglers():
    async def run():
        # Allow up to 50% unresponsive after a 0s settle window.
        sched = _make_scheduler(4, settings={
            "STEP_TIMEOUT": 30,
            "STEP_TIMEOUT_TOLERANCE": 0.5,
            "STEP_SETTLE_SECONDS": 0,
        })
        sched._loop = asyncio.get_running_loop()
        sched._response_event = asyncio.Event()
        sched._begin_step_barrier()

        async def drip():
            await asyncio.sleep(0)
            _reply(sched, "a0")
            _reply(sched, "a1")  # 2/4 outstanding == 50% <= tolerance

        await asyncio.gather(
            asyncio.wait_for(sched.confirm_responses(), timeout=5),
            drip(),
        )
        # Resolved early with 2 still outstanding (pruned later by the shutdown sweep).
        assert sched._outstanding == 2
        # The stragglers are exactly the non-responders the sweep would prune.
        assert sched._expected_this_step - sched._responded_this_step == {"a2", "a3"}

    asyncio.run(run())


def test_step_response_history_is_capped():
    sched = _make_scheduler(1)
    sched._begin_step_barrier()
    for t in range(0, 6):
        sched.time = t
        _reply(sched, "a0", time_step=t)
    sr = sched.agent_collection["a0"]["step_response"]
    assert len(sr) <= 2
    assert 5 in sr  # newest kept
    assert 4 in sr  # previous step kept for the perf slow-agent reader


# ── Sleep protocol ─────────────────────────────────────────────────────────


def test_sleeping_agent_is_excluded_until_wake_step():
    sched = _make_scheduler(3)
    sched._begin_step_barrier()
    # a0 answers step 0 and announces a 2-step nap (wake step = 2).
    _reply(sched, "a0", sleep_steps=2)
    _reply(sched, "a1")
    _reply(sched, "a2")
    assert sched._outstanding == 0
    assert sched._sleeping_until == {"a0": 2}

    # Step 1: a0 not expected.
    sched.time = 1
    sched._begin_step_barrier()
    assert sched._expected_this_step == {"a1", "a2"}
    assert sched._outstanding == 2
    assert sched._sleeping_count_this_step == 1

    # Step 2: nap expired — a0 expected again, ledger cleaned.
    sched.time = 2
    sched._begin_step_barrier()
    assert sched._expected_this_step == {"a0", "a1", "a2"}
    assert sched._sleeping_until == {}


def test_early_wake_reply_clears_sleep_without_counting():
    sched = _make_scheduler(2)
    sched._begin_step_barrier()
    _reply(sched, "a0", sleep_steps=10)
    _reply(sched, "a1")

    sched.time = 1
    sched._begin_step_barrier()
    assert sched._expected_this_step == {"a1"}

    # a0 wakes itself early (queued event) and replies unsolicited.
    _reply(sched, "a0")
    # Not counted for this step's barrier...
    assert "a0" not in sched._responded_this_step
    assert sched._outstanding == 1
    # ...but expected again from the next step.
    assert "a0" not in sched._sleeping_until
    sched.time = 2
    sched._begin_step_barrier()
    assert sched._expected_this_step == {"a0", "a1"}


def test_woken_sleeper_shutdown_reply_is_reaped():
    sched = _make_scheduler(2)
    sched._begin_step_barrier()
    _reply(sched, "a0", sleep_steps=10)
    _reply(sched, "a1")

    sched.time = 1
    sched._begin_step_barrier()
    # a0 woke via an event, reached a terminal state and replied shutdown.
    _reply(sched, "a0", reaction="shutdown")
    assert "a0" in sched._shutdown_replied  # sweep in step() removes it


def test_final_step_does_not_expect_sleeping_agents():
    # Indefinite sleepers unsubscribed from the broadcast; the final step must
    # not wait on them (step() direct-notifies + deregisters them instead).
    sched = _make_scheduler(3)
    sched._begin_step_barrier()
    _reply(sched, "a0", sleep_steps=-1, wake_topic="run/oid0")
    _reply(sched, "a1")
    _reply(sched, "a2")

    sched.time = 1
    sched._begin_step_barrier(is_final=True)
    assert sched._expected_this_step == {"a1", "a2"}
    assert sched._sleeping_until == {"a0": float("inf")}
    assert sched._sleep_wake_topics == {"a0": "run/oid0"}


def test_indefinite_sleep_never_expires_and_wakes_on_reply():
    sched = _make_scheduler(2)
    sched._begin_step_barrier()
    _reply(sched, "a0", sleep_steps=-1, wake_topic="run/oid0")
    _reply(sched, "a1")

    # Far in the future it is still excluded.
    sched.time = 5000
    sched._begin_step_barrier()
    assert sched._expected_this_step == {"a1"}

    # A reply (woken by an event) clears both ledgers.
    _reply(sched, "a0")
    assert "a0" not in sched._sleeping_until
    assert "a0" not in sched._sleep_wake_topics


def test_pruned_straggler_is_reinstated_on_late_reply():
    sched = _make_scheduler(2)
    sched._pruned_stash = {}
    sched._begin_step_barrier()
    _reply(sched, "a0")
    # a1 never replies → swept as a non-responder (stashed), like step() does.
    non_responders = sched._expected_this_step - sched._responded_this_step
    assert non_responders == {"a1"}
    sched._pruned_stash["a1"] = sched.agent_collection["a1"]
    sched.remove_agent("a1")
    assert "a1" not in sched.agent_collection

    # The late reply arrives next step: agent is re-registered, not lost.
    sched.time = 1
    sched._begin_step_barrier()
    _reply(sched, "a1")
    assert "a1" in sched.agent_collection
    assert sched._pruned_stash == {}
    # And it is a normal expected responder from the following step.
    sched.time = 2
    sched._begin_step_barrier()
    assert "a1" in sched._expected_this_step


def test_pruned_agent_shutdown_reply_is_not_reinstated():
    sched = _make_scheduler(1)
    sched._pruned_stash = {"a0": sched.agent_collection["a0"]}
    sched.remove_agent("a0")
    sched._begin_step_barrier()
    _reply(sched, "a0", reaction="shutdown")
    assert "a0" not in sched.agent_collection


def test_remove_agent_cleans_sleep_ledgers():
    sched = _make_scheduler(1)
    sched._begin_step_barrier()
    _reply(sched, "a0", sleep_steps=-1, wake_topic="run/oid0")
    sched.remove_agent("a0")
    assert sched._sleeping_until == {}
    assert sched._sleep_wake_topics == {}
    assert "a0" not in sched.agent_collection


def test_sleep_renewal_on_wake_step_reply():
    sched = _make_scheduler(1)
    sched._begin_step_barrier()
    _reply(sched, "a0", sleep_steps=2)

    sched.time = 2
    sched._begin_step_barrier()
    assert sched._expected_this_step == {"a0"}
    # Still idle — answers its check-in and goes straight back to sleep.
    _reply(sched, "a0", sleep_steps=2)
    assert sched._outstanding == 0
    assert sched._sleeping_until == {"a0": 4}
