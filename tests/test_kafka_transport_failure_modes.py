"""The failure modes an adversarial review REPRODUCED, one test each.

Every test in here corresponds to a defect that was measured against the live
broker before it was fixed, and every one of them shared a single shape: the
transport went partly or wholly deaf while ``summary.json`` would still have
said ``status: completed, ok: true, error: null``.  That is the shape CLAUDE.md
records twice already (the 3,247-lost-events run in section 8, the
8,032-vs-14,300-orders run in section 6.23), so these are not hypotheticals with
a test bolted on -- they are the project's known way of losing a night.

The existing ``tests/test_kafka_transport_unit.py`` pins the HAPPY paths of the
same machinery (attach, detach, refcounts, counters, absorb-don't-raise).  This
file pins what happens when a step of that machinery FAILS, which is exactly
what none of those 19 tests exercised.

Real broker where the failure is about the broker; injected failures where the
point is that one call raised.  A fake that re-implemented partition assignment
would be testing the fake.
"""

import logging
import os
import subprocess
import sys
import time

import pytest

from tests.kafka_run_ids import dated_run_id

BOOTSTRAP = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9094')

SETTINGS = {
    'KAFKA_BOOTSTRAP_SERVERS': BOOTSTRAP,
    'KAFKA_APP_PARTITIONS': 8,
    'KAFKA_REPLY_PARTITIONS': 4,
    'KAFKA_LINGER_MS': 5,
}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _broker_is_up(timeout=5.0):
    try:
        from confluent_kafka.admin import AdminClient
        metadata = AdminClient({'bootstrap.servers': BOOTSTRAP}).list_topics(timeout=timeout)
        return bool(metadata and metadata.brokers)
    except Exception:
        return False


_BROKER_UP = _broker_is_up()
needs_broker = pytest.mark.skipif(not _BROKER_UP, reason=f'no Kafka broker at {BOOTSTRAP}')


@pytest.fixture
def run_id():
    from apps.transport.kafka_bus import shutdown_all_buses
    from apps.transport.lane_admin import reap_run_lane_topics

    rid = dated_run_id('fail')
    yield rid
    shutdown_all_buses()
    try:
        reap_run_lane_topics(rid, BOOTSTRAP)
    except Exception:
        pass


@pytest.fixture
def transport_factory(run_id):
    from apps.transport.kafka_transport import KafkaTransport

    made = []

    def _make(identity):
        transport = KafkaTransport(SETTINGS, run_id, identity)
        made.append(transport)
        return transport

    yield _make
    for transport in reversed(made):
        try:
            transport.disconnect()
        except Exception:
            pass


