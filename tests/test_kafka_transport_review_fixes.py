"""One test per finding from the adversarial review of the phase-2 transport.

Every test here pins a defect that was REPRODUCED against the live broker at
localhost:9094 before it was fixed, and every one of them is the same shape the
rest of this package exists to defeat: the transport lost traffic (or claimed to
have lost traffic it had not), while the run would still have reported
``status: completed, ok: true, error: null``.

TWO RULES THIS FILE FOLLOWS, because the review found tests that broke both:

* **No stubbed contracts.**  ``test_clients_may_not_auto_create_a_lane_topic``
  used to assert two values out of a config dict and was cited as proof that
  undeliverable publishes "fail loudly against ``delivery_failures``" -- which
  was false on the timescale the bus actually lives.  Where the claim is about
  the broker, the broker answers it (CLAUDE.md 6.15).
* **No counter is driven by assignment when real traffic can drive it.**
  ``test_transport_health_aggregates_and_flags_a_degraded_process`` set
  ``delivery_failures = 3`` by hand on a bus that had received nothing, so it
  green-lit the degraded flag without ever seeing normal cross-agent traffic --
  which is exactly the traffic that made the flag fire on every healthy run.
"""

import json
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

    rid = dated_run_id('rev')
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


def _wait(predicate, timeout=8.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _colliding_app_topic(mine: str, run_id: str, partitions: int) -> str:
    """Another agent's app topic that hashes onto the SAME app partition as ``mine``.

    This is the normal case, not a contrivance: ~16 agent topics share each of
    the 64 app partitions in a 1060-agent run.
    """
    from apps.transport.lane_topics import partition_for

    target = partition_for(mine, partitions)
    for i in range(10_000):
        candidate = f'{run_id}/truck_{i:05d}'
        if candidate != mine and partition_for(candidate, partitions) == target:
            return candidate
    raise AssertionError('no colliding app topic found')


# ---------------------------------------------------------------------------
# FINDINGS 1 / 6 / 13 (major, reported three times) -- ``unrouted_messages``
# made ``is_degraded()`` unconditionally True on every HEALTHY run.
# ---------------------------------------------------------------------------

@needs_broker
def test_normal_cross_agent_app_lane_traffic_is_not_degraded(transport_factory, run_id, caplog):
    """A foreign app-lane record is NORMAL and must not raise the alarm.

    App-lane partitions are shared by hash, so a process routinely fetches
    records belonging to agents in other processes: the module's own arithmetic
    puts that at ~46,000 rejects per process per run.  With
    ``unrouted_messages`` in ``DEGRADED_COUNTERS`` that made all ~64 buses log
    the greppable ``ORSIM TRANSPORT DEGRADED`` marker on every clean run, so the
    one signal meant to separate a lossy run from a clean one fired 100% of the
    time -- the crying-wolf inverse of the CLAUDE.md 8 / 6.23 silent-loss shape
    the counter was added to catch.
    """
    from apps.transport.kafka_bus import transport_health

    mine = f'{run_id}/truck_mine'
    transport = transport_factory('truck_mine')
    transport.loop_start()
    received = []
    transport.on_message = lambda *args: received.append(args[-1])
    transport.subscribe(mine)

    bus = transport._bus
    foreign = _colliding_app_topic(mine, run_id, bus.lane_partitions['app'])

    # Publish to our own topic first, so the bus has genuinely routed something
    # (``messages_out > 0``) and cannot be mistaken for a routing blackhole.
    transport.publish(mine, json.dumps({'hello': 1}))
    assert _wait(lambda: received), 'the handle never received its own record'

    for i in range(20):
        transport.publish(foreign, json.dumps({'foreign': i}))
    assert _wait(lambda: bus.unrouted_messages >= 20), \
        f'expected the foreign records to be fetched and rejected: {bus.stats()}'

    stats = bus.stats()
    for name in ('dropped_publishes', 'delivery_failures', 'foreign_messages',
                 'inbox_overflow', 'consume_errors', 'record_errors',
                 'unflushed_messages', 'handler_errors', 'dispatcher_deaths',
                 'bus_rebinds'):
        assert stats[name] == 0, f'{name} moved on a healthy run: {stats}'

    assert bus.is_degraded() is False, stats
    assert transport_health(run_id)['degraded'] is False

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        bus._log_counters('under test')
    assert 'ORSIM TRANSPORT DEGRADED' not in caplog.text, \
        'a healthy bus must not print the marker an operator greps for'


def test_a_total_routing_failure_IS_degraded():
    """The signature ``unrouted_messages`` was meant to catch, as a ratio.

    A bus holding ``app=64`` while the publisher holds ``app=1`` (the lane-topic
    resurrection hazard) fetches records all run and delivers none of them, with
    every loss counter at zero.  ``messages_out == 0`` over a floor is that
    signature at its extreme -- and unlike a non-zero test on
    ``unrouted_messages`` it is false on every healthy run.
    """
    from apps.transport.kafka_bus import (ROUTING_BLACKHOLE_MIN_MESSAGES,
                                          degraded_from, routing_blackhole)

    healthy = {'messages_in': 47_000, 'messages_out': 1_200,
               'unrouted_messages': 45_800}
    assert routing_blackhole(healthy) is False
    assert degraded_from(healthy) is False

    blackhole = {'messages_in': ROUTING_BLACKHOLE_MIN_MESSAGES,
                 'messages_out': 0, 'unrouted_messages': ROUTING_BLACKHOLE_MIN_MESSAGES}
    assert routing_blackhole(blackhole) is True
    assert degraded_from(blackhole) is True

    # A just-booted bus has routed nothing yet and must not be accused.
    assert routing_blackhole({'messages_in': 3, 'messages_out': 0}) is False


def test_unrouted_messages_is_reported_but_is_not_a_verdict():
    from apps.transport.kafka_bus import DEGRADED_COUNTERS, DIAGNOSTIC_COUNTERS, _KafkaBus

    assert 'unrouted_messages' not in DEGRADED_COUNTERS
    assert 'unrouted_messages' in DIAGNOSTIC_COUNTERS
    assert 'queue_stalls' not in DEGRADED_COUNTERS
    assert _KafkaBus.DEGRADED_COUNTERS is DEGRADED_COUNTERS
    # The genuine loss counters stay plain truthiness tests.
    for name in ('dropped_publishes', 'delivery_failures', 'inbox_overflow',
                 'consume_errors', 'record_errors', 'unflushed_messages',
                 'handler_errors', 'dispatcher_deaths', 'bus_rebinds'):
        assert name in DEGRADED_COUNTERS


# ---------------------------------------------------------------------------
# FINDING 2 (major) -- the one loss the code called ``they are LOST`` was
# counted nowhere, so ``transport_health`` reported a clean process.
# ---------------------------------------------------------------------------

@needs_broker
def test_an_unflushed_publish_is_counted_not_only_logged(transport_factory, run_id):
    """Delete the app lane topic under a live bus, then publish: the loss counts.

    This is the broker-restart / disk-alarm / operator-reap case.  With
    ``allow.auto.create.topics=False`` those produces get NO delivery report
    until ``message.timeout.ms``, i.e. long after the 2 s flush budget and after
    the bus is gone -- so ``delivery_failures`` cannot cover it and
    ``is_degraded()`` answered False on a process that had irrecoverably lost
    agent step replies.
    """
    from confluent_kafka.admin import AdminClient

    mine = f'{run_id}/truck_unflushed'
    transport = transport_factory('truck_unflushed')
    transport.loop_start()
    transport.subscribe(mine)
    bus = transport._bus
    app_topic = bus.lane_topics['app']

    admin = AdminClient({'bootstrap.servers': BOOTSTRAP})
    admin.delete_topics([app_topic], operation_timeout=10)
    assert _wait(lambda: app_topic not in admin.list_topics(timeout=5).topics,
                 timeout=20), 'the lane topic was not deleted'

    for i in range(20):
        transport.publish(mine, json.dumps({'reply': i}))

    remaining = bus.flush(2.0)
    assert remaining > 0, 'the publishes should not have been deliverable'
    assert bus.unflushed_messages >= remaining
    assert bus.is_degraded() is True, bus.stats()

    # And the clients may not resurrect it: that is what would re-partition the
    # run at num.partitions=1 and make records vanish with no error at all.
    assert app_topic not in admin.list_topics(timeout=5).topics
    assert bus._consumer_config()['allow.auto.create.topics'] is False
    assert bus._producer_config({})['allow.auto.create.topics'] is False


# ---------------------------------------------------------------------------
# FINDING 3 (minor) -- a disconnect landing mid-subscribe leaked a partition.
# ---------------------------------------------------------------------------

@needs_broker
def test_a_disconnect_during_a_subscribe_leaves_no_assigned_partition(transport_factory,
                                                                      run_id):
    """The window fix (5) opened: the watermark query now PARKS the greenlet.

    ``attach_topic``'s bookkeeping (route + partition refcount) lands before
    ``_ensure_assigned``, and the agent's own heartbeat greenlet
    (``handle_heartbeat_failure`` -> ``stop_listening`` ->
    ``Messenger.disconnect``) can run in between.  ``disconnect`` iterates
    ``list(self._topics)``, which does not yet contain the in-flight topic, so
    nothing detached it and the partition stayed assigned with a refcount nobody
    owned for the bus's whole lifetime.

    The disconnect is injected from inside ``attach_topic`` rather than timed
    against a real ``tpool`` yield: the point of the test is the ORDER, and a
    timing race would make it flaky in both directions.
    """
    transport = transport_factory('truck_raced')
    bus = transport._bus
    topic = f'{run_id}/truck_raced'

    real_attach = bus.attach_topic
    landed = {'disconnected': False}

    def attach_then_disconnect(name, handle):
        real_attach(name, handle)
        if not landed['disconnected']:
            landed['disconnected'] = True
            transport.disconnect()          # the heartbeat greenlet's teardown

    bus.attach_topic = attach_then_disconnect
    transport.subscribe(topic)
    bus.attach_topic = real_attach

    assert landed['disconnected']
    assert transport._topics == set(), 'a closed handle must not believe it is subscribed'
    assert bus._routes == {}, bus._routes
    assert bus._partition_refs == {}, bus._partition_refs
    held = {(tp.topic, tp.partition) for tp in (bus._consumer.assignment() or ())}
    assert all(t != bus.lane_topics['app'] for t, _ in held), held


@needs_broker
def test_a_closed_handle_cannot_attach_a_topic(transport_factory, run_id):
    transport = transport_factory('truck_closed')
    bus = transport._bus
    transport.disconnect()
    with pytest.raises(RuntimeError):
        bus.attach_topic(f'{run_id}/truck_closed', transport)
    assert bus._partition_refs == {}


@needs_broker
def test_drop_handle_releases_the_refcounts_of_the_routes_it_removes(transport_factory,
                                                                     run_id):
    """``drop_handle`` used to remove the route and leave the refcount standing."""
    transport = transport_factory('truck_dropped')
    bus = transport._bus
    topic = f'{run_id}/truck_dropped'
    transport.subscribe(topic)
    assert bus._partition_refs, 'precondition: the partition is held'

    # Simulate the leak's starting state: the handle still routed, as it is
    # when a disconnect raced its own subscribe.
    transport._topics.clear()
    bus.drop_handle(transport)

    assert bus._routes == {}
    assert bus._partition_refs == {}, bus._partition_refs


# ---------------------------------------------------------------------------
# FINDING 4 (minor) -- a reapable-but-undatable run id leaks three topics that
# no code path can ever collect, silently.
# ---------------------------------------------------------------------------

def test_an_undatable_run_id_is_warned_about_at_creation(caplog):
    from apps.transport.lane_admin import (_warn_about_uncollectable_names,
                                           lane_topic_names)

    names = lane_topic_names('run_mytest')
    with caplog.at_level(logging.WARNING):
        _warn_about_uncollectable_names(names, 'run_mytest')
    assert 'no parseable' in caplog.text
    assert 'leak' in caplog.text

    caplog.clear()
    good = lane_topic_names(dated_run_id('sweepable'))
    with caplog.at_level(logging.WARNING):
        _warn_about_uncollectable_names(good, 'whatever')
    assert caplog.text == ''


def test_a_future_run_stamp_is_undatable_rather_than_eternally_young(caplog):
    """``orsim_*_run_20990101_000001`` parsed fine and could never be older than
    the cutoff, so the sweep carried it forever while reporting nothing."""
    from apps.transport.lane_admin import _run_started_at

    assert _run_started_at('orsim_app_run_20260101_000000') is not None
    with caplog.at_level(logging.WARNING):
        assert _run_started_at('orsim_app_run_20990101_000001') is None
    assert 'FUTURE' in caplog.text


def test_the_test_fixtures_mint_run_ids_the_sweep_can_collect():
    """The suite's own debris is what filled the broker; this is the guard."""
    from apps.transport.lane_admin import (_run_started_at, is_reapable_lane_topic,
                                           lane_topic_names)

    for lane, name in lane_topic_names(dated_run_id('fixture')).items():
        assert is_reapable_lane_topic(name), name
        assert _run_started_at(name) is not None, name


# ---------------------------------------------------------------------------
# FINDING 5 (critical) -- librdkafka's 300 s default ``message.timeout.ms`` left
# the only error channel the design has blind for five minutes.
# ---------------------------------------------------------------------------

def test_the_producer_sets_an_explicit_delivery_budget():
    from apps.transport.kafka_bus import DEFAULT_MESSAGE_TIMEOUT_MS, _KafkaBus

    bus = _KafkaBus.__new__(_KafkaBus)
    bus.bootstrap = BOOTSTRAP
    config = bus._producer_config({})
    assert config['message.timeout.ms'] == DEFAULT_MESSAGE_TIMEOUT_MS
    assert DEFAULT_MESSAGE_TIMEOUT_MS <= 30_000, \
        'the budget must be short enough that a step still cares'
    assert config['message.timeout.ms'] >= config['linger.ms']


@needs_broker
def test_an_undeliverable_publish_reports_within_its_budget(run_id):
    """With the budget set, an undeliverable record fails LOUDLY and promptly.

    Measured before the fix with this exact producer config: ``flush(20)``
    returned 1 still queued and ``on_delivery`` fired ZERO times in 20 s.
    """
    from confluent_kafka import Producer

    from apps.transport.kafka_bus import _KafkaBus

    bus = _KafkaBus.__new__(_KafkaBus)
    bus.bootstrap = BOOTSTRAP
    config = bus._producer_config({'KAFKA_MESSAGE_TIMEOUT_MS': 2000})
    producer = Producer(config)

    errors = []
    missing = f'orsim_app_{run_id}_does_not_exist'
    started = time.monotonic()
    for i in range(5):
        producer.produce(missing, value=b'x', partition=0,
                         on_delivery=lambda err, msg: errors.append(err))
    producer.flush(10)
    elapsed = time.monotonic() - started

    assert errors, 'the delivery callback never fired at all'
    assert all(err is not None for err in errors), errors
    assert elapsed < 9.0, f'the report took {elapsed:.1f}s'
    assert any('MSG_TIMED_OUT' in str(err) or 'UNKNOWN_TOPIC' in str(err).upper()
               for err in errors), errors


# ---------------------------------------------------------------------------
# FINDING 7 (major) -- the dispatcher greenlet had no guard at all.
# ---------------------------------------------------------------------------

_DISPATCHER_CHILD = r'''
import json, os, sys
import eventlet
eventlet.monkey_patch()

bootstrap, run_id = sys.argv[1], sys.argv[2]
from apps.transport.kafka_bus import acquire_bus, transport_health
from apps.transport.kafka_transport import KafkaTransport

settings = {'KAFKA_BOOTSTRAP_SERVERS': bootstrap,
            'KAFKA_APP_PARTITIONS': 8, 'KAFKA_REPLY_PARTITIONS': 4}
handles = [KafkaTransport(settings, run_id, 'truck_%03d' % i) for i in range(3)]
seen = [0, 0, 0]
bcast = '%s/scheduler_1/ORSimAgent' % run_id
for i, h in enumerate(handles):
    h.loop_start()
    h.on_message = (lambda idx: (lambda *a: seen.__setitem__(idx, seen[idx] + 1)))(i)
    h.subscribe(bcast)
bus = handles[0]._bus

handles[0].publish(bcast, json.dumps({'step': 1}))
with eventlet.Timeout(10, False):
    while min(seen) < 1:
        eventlet.sleep(0.02)
healthy = list(seen)

eventlet.kill(bus._dispatcher)
eventlet.sleep(0.2)
out = {
    'healthy': healthy,
    'dispatcher_alive': bus.stats()['dispatcher_alive'],
    'dispatcher_deaths': bus.dispatcher_deaths,
    'is_closed': bus.is_closed(),
    'is_degraded': bus.is_degraded(),
    'health_degraded': transport_health(run_id)['degraded'],
}
print('RESULT ' + json.dumps(out))
# The poll thread notices _stopping and runs the ordinary teardown, which logs
# the counters AND publishes them to the cross-process health topic. Give it a
# moment, then make sure the snapshot actually left this process.
eventlet.sleep(1.0)
from apps.transport.health_stream import flush_health
flush_health(5.0)
'''


@needs_broker
def test_a_dead_dispatcher_is_visible_and_closes_the_bus(run_id):
    """Total silent deafness for every agent in the process, now reported.

    REPRODUCED before the fix: with the dispatcher dead, three agents received
    0 of 5 broadcasts while ``is_closed()``, ``is_degraded()`` and
    ``transport_health()['degraded']`` all answered False, ``stats()`` had no
    dispatcher field at all, and the teardown line printed at INFO.
    """
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (os.getenv('PYTHONPATH'), '/home/user/orsim', REPO_ROOT) if p))
    completed = subprocess.run(
        [sys.executable, '-c', _DISPATCHER_CHILD, BOOTSTRAP, run_id],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert 'RESULT ' in completed.stdout, completed.stdout + completed.stderr
    out = json.loads(completed.stdout.split('RESULT ', 1)[1].splitlines()[0])

    assert min(out['healthy']) >= 1, f'fan-out was broken before the kill: {out}'
    assert out['dispatcher_alive'] is False, out
    assert out['dispatcher_deaths'] >= 1, out
    assert out['is_closed'] is True, 'a bus that cannot deliver must not be handed out'
    assert out['is_degraded'] is True, out
    # ``transport_health()`` is registry-backed and a torn-down bus deregisters
    # itself, so the IN-PROCESS verdict can legitimately be empty by the time it
    # is asked. The verdict that matters is the one that left the process: the
    # teardown publishes the counters to the cross-process health topic, which
    # is what the run's own finalize path reads. This is findings 7 and 8
    # meeting end to end.
    from apps.transport.health_stream import collect_transport_health

    health = collect_transport_health(run_id, BOOTSTRAP, timeout=15.0)
    assert health['available'] is True, health
    assert health['degraded'] is True, health
    assert health['totals']['dispatcher_deaths'] >= 1, health


# ---------------------------------------------------------------------------
# FINDINGS 8 / 17 (major, reported twice) -- no transport counter reached the
# run's outcome, so a lossy run was indistinguishable from a clean one.
# ---------------------------------------------------------------------------

@needs_broker
def test_a_worker_s_counters_reach_another_process_s_verdict(run_id):
    """The cross-process channel, end to end.

    ``transport_health()`` is per-PROCESS and ``summary.json`` is written by a
    process that hosts no buses, so before this the only evidence of a loss was
    64 ERROR lines in a journal nobody greps -- CLAUDE.md 6.21's channel,
    recorded as missed twice in one session.
    """
    from apps.transport.health_stream import (collect_transport_health, flush_health,
                                              publish_bus_health)

    clean = {'run_id': run_id, 'pid': 4242, 'messages_in': 5000, 'messages_out': 4000,
             'unrouted_messages': 1000}
    publish_bus_health(BOOTSTRAP, run_id, clean, reason='periodic')
    assert flush_health(10.0) == 0, 'the snapshot was not delivered'

    health = collect_transport_health(run_id, BOOTSTRAP, timeout=10.0)
    assert health['available'] is True, health
    assert health['degraded'] is False, health
    assert health['totals']['unrouted_messages'] == 1000, health

    lossy = dict(clean, pid=4243, delivery_failures=7)
    publish_bus_health(BOOTSTRAP, run_id, lossy, reason='closed')
    assert flush_health(10.0) == 0

    health = collect_transport_health(run_id, BOOTSTRAP, timeout=10.0)
    assert health['processes'] == 2, health
    assert health['degraded'] is True, health
    assert health['totals']['delivery_failures'] == 7, health
    assert 4243 in health['degraded_pids'], health


def test_an_unreadable_health_topic_is_UNKNOWN_and_never_clean():
    """"No snapshots" and "no losses" are the same bytes; they must not be the
    same answer."""
    from apps.transport.health_stream import collect_transport_health

    health = collect_transport_health('run_20260101_000000_nope',
                                      'localhost:1',       # nothing listens here
                                      timeout=2.0)
    assert health['available'] is False
    assert health['degraded'] is None, 'unknown must never read as clean'


def test_the_run_finalize_path_folds_the_verdict_into_the_perf_summary():
    """The field has to exist on the SUMMARY, not only in a worker's journal."""
    import inspect

    from apps.simulation import simulation_runtime

    source = inspect.getsource(simulation_runtime.SimulationRuntime)
    assert '_collect_transport_health' in source
    assert 'transport_degraded' in source
    body = inspect.getsource(simulation_runtime.SimulationRuntime.on_simulation_complete)
    assert body.index('_collect_transport_health') < body.index('_publish_perf_summary'), \
        'the verdict must be collected before the summary that carries it'


# ---------------------------------------------------------------------------
# FINDING 9 (major) -- handles already bound to a bus that died were never told.
# ---------------------------------------------------------------------------

@needs_broker
def test_a_handle_rebinds_itself_when_its_bus_dies(transport_factory, run_id):
    """``acquire_bus`` only ever protected a NEW handle.

    REPRODUCED: with the poll loop ended, the three original handles still had
    ``loop_is_running True``, ``_closed False``, ``subscribe`` and ``publish``
    both returning ``(0, 0)``, and received none of the broadcasts a fresh bus
    delivered -- ~17 agents silently deaf, pruned by the scheduler, run reports
    ok: true.  In a real run no agent re-acquires a bus after boot, so there was
    no in-process recovery at all.
    """
    topic = f'{run_id}/truck_rebind'
    transport = transport_factory('truck_rebind')
    transport.loop_start()
    received = []
    transport.on_message = lambda *args: received.append(args[-1])
    transport.subscribe(topic)

    dead = transport._bus
    dead.shutdown(2.0)
    assert dead.is_closed() is True

    transport.publish(topic, json.dumps({'after': 'death'}))

    fresh = transport._bus
    assert fresh is not dead, 'the handle kept publishing into a corpse'
    assert fresh.is_closed() is False
    assert fresh.bus_rebinds == 1
    assert fresh.is_degraded() is True, 'a process that went deaf must not report clean'
    assert topic in transport._topics
    assert transport._topics == {topic}

    # And it is really wired up again: a record published now comes back.
    transport.publish(topic, json.dumps({'alive': True}))
    assert _wait(lambda: received), 'the rebound handle still receives nothing'


# ---------------------------------------------------------------------------
# FINDING 10 (minor) -- ``retries: 3`` without idempotence permits duplicates,
# and nothing in ORSim or the apps dedupes on ``time_step``.
# ---------------------------------------------------------------------------

def test_the_lane_producer_does_not_retry_into_a_duplicate():
    from apps.transport.kafka_bus import _KafkaBus

    bus = _KafkaBus.__new__(_KafkaBus)
    bus.bootstrap = BOOTSTRAP
    config = bus._producer_config({})
    assert config['retries'] == 0, (
        'retries without idempotence permits broker-side duplicates; a duplicated '
        'step broadcast makes every agent step twice for one time step')
    assert config['enable.idempotence'] is False, \
        'idempotence waits on the transaction coordinator at startup, project-wide'
    assert config['max.in.flight.requests.per.connection'] == 1, \
        'per-agent reply ORDER rests on this, not on linger'


# ---------------------------------------------------------------------------
# FINDING 11 (minor) -- the seam check ran AFTER the transport was built, so
# every agent boot on an orsim without the seam left a retained bus behind.
# ---------------------------------------------------------------------------

@needs_broker
def test_a_missing_orsim_seam_does_not_strand_a_retained_bus(monkeypatch, run_id):
    """The run must die loudly AND leave nothing pinned.

    REPRODUCED with ``seam_available()`` forced False (the installed-v1.3.0
    state the module is written for): five calls raised ``TransportSeamMissing``
    as intended and left a live bus with ``handles: 5`` and ``_reap_at: None``,
    i.e. one Consumer + one native poll thread pinned per worker across every
    subsequent run.
    """
    from apps import agent_transport
    from apps.transport.kafka_bus import get_bus, shutdown_all_buses

    from apps.transport.kafka_transport import KafkaTransport

    monkeypatch.setattr(agent_transport, 'seam_available', lambda: False)
    # The real path reads the flag off ORSimEnv.messenger_settings inside the
    # worker; here the build is forced directly, because what is under test is
    # the ORDER of build and check, not the flag plumbing.
    monkeypatch.setattr(
        agent_transport, 'build_transport',
        lambda run_id, identity, settings=None: KafkaTransport(SETTINGS, run_id, identity))

    try:
        for i in range(3):
            with pytest.raises(agent_transport.TransportSeamMissing):
                agent_transport.select_agent_transport(
                    (), {'run_id': run_id, 'unique_id': f'truck_{i:03d}',
                         'transport': None},
                    owner='test')
        bus = get_bus(BOOTSTRAP, run_id)
        if bus is not None:
            assert bus.stats()['handles'] == 0, bus.stats()
            assert bus._reap_at is not None, \
                'a bus with no handles must be able to reach its self-reap'
    finally:
        shutdown_all_buses()


@needs_broker
def test_a_transport_dropped_without_disconnect_still_releases_its_bus(run_id):
    """The GC fallback for the one path that skips ``disconnect`` entirely.

    ``ORSimAgent.start_listening`` subscribes BEFORE it spawns the teardown
    heartbeat greenlet, so a raise there left the handle's ``retain()`` standing
    forever and the bus could never reach its 30 s self-reap.
    """
    import gc

    from apps.transport.kafka_bus import get_bus, shutdown_all_buses
    from apps.transport.kafka_transport import KafkaTransport

    try:
        for i in range(3):
            KafkaTransport(SETTINGS, run_id, f'truck_gc_{i}')     # dropped at once
        gc.collect()
        bus = get_bus(BOOTSTRAP, run_id)
        assert bus is not None
        assert bus.stats()['handles'] == 0, bus.stats()
        assert bus._reap_at is not None
    finally:
        shutdown_all_buses()


# ---------------------------------------------------------------------------
# FINDING 12 (minor) -- a raising handler left the agent silent with every
# counter clean, and ``messages_out`` counted evicted messages as delivered.
# ---------------------------------------------------------------------------

@needs_broker
def test_a_raising_handler_is_counted_not_only_logged(transport_factory, run_id):
    """A truncated payload makes ORSim's own ``json.loads`` raise in the callback.

    The agent then never replies, the scheduler counts it unresponsive every
    step and eventually heartbeat-kills it -- the 15-killed-facility-agents
    shape of CLAUDE.md 6.23 -- while ``transport_health()`` reported the process
    clean and ``messages_out`` kept rising as if delivery were fine.
    """
    topic = f'{run_id}/truck_boom'
    transport = transport_factory('truck_boom')
    transport.loop_start()

    def boom(*_args):
        raise ValueError('Expecting value: line 1 column 1 (char 0)')

    transport.on_message = boom
    transport.subscribe(topic)
    transport.publish(topic, b'{not json')

    bus = transport._bus
    assert _wait(lambda: bus.handler_errors >= 1), bus.stats()
    assert bus.is_degraded() is True, bus.stats()


def test_messages_out_counts_enqueued_records_not_evicted_ones(transport_factory, run_id):
    from apps.transport.kafka_bus import _Message

    transport = transport_factory('truck_evict')
    transport._inbox_max = 2
    bus = transport._bus
    bus.messages_out = 0

    assert transport._deliver(_Message('t', b'1')) is True
    assert transport._deliver(_Message('t', b'2')) is True
    assert transport._deliver(_Message('t', b'3')) is False, 'this one evicted the oldest'
    assert bus.inbox_overflow == 1

    # What the bus would have counted, going through the real routing path.
    bus._routes['t'] = [transport]
    bus.messages_out = 0
    bus._route(_Message('t', b'4'))
    assert bus.messages_out == 0, 'an eviction is not a delivery'


# ---------------------------------------------------------------------------
# FINDING 14 (major) -- the dependency gating was inverted for the cutover.
# ---------------------------------------------------------------------------

def test_the_rabbit_gate_follows_the_celery_broker_url(monkeypatch):
    """The first run after RabbitMQ was deleted would have aborted in preflight.

    ``assert_simulation_dependencies`` hard-failed on a closed
    ``127.0.0.1:5672`` and pointed the operator at a service they had
    deliberately removed, while the new critical-path substrate had no check of
    any kind.
    """
    from apps.simulation import preflight

    monkeypatch.setattr(preflight, '_tcp_open', lambda host, port, timeout=3.0: port != 5672)
    monkeypatch.setattr(preflight, '_osrm_serving', lambda *a, **k: True)
    monkeypatch.setattr(preflight, 'celery_ping', lambda *a, **k: True)
    monkeypatch.setattr(preflight, '_agent_transport_name', lambda: 'paho')

    monkeypatch.setenv('CELERY_BROKER_URL', 'amqp://guest:guest@127.0.0.1:5672//')
    with pytest.raises(RuntimeError) as excinfo:
        preflight.assert_simulation_dependencies()
    assert 'RabbitMQ' in str(excinfo.value)

    monkeypatch.setenv('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
    preflight.assert_simulation_dependencies()      # must not raise


def test_a_non_paho_transport_gates_on_a_real_kafka_request(monkeypatch):
    from apps.simulation import preflight

    monkeypatch.setattr(preflight, '_tcp_open', lambda host, port, timeout=3.0: True)
    monkeypatch.setattr(preflight, '_osrm_serving', lambda *a, **k: True)
    monkeypatch.setattr(preflight, 'celery_ping', lambda *a, **k: True)
    monkeypatch.setattr(preflight, '_agent_transport_name', lambda: 'kafka')
    monkeypatch.setenv('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
    monkeypatch.setattr(preflight, '_kafka_serving',
                        lambda bootstrap, run_id, timeout: 'metadata request failed (boom)')

    with pytest.raises(RuntimeError) as excinfo:
        preflight.assert_simulation_dependencies('run_20260101_000000')
    assert 'Kafka' in str(excinfo.value)
    assert 'AGENT_TRANSPORT=kafka' in str(excinfo.value)


@needs_broker
def test_the_kafka_probe_issues_a_real_metadata_request():
    """A TCP check does not catch a wedged broker -- the 6.21 lesson."""
    from apps.simulation.preflight import _kafka_serving

    assert _kafka_serving(BOOTSTRAP, None, 5.0) is None
    reason = _kafka_serving('localhost:1', None, 2.0)
    assert reason and 'metadata' in reason


# ---------------------------------------------------------------------------
# FINDING 15 (major) -- a raise in ``start_listening`` excluded the agent from
# every step barrier for the whole run, with nothing failing.
# ---------------------------------------------------------------------------

def test_a_failed_start_listening_tears_the_agent_down_instead_of_escaping(monkeypatch):
    from orsim.lifecycle import ORSimAgent

    from apps.container_logistics.openride_agent import OpenRideAgent

    def boom(self):
        raise RuntimeError('incremental_assign failed: _CONFLICT')

    monkeypatch.setattr(ORSimAgent, 'start_listening', boom, raising=False)

    class _Concrete(OpenRideAgent):
        # OpenRideAgent is abstract; the seven members below are the ABC's, and
        # none of them is reached by this test.
        def _create_app(self): ...
        def entering_market(self): ...
        def estimate_next_event_time(self): ...
        def exiting_market(self): ...
        def logout(self): ...
        def process_payload_on_init(self, payload): ...
        def step(self): ...

    agent = _Concrete.__new__(_Concrete)
    agent.unique_id = 'truck_000'
    torn_down = []
    agent.stop_listening = lambda: torn_down.append(True)

    agent.start_listening()         # must NOT raise

    assert agent.agent_failed is True
    assert torn_down == [True], 'the failed agent must release its transport'


def test_an_agent_that_never_boots_is_logged_and_counted(caplog):
    """``pending_boot`` excluded an agent from ``expected`` with NO deadline.

    So an agent whose boot raised simply never participated: it was never
    pruned (pruning only touches agents that WERE expected), the run finished
    with fewer orders, and ``summary.json`` still said ok: true.
    """
    from apps.simulation.openride_scheduler import OpenRideScheduler

    sched = OpenRideScheduler.__new__(OpenRideScheduler)
    sched._boot_timed_out = 0
    sched.time = 0
    item = {'pending_boot': True, 'boot_step': 0}

    sched.time = OpenRideScheduler.BOOT_DEADLINE_STEPS - 1
    sched._check_boot_deadline('truck_000', item)
    assert sched._boot_timed_out == 0, 'a slow worker queue must not be accused'

    sched.time = OpenRideScheduler.BOOT_DEADLINE_STEPS
    with caplog.at_level(logging.ERROR):
        sched._check_boot_deadline('truck_000', item)
    assert sched._boot_timed_out == 1
    assert 'AGENT BOOT NEVER COMPLETED' in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        sched._check_boot_deadline('truck_000', item)
    assert sched._boot_timed_out == 1, 'once per agent, not once per step'
    assert caplog.text == ''


# ---------------------------------------------------------------------------
# FINDING 16 (minor) -- ``linger.ms=5`` was ~10 ms of pure latency per step.
# ---------------------------------------------------------------------------

def test_the_lane_producer_does_not_linger_by_default():
    from apps.config import messenger_backend
    from apps.transport.kafka_bus import _KafkaBus

    bus = _KafkaBus.__new__(_KafkaBus)
    bus.bootstrap = BOOTSTRAP
    assert bus._producer_config({})['linger.ms'] == 0
    assert bus._producer_config({'KAFKA_LINGER_MS': 5})['linger.ms'] == 5, \
        'the knob must still work for a measurement'
    assert messenger_backend['KAFKA_LINGER_MS'] == 0, \
        'the stamped default is what a run actually gets'