def _wait(predicate, timeout=6.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class _ProxyConsumer:
    """Forwards everything to a real ``Consumer``, failing ``incremental_assign``.

    Wraps rather than patches because ``confluent_kafka.Consumer`` is a C type
    and will not take an attribute.  The poll thread captured the REAL consumer
    when it started, so swapping this in affects only the assign path -- which
    is the path under test.
    """

    def __init__(self, inner, fail_times=1):
        self._inner = inner
        self.remaining = fail_times
        self.assign_attempts = 0

    def incremental_assign(self, assignments):
        self.assign_attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError('injected incremental_assign failure (_CONFLICT shape)')
        return self._inner.incremental_assign(assignments)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# FINDING 1 (critical) -- a failed subscribe used to be PERMANENT and SILENT.
# ---------------------------------------------------------------------------

@needs_broker
def test_a_failed_subscribe_raises_and_leaves_nothing_behind(transport_factory, run_id):
    """The retry must be a clean first claim, not a no-op that returns (0, 0).

    Old order: ``_topics.add(name)`` BEFORE ``attach_topic``.  One transient
    assign failure then made every retry match the ``already held`` guard and
    return success while the consumer's assignment stayed ``[]`` -- and the
    leaked partition refcount stopped every OTHER agent in the worker from ever
    triggering the 0 -> 1 assign either.  ``_resubscribe_broadcast`` catches,
    logs and retries in exactly that shape, then sets
    ``_broadcast_subscribed = True``.
    """
    from apps.transport.lane_topics import LANE_BCAST

    broadcast = f'{run_id}/scheduler_1/ORSimAgent'
    transport = transport_factory('agent_flaky')
    bus = transport._bus
    bus._consumer = _ProxyConsumer(bus._consumer, fail_times=1)

    with pytest.raises(RuntimeError):
        transport.subscribe(broadcast)

    assert broadcast not in transport._topics, \
        'a subscribe that raised must not leave the topic recorded as held'
    assert (LANE_BCAST, 0) not in bus._partition_refs, \
        'the partition refcount must be rolled back, or no agent can ever assign it'
    assert broadcast not in bus._routes

    transport.subscribe(broadcast)          # the retry the agent really performs

    assert broadcast in transport._topics
    assert bus._partition_refs[(LANE_BCAST, 0)] == 1
    held = bus._current_assignment()
    assert (bus.lane_topics[LANE_BCAST], 0) in held, \
        'the retry must actually reach incremental_assign'


@needs_broker
def test_a_failed_subscribe_does_not_deafen_the_OTHER_agents_in_the_worker(
        transport_factory, run_id):
    """The leaked refcount was the half that took ~17 agents down, not one.

    ``attach_topic`` bumped the partition refcount inside its lock and assigned
    outside it, so a raise left the count at 1 with the partition unassigned.
    The next perfectly healthy agent to want that partition took the count to 2
    and therefore never triggered the 0 -> 1 assign.
    """
    from apps.transport.lane_topics import LANE_BCAST

    broadcast = f'{run_id}/scheduler_1/ORSimAgent'
    first = transport_factory('agent_a')
    second = transport_factory('agent_b')
    bus = first._bus
    assert second._bus is bus

    bus._consumer = _ProxyConsumer(bus._consumer, fail_times=1)
    with pytest.raises(RuntimeError):
        first.subscribe(broadcast)

    # A healthy agent boots next.  It must get a real assignment.
    second.loop_start()
    received = []
    second.on_message = lambda _t, _u, msg: received.append(msg.topic)
    second.subscribe(broadcast)

    assert (bus.lane_topics[LANE_BCAST], 0) in bus._current_assignment()

    publisher = transport_factory('scheduler_1')
    publisher.publish(broadcast, '{"step": 1}')
    assert _wait(lambda: received), \
        'the second agent received nothing: the leaked refcount is back'
    assert received[0] == broadcast


# ---------------------------------------------------------------------------
# FINDING 2 (critical) -- one bad record used to kill the only consumer thread.
# ---------------------------------------------------------------------------

def test_a_non_utf8_topic_header_is_foreign_not_fatal():
    """``orsim_topic_of`` is TOTAL by contract: it runs on the poll thread.

    ``auto.create.topics.enable`` is true on this broker, so a lane topic is
    writable by anything; ``value.decode('utf-8')`` on a malformed header raised
    ``UnicodeDecodeError`` straight out of the poll loop.
    """
    from apps.transport.lane_topics import orsim_topic_of

    class _Bad:
        def headers(self):
            return [('orsim_topic', b'\xff\xfe bad')]

    class _Raises:
        def headers(self):
            raise RuntimeError('librdkafka said no')

    assert orsim_topic_of(_Bad()) is None
    assert orsim_topic_of(_Raises()) is None


@needs_broker
def test_a_record_that_raises_does_not_kill_the_poll_thread(transport_factory, run_id):
    """The process's ONE consumer thread must outlive any single record.

    Measured before the fix: one malformed record left ``poll thread alive:
    False``, ``bus.is_closed(): False``, the bus still in the registry, and
    every subsequent broadcast undelivered -- with all counters reading zero,
    because the counter-printing path runs ON the dead thread.
    """
    app_topic = f'{run_id}/agent_boom'
    transport = transport_factory('agent_boom')
    bus = transport._bus
    transport.loop_start()
    received = []
    transport.on_message = lambda _t, _u, msg: received.append(msg.payload)
    transport.subscribe(app_topic)

    real_on_record = bus._on_record
    state = {'poisoned': True}

    def _poisoned(msg):
        if state['poisoned']:
            state['poisoned'] = False
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'injected')
        return real_on_record(msg)

    bus._on_record = _poisoned

    transport.publish(app_topic, '{"seq": 1}')      # this one is eaten
    assert _wait(lambda: bus.record_errors >= 1), 'the failure must be COUNTED'
    assert bus._poll_thread.is_alive(), 'the poll thread must survive a bad record'
    assert not bus.is_closed()

    transport.publish(app_topic, '{"seq": 2}')      # ... and this one still arrives
    assert _wait(lambda: received), 'the process went deaf after one bad record'


@needs_broker
def test_a_dead_poll_thread_makes_the_bus_unusable_rather_than_invisible(run_id):
    """A corpse must never be handed to the next agent that boots.

    With ``is_closed()`` reading only the flag, a bus whose poll thread had died
    stayed in the registry and every later agent attached to it -- each of them
    deaf for the rest of the run, counters frozen, nothing logged.
    """
    from apps.transport.kafka_bus import acquire_bus, get_bus

    bus = acquire_bus(BOOTSTRAP, run_id, SETTINGS)
    assert not bus.is_closed()

    class _DeadThread:
        def is_alive(self):
            return False

    bus._poll_thread = _DeadThread()
    assert bus.is_closed(), 'a dead poll thread is a closed bus'

    replacement = acquire_bus(BOOTSTRAP, run_id, SETTINGS)
    assert replacement is not bus, 'acquire_bus must build a fresh bus over a corpse'
    assert get_bus(BOOTSTRAP, run_id) is replacement
    replacement.shutdown()


# ---------------------------------------------------------------------------
# FINDINGS 3 and 7 (major) -- shutting the LOSER down evicted the WINNER.
# ---------------------------------------------------------------------------

@needs_broker
def test_discarding_a_construction_race_loser_leaves_the_winner_registered(
        transport_factory, run_id):
    """One bus per process is the central design decision of the whole phase.

    ``_teardown_resources`` popped ``self.key`` unconditionally, and the loser's
    key is identical to the winner's.  Measured: 4 of 5 concurrent 17-greenlet
    boots ended with ``len(_REGISTRY) == 0`` while a live bus served all 17
    handles -- ``all_bus_stats()`` returned ``[]``, ``get_bus`` returned None,
    and the next agent to boot built a SECOND consumer, producer and poll thread.
    """
    from apps.transport import kafka_bus as kb

    transport = transport_factory('agent_reg')
    winner = transport._bus
    assert kb.get_bus(BOOTSTRAP, run_id) is winner

    loser = kb._KafkaBus(BOOTSTRAP, run_id, SETTINGS)     # what acquire_bus builds
    loser.discard()                                        # ... and what it now calls
    assert kb.get_bus(BOOTSTRAP, run_id) is winner, \
        'discarding the loser evicted the live winner from the registry'

    # shutdown() must be safe on the same key too: the identity check, not the
    # call site, is what is unconditionally correct.
    other = kb._KafkaBus(BOOTSTRAP, run_id, SETTINGS)
    other.shutdown()
    assert kb.get_bus(BOOTSTRAP, run_id) is winner
    assert not winner.is_closed()
    assert winner._poll_thread.is_alive()
    assert [row for row in kb.all_bus_stats() if row['run_id'] == run_id], \
        'the live bus must stay visible to the operator-facing view'


# ---------------------------------------------------------------------------
# FINDING 4 (major) -- reaping a live run's lane topics re-partitions it.
# ---------------------------------------------------------------------------

def test_a_lane_topic_with_fewer_partitions_than_requested_is_fatal():
    """``app=1`` where the run needs ``app=64`` is the auto-recreate signature.

    Publishers and subscribers would then compute different partitions from the
    same key and records would vanish with no error on either side -- what
    ``lane_admin``'s own docstring calls the worst failure shape this project
    has.  MORE partitions than requested stays legal: that is the documented
    "broker is authoritative" asymmetry.
    """
    from apps.transport.lane_admin import _reject_shrunken_lanes

    names = {'bcast': 'orsim_bcast_run_x', 'reply': 'orsim_reply_run_x',
             'app': 'orsim_app_run_x'}
    wanted = {'bcast': 1, 'reply': 8, 'app': 64}

    ok = _reject_shrunken_lanes({'bcast': 1, 'reply': 8, 'app': 64}, wanted, names, 'run_x')
    assert ok == {'bcast': 1, 'reply': 8, 'app': 64}

    adopted = _reject_shrunken_lanes({'bcast': 1, 'reply': 8, 'app': 128},
                                     wanted, names, 'run_x')
    assert adopted['app'] == 128, 'more partitions than asked for is the documented rule'

    with pytest.raises(RuntimeError) as excinfo:
        _reject_shrunken_lanes({'bcast': 1, 'reply': 8, 'app': 1}, wanted, names, 'run_x')
    assert 'num.partitions=1' in str(excinfo.value)


@needs_broker
def test_clients_may_not_auto_create_a_lane_topic(transport_factory, run_id):
    """The producer is what actually resurrected a reaped topic in the repro.

    ~20 straggler publishes from a bus that outlived the reap recreated the lane
    topic at ONE partition and reported ``delivery_failures 0,
    dropped_publishes 0`` -- a clean success on top of total silent loss.

    THIS USED TO ASSERT TWO VALUES OUT OF A CONFIG DICT, and that was cited as
    proof that such publishes "fail loudly against ``delivery_failures``" --
    which is false on the timescale the bus actually lives: with
    ``allow.auto.create.topics=False`` a produce to a missing topic gets no
    delivery report until ``message.timeout.ms``, long after the 2 s flush
    budget.  A stubbed-contract test of exactly the CLAUDE.md 6.15 shape.  The
    broker answers it now: the topic must NOT come back, and the loss must be
    COUNTED rather than only logged.
    """
    from confluent_kafka.admin import AdminClient

    mine = f'{run_id}/truck_autocreate'
    transport = transport_factory('truck_autocreate')
    transport.loop_start()
    transport.subscribe(mine)
    bus = transport._bus

    assert bus._consumer_config()['allow.auto.create.topics'] is False
    assert bus._producer_config({})['allow.auto.create.topics'] is False

    app_topic = bus.lane_topics['app']
    admin = AdminClient({'bootstrap.servers': BOOTSTRAP})
    admin.delete_topics([app_topic], operation_timeout=10)
    assert _wait(lambda: app_topic not in admin.list_topics(timeout=5).topics,
                 timeout=20), 'the lane topic was not deleted'

    for i in range(20):
        transport.publish(mine, b'{"straggler": %d}' % i)
    remaining = bus.flush(2.0)

    assert app_topic not in admin.list_topics(timeout=5).topics, \
        'a client resurrected the lane topic -- it would come back at num.partitions=1'
    assert remaining > 0, 'the straggler publishes should not have been deliverable'
    assert bus.unflushed_messages >= remaining, \
        'a loss the code calls LOST must move a counter, not only a log line'
    assert bus.is_degraded() is True, bus.stats()


def test_the_ops_sweep_will_not_delete_a_run_younger_than_the_floor(monkeypatch, caplog):
    """``reap_lane_topics_older_than(0)`` used to be a live-run killer.

    It has no liveness check of any kind and never can have one, so the age
    floor is the guard.  Runs on this box finish in 6-16 minutes; an hour is
    many multiples of the longest ever measured.
    """
    from apps.transport import lane_admin

    now = time.time()
    old_stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(now - 48 * 3600))
    new_stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(now - 60))
    old_topic = f'orsim_app_run_{old_stamp}'
    new_topic = f'orsim_app_run_{new_stamp}'

    class _Meta:
        topics = {old_topic: object(), new_topic: object(), 'run_status': object()}

    class _Admin:
        def list_topics(self, timeout=10):
            return _Meta()

    deleted = []
    monkeypatch.setattr(lane_admin, '_admin', lambda bootstrap: _Admin())
    monkeypatch.setattr(lane_admin, '_delete_topics',
                        lambda names, bootstrap: deleted.extend(names) or list(names))

    with caplog.at_level(logging.ERROR):
        lane_admin.reap_lane_topics_older_than(0.0, BOOTSTRAP)

    assert deleted == [old_topic], \
        'the one-minute-old run must survive a sweep that asked for age 0'
    assert 'below the' in caplog.text and 'floor' in caplog.text

    # dry_run is free to look at anything: it deletes nothing.
    assert set(lane_admin.reap_lane_topics_older_than(0.0, BOOTSTRAP, dry_run=True)) == \
        {old_topic, new_topic}
    assert deleted == [old_topic]


def test_run_finalize_sweeps_by_AGE_and_never_deletes_its_own_run(monkeypatch):
    """Finalize is not a quiet moment -- stragglers and 30 s bus lingers are live.

    So ``SimulationRuntime`` must not call ``reap_run_lane_topics`` for the run
    it has just finished.  This asserts the call it DOES make.
    """
    from apps.simulation import simulation_runtime as sr
    from apps.transport import lane_admin

    monkeypatch.setitem(sr.__dict__, '_unused', None)   # keeps the import graph honest

    calls = {}
    monkeypatch.setattr(lane_admin, 'reap_lane_topics_older_than',
                        lambda hours, bootstrap=None, dry_run=False:
                        calls.setdefault('hours', hours) or [])

    def _boom(*_a, **_k):
        raise AssertionError('run finalize must NOT delete its own live lane topics')

    monkeypatch.setattr(lane_admin, 'reap_run_lane_topics', _boom)

    import apps.config as config
    monkeypatch.setitem(config.messenger_backend, 'AGENT_TRANSPORT', 'kafka')
    monkeypatch.setitem(config.messenger_backend, 'KAFKA_BOOTSTRAP_SERVERS', BOOTSTRAP)

    runtime = sr.SimulationRuntime.__new__(sr.SimulationRuntime)
    runtime.run_id = 'run_20260101_000000'
    runtime._reap_transport_lane_topics()

    assert calls['hours'] == lane_admin.DEFAULT_RETENTION_HOURS


# ---------------------------------------------------------------------------
# FINDINGS 5 and 8 (minor/major) -- the nap/wake assign, and the hub block.
# ---------------------------------------------------------------------------

@needs_broker
def test_the_broadcast_partition_is_STICKY_across_a_full_nap(transport_factory, run_id):
    """Once any local handle has wanted it, the bus keeps it for its lifetime.

    The broadcast refcount is PROCESS-wide, so the last co-resident agent to nap
    indefinitely drives it to 0 and the first waker paid a fresh 0 -> 1 assign --
    a broker round trip, from inside ``on_message``.  The refcount BOOKKEEPING is
    unchanged (``test_a_lane_partition_is_released_only_when_the_LAST_handle_detaches``
    still holds); only the consumer is left alone.
    """
    from apps.transport.lane_topics import LANE_BCAST

    broadcast = f'{run_id}/scheduler_1/ORSimAgent'
    agent = transport_factory('agent_napper')
    bus = agent._bus
    bcast_topic = bus.lane_topics[LANE_BCAST]

    agent.loop_start()
    agent.subscribe(broadcast)
    assert (bcast_topic, 0) in bus._current_assignment()

    agent.unsubscribe(broadcast)                       # the indefinite nap
    assert (LANE_BCAST, 0) not in bus._partition_refs, 'bookkeeping must be unchanged'
    assert (bcast_topic, 0) in bus._current_assignment(), \
        'the broadcast partition must stay assigned across a full nap'

    watermarks = []
    real = bus._watermark_once
    bus._watermark_once = lambda t, p: watermarks.append((t, p)) or real(t, p)

    received = []
    agent.on_message = lambda _t, _u, msg: received.append(msg.topic)
    agent.subscribe(broadcast)                          # the wake
    assert watermarks == [], 'the wake must not cost a watermark round trip'

    transport_factory('scheduler_1').publish(broadcast, '{"step": 7}')
    assert _wait(lambda: received), 'the woken agent must receive the step broadcast'


@needs_broker
def test_an_app_lane_partition_is_still_released_on_the_last_detach(
        transport_factory, run_id):
    """Stickiness is the BROADCAST lane only -- the app lane is unchanged.

    ~1060 per-agent app topics hash over 64 partitions; holding every partition
    a departed agent once wanted would make every process read the whole lane.
    """
    from apps.transport.lane_topics import LANE_APP, partition_for

    app_topic = f'{run_id}/agent_leaver'
    agent = transport_factory('agent_leaver')
    bus = agent._bus
    part = partition_for(app_topic, bus.lane_partitions[LANE_APP])

    agent.subscribe(app_topic)
    assert (bus.lane_topics[LANE_APP], part) in bus._current_assignment()
    agent.unsubscribe(app_topic)
    assert (bus.lane_topics[LANE_APP], part) not in bus._current_assignment()


def test_the_watermark_query_is_bounded_per_attempt_not_per_budget():
    """10 s of budget must be ~10 yielding attempts, not two 5 s hub blocks.

    Positive-controlled measurement of the old shape: a watchdog greenlet ran
    122,345 slices in the 0.5 s before ``get_watermark_offsets`` and ZERO during
    a 5.0007 s call.  The per-attempt bound is the half of the fix that survives
    even when ``tpool`` is unavailable.
    """
    from apps.transport import kafka_bus as kb

    assert kb.WATERMARK_QUERY_TIMEOUT_S <= 1.0
    assert kb.WATERMARK_RESOLVE_TIMEOUT_S / kb.WATERMARK_QUERY_TIMEOUT_S >= 5


def test_the_transport_docstring_no_longer_claims_zero_blocking_calls():
    """The claim was false, and a false premise is what the next reader builds on.

    ``subscribe()`` makes one broker round trip on a 0 -> 1 partition transition,
    as ``_ensure_assigned``'s own docstring has always admitted.
    """
    from apps.transport import kafka_transport

    doc = kafka_transport.__doc__
    assert 'ZERO NETWORK CALLS, ZERO BLOCKING CALLS' not in doc
    assert 'ONE ROUND TRIP ON A 0 -> 1 PARTITION TRANSITION' in doc


# ---------------------------------------------------------------------------
# FINDING 6 (minor) -- no transport failure ever reached an operator.
# ---------------------------------------------------------------------------

@needs_broker
def test_transport_health_aggregates_and_flags_a_degraded_process(transport_factory,
                                                                  run_id, caplog):
    """A run that lost transport traffic must not be quotable as clean.

    ``all_bus_stats`` and ``shutdown_all_buses`` had ZERO production callers, and
    ``_log_counters`` ran only from the poll thread's self-reap 30 s after the
    last handle released -- the exact path a dead poll thread makes unreachable.
    """
    from apps.transport.kafka_bus import log_transport_health, transport_health

    transport = transport_factory('agent_health')
    transport.loop_start()
    received = []
    transport.on_message = lambda *args: received.append(args[-1])
    own = f'{run_id}/agent_health'
    transport.subscribe(own)
    bus = transport._bus

    # NORMAL CROSS-AGENT TRAFFIC FIRST.  This assertion used to be made on a bus
    # that had received nothing at all, which is why it could pass while
    # ``is_degraded()`` was in fact True on every real run: app-lane partitions
    # are shared by hash, so a healthy process fetches ~46,000 records no local
    # handle wants and ``unrouted_messages`` was in DEGRADED_COUNTERS.
    transport.publish(own, b'{"mine": 1}')
    assert _wait(lambda: received), 'the handle never received its own record'
    foreign = None
    from apps.transport.lane_topics import partition_for
    target = partition_for(own, bus.lane_partitions['app'])
    for i in range(10000):
        candidate = f'{run_id}/agent_other_{i}'
        if partition_for(candidate, bus.lane_partitions['app']) == target:
            foreign = candidate
            break
    assert foreign is not None
    for i in range(5):
        transport.publish(foreign, b'{"theirs": 1}')
    assert _wait(lambda: bus.unrouted_messages >= 5), bus.stats()

    health = transport_health(run_id)
    assert health['buses'] == 1
    assert health['degraded'] is False, \
        f'normal cross-agent app-lane traffic must not read as degraded: {health}'

    bus.delivery_failures = 3
    bus.inbox_overflow = 2
    health = transport_health(run_id)
    assert health['degraded'] is True
    assert health['totals']['delivery_failures'] == 3
    assert health['totals']['inbox_overflow'] == 2

    with caplog.at_level(logging.ERROR):
        log_transport_health('under test')
    assert 'ORSIM TRANSPORT DEGRADED' in caplog.text, \
        'the marker must be greppable in journalctl -u openride-celery'


def test_the_counters_are_printed_even_when_the_process_just_exits():
    """A killed celery worker used to print nothing at all.

    Runs in a child so the ``atexit`` hook is genuinely exercised at interpreter
    shutdown rather than simulated by calling it.
    """
    program = (
        'import apps.transport.kafka_bus as kb\n'
        'class _Fake:\n'
        '    key = ("b", "r")\n'
        '    def _log_counters(self, reason="closed"):\n'
        '        print("ORSIM TRANSPORT DEGRADED: fake " + reason)\n'
        'kb._REGISTRY[_Fake.key] = _Fake()\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', program], cwd=REPO_ROOT, capture_output=True,
        text=True, timeout=60,
        env={**os.environ, 'PYTHONPATH': os.environ.get('PYTHONPATH', '')})
    assert result.returncode == 0, result.stderr
    assert 'ORSIM TRANSPORT DEGRADED: fake process exit' in result.stdout


# ---------------------------------------------------------------------------
# FINDING 9 (minor) -- the handoff deque was unbounded with no counter.
# ---------------------------------------------------------------------------

@needs_broker
def test_the_handoff_queue_applies_backpressure_instead_of_growing(run_id):
    """Bound it by NOT FETCHING, never by dropping.

    The per-handle ``_inbox`` drops its oldest because a wedged agent is already
    broken.  This deque must not: the record it would lose is a step broadcast
    the barrier is waiting on.  librdkafka's upstream buffer is order 1 GB
    (64 MB per partition, ~18 partitions), so materialising it as Python objects
    while the hub is parked is an OOM vector with nothing logged.
    """
    from apps.transport import kafka_bus as kb

    bus = kb._KafkaBus(BOOTSTRAP, run_id, SETTINGS)     # built, deliberately not started
    try:
        bus._patched = True                              # take the dispatcher path
        bus._queue.extend(object() for _ in range(kb.QUEUE_HIGH_WATER))

        class _CountingConsumer:
            def __init__(self):
                self.polls = 0

            def poll(self, timeout):
                self.polls += 1
                return None

        counting = _CountingConsumer()
        bus._consumer = counting

        thread = kb._native_threading.Thread(target=bus._poll_forever, daemon=True)
        thread.start()
        assert _wait(lambda: bus.queue_stalls > 0, timeout=3.0)
        assert counting.polls == 0, 'the poll thread must stop FETCHING while backed up'

        bus._queue.clear()                               # the dispatcher caught up
        assert _wait(lambda: counting.polls > 0, timeout=3.0), \
            'fetching must resume once the deque drains'

        bus._stopping = True
        thread.join(3.0)
        assert not thread.is_alive()
        assert bus.stats()['queue_stalls'] == bus.queue_stalls
    finally:
        bus._consumer = None
        bus.discard()


# ---------------------------------------------------------------------------
# FINDING 10 (minor) -- acquire_bus's check-then-act against the reaper.
# ---------------------------------------------------------------------------

@needs_broker
def test_retain_fails_closed_on_a_reaped_bus(transport_factory, run_id):
    """``retain()`` never re-checked ``_closed``, so it silently resurrected one.

    The new handle was then bound to a bus with no poll thread, no dispatcher and
    a destroyed producer: that agent receives nothing for the rest of the run and
    the barrier burns ``STEP_TIMEOUT`` on it every step, with nothing raised and
    nothing counted.
    """
    bus = transport_factory('agent_retain')._bus
    assert bus.retain() is True
    bus.release()

    bus._closed = True
    assert bus.retain() is False, 'retain() must fail closed'
    bus._closed = False
    bus._stopping = True
    assert bus.retain() is False, 'a bus that is STOPPING is already gone'


@needs_broker
def test_acquire_bus_rebuilds_when_the_registered_bus_cannot_be_retained(run_id):
    """A False retain must be treated exactly like a registry miss."""
    from apps.transport import kafka_bus as kb

    key = (BOOTSTRAP, run_id)

    class _Corpse:
        """Alive by the flag, dead by the time you try to claim it."""

        key = (BOOTSTRAP, run_id)
        retains = 0

        def is_closed(self):
            return False

        def retain(self):
            _Corpse.retains += 1
            return False

    corpse = _Corpse()
    with kb._REGISTRY_LOCK:
        kb._REGISTRY[key] = corpse
    try:
        bus = kb.acquire_bus(BOOTSTRAP, run_id, SETTINGS)
        assert bus is not corpse
        assert _Corpse.retains == 1
        assert kb.get_bus(BOOTSTRAP, run_id) is bus
        bus.shutdown()
    finally:
        with kb._REGISTRY_LOCK:
            if kb._REGISTRY.get(key) is corpse:
                kb._REGISTRY.pop(key, None)
