"""What the conformance suite does not cover, and why each gap matters.

The suite is the authority on the CONTRACT: broadcast, additive subscribe, topic
fidelity, forward-only delivery, re-entrancy, flush-on-disconnect.  It is
deliberately silent about everything that is specific to THIS implementation --
the shared process-level bus, the partition refcount, the four counters, and the
absorb-don't-raise rules on the send path.  Those are what this file pins.

Every test here runs against the real broker.  An in-memory double would have to
re-implement partition assignment and header round-tripping, and would then be
testing the double.
"""

import logging
import os
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


def _broker_is_up(timeout=5.0):
    try:
        from confluent_kafka.admin import AdminClient
        metadata = AdminClient({'bootstrap.servers': BOOTSTRAP}).list_topics(timeout=timeout)
        return bool(metadata and metadata.brokers)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _broker_is_up(), reason=f'no Kafka broker at {BOOTSTRAP}')


@pytest.fixture
def run_id():
    """A fresh run id, whose lane topics are reaped afterwards."""
    from apps.transport.kafka_bus import shutdown_all_buses
    from apps.transport.lane_admin import reap_run_lane_topics

    rid = dated_run_id('unit')
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


# ---------------------------------------------------------------------------
# The send path absorbs, counts and logs.  It never raises.
# ---------------------------------------------------------------------------

class _AngryProducer:
    """A producer that always fails the way librdkafka fails."""

    def __init__(self, exc):
        self.exc = exc
        self.produce_calls = 0

    def produce(self, *args, **kwargs):
        self.produce_calls += 1
        raise self.exc

    def poll(self, timeout=0):
        return 0

    def flush(self, timeout=0):
        return 0


def test_publish_absorbs_buffererror_and_counts_it(transport_factory, run_id, caplog):
    """``publish()``'s most important call site is outside any ``try``.

    DURABILITY CONTRACT: raising here kills an agent mid-step.  A ``BufferError``
    means librdkafka's local queue is full; the documented remedy is to serve
    delivery callbacks and retry, which is why ``produce`` is attempted twice
    before the drop is counted.
    """
    transport = transport_factory('agent_buffer')
    angry = _AngryProducer(BufferError('Local: Queue full'))
    transport._bus._producer = angry

    with caplog.at_level(logging.ERROR):
        transport.publish(f'{run_id}/agent_buffer', '{"seq": 1}')   # must not raise

    assert angry.produce_calls == 2, 'a BufferError must be retried once after poll(0)'
    assert transport._bus.dropped_publishes == 1
    assert 'DROPPED a publish' in caplog.text


def test_publish_absorbs_kafkaexception_and_counts_it(transport_factory, run_id, caplog):
    from confluent_kafka import KafkaException

    transport = transport_factory('agent_kex')
    angry = _AngryProducer(KafkaException('boom'))
    transport._bus._producer = angry

    with caplog.at_level(logging.ERROR):
        transport.publish(f'{run_id}/agent_kex', '{"seq": 1}')

    assert angry.produce_calls == 1, 'a KafkaException is not retried'
    assert transport._bus.dropped_publishes == 1
    assert 'rejected' in caplog.text


def test_publish_absorbs_an_unexpected_exception_too(transport_factory, run_id):
    transport = transport_factory('agent_weird')
    transport._bus._producer = _AngryProducer(RuntimeError('something new'))
    transport.publish(f'{run_id}/agent_weird', '{"seq": 1}')
    assert transport._bus.dropped_publishes == 1


def test_a_delivery_report_error_is_counted_and_logged(transport_factory, caplog):
    """The ONLY channel on which a rejected produce is ever reported.

    ``produce()`` returned normally for an illegal ``'/'``-bearing topic against
    this very broker; the sole signal was this callback.  A transport that
    ignores it is the "lost 3,247 events, still ok: true" failure with extra
    steps.
    """
    transport = transport_factory('agent_dr')
    bus = transport._bus

    class _Msg:
        def topic(self):
            return 'orsim_app_whatever'

    with caplog.at_level(logging.ERROR):
        bus._on_delivery(None, _Msg())          # success: no counter, no log
        assert bus.delivery_failures == 0
        bus._on_delivery('Broker: Invalid topic', _Msg())

    assert bus.delivery_failures == 1
    assert 'delivery FAILED' in caplog.text


# ---------------------------------------------------------------------------
# Teardown is idempotent in either order
# ---------------------------------------------------------------------------

def test_disconnect_is_idempotent(transport_factory, run_id):
    transport = transport_factory('agent_idem')
    transport.loop_start()
    transport.subscribe(f'{run_id}/agent_idem')
    transport.disconnect()
    transport.disconnect()                     # must not raise, must not double-release
    assert transport._topics == set()
    assert transport._loop_running is False


def test_loop_stop_then_disconnect(transport_factory, run_id):
    transport = transport_factory('agent_ls_first')
    transport.loop_start()
    transport.subscribe(f'{run_id}/agent_ls_first')
    transport.loop_stop()
    transport.disconnect()
    assert transport._loop_running is False


def test_disconnect_then_loop_stop(transport_factory, run_id):
    """The order agents actually use: ``Messenger.disconnect`` skips ``loop_stop``
    for a ``channel_id=None`` Messenger, so a later stray ``loop_stop`` must be
    harmless rather than a second teardown."""
    transport = transport_factory('agent_dc_first')
    transport.loop_start()
    transport.subscribe(f'{run_id}/agent_dc_first')
    transport.disconnect()
    transport.loop_stop()
    assert transport._loop_running is False


def test_publish_after_disconnect_is_dropped_not_raised(transport_factory, run_id):
    transport = transport_factory('agent_late_pub')
    transport.loop_start()
    before = transport._bus.dropped_publishes
    transport.disconnect()
    transport.publish(f'{run_id}/agent_late_pub', '{"seq": 1}')   # must not raise
    assert transport._bus.dropped_publishes == before + 1


# ---------------------------------------------------------------------------
# Per-handle FIFO, including under a re-entrant publish from inside on_message
# ---------------------------------------------------------------------------

def test_per_handle_fifo_survives_a_reentrant_publish(transport_factory, run_id):
    """``_pump``'s ``_pumping`` flag must not reorder or lose anything.

    ``OpenRideAgent`` re-enters ``publish``/``subscribe``/``unsubscribe`` from
    inside ``on_receive_message``, and ``subscribe`` calls ``_pump``.  Without
    the flag that is a NESTED drain, which delivers a later message from inside
    an earlier one's handler -- inverting the order the barrier depends on.
    """
    from apps.transport.kafka_bus import _Message

    transport = transport_factory('agent_fifo')
    topic = f'{run_id}/agent_fifo'
    seen = []

    def on_message(client, userdata, message):
        seen.append(int(message.payload))
        # Re-enter all three, exactly as OpenRideAgent does on every nap/wake.
        transport.publish(f'{run_id}/scheduler_1/ORSimScheduler', '{"reply": 1}')
        transport.unsubscribe(topic)
        transport.subscribe(topic)

    transport.on_message = on_message
    transport.loop_start()
    transport.subscribe(topic)

    for n in range(1, 6):
        transport._deliver(_Message(topic, str(n).encode()))
    transport._pump()

    assert seen == [1, 2, 3, 4, 5]


def test_a_message_arriving_before_on_message_is_buffered_then_delivered_in_order(
        transport_factory, run_id):
    """One queue, not two: no inversion between buffered and fresh messages."""
    from apps.transport.kafka_bus import _Message

    transport = transport_factory('agent_buf')
    topic = f'{run_id}/agent_buf'
    transport.loop_start()
    transport.subscribe(topic)

    transport._deliver(_Message(topic, b'1'))
    transport._pump()                       # on_message is still None: must NOT drop
    assert len(transport._inbox) == 1

    seen = []
    transport.on_message = lambda c, u, m: seen.append(int(m.payload))
    assert seen == [1], 'the on_message setter must pump what was buffered'

    transport._deliver(_Message(topic, b'2'))
    transport._pump()
    assert seen == [1, 2]


def test_the_inbox_is_bounded_and_the_overflow_is_counted(transport_factory, run_id, caplog):
    """Silently unbounded growth on a wedged agent is worse than a loud drop."""
    from apps.transport.kafka_bus import _Message

    transport = transport_factory('agent_flood')
    transport._inbox_max = 4
    topic = f'{run_id}/agent_flood'

    with caplog.at_level(logging.ERROR):
        for n in range(10):
            transport._deliver(_Message(topic, str(n).encode()))

    assert len(transport._inbox) == 4
    assert transport._bus.inbox_overflow == 6
    # The OLDEST is dropped: the newest message is the one the barrier waits on.
    assert [int(m.payload) for m in transport._inbox] == [6, 7, 8, 9]
    assert 'inbox full' in caplog.text


def test_an_on_message_that_raises_does_not_kill_the_pump(transport_factory, run_id, caplog):
    from apps.transport.kafka_bus import _Message

    transport = transport_factory('agent_raiser')
    topic = f'{run_id}/agent_raiser'
    seen = []

    def on_message(client, userdata, message):
        seen.append(int(message.payload))
        if len(seen) == 1:
            raise ValueError('handler blew up')

    transport.on_message = on_message
    transport.loop_start()
    transport.subscribe(topic)
    with caplog.at_level(logging.ERROR):
        transport._deliver(_Message(topic, b'1'))
        transport._deliver(_Message(topic, b'2'))
        transport._pump()

    assert seen == [1, 2], 'a raising handler must not deafen the agent'
    assert 'on_message raised' in caplog.text


# ---------------------------------------------------------------------------
# The shared bus: one per process, refcounted per (lane, partition)
# ---------------------------------------------------------------------------

def test_a_second_handle_for_the_same_run_reuses_the_same_bus(transport_factory):
    """The design decision this whole package exists for: ~1060 agents, 64 buses."""
    first = transport_factory('agent_one')
    second = transport_factory('agent_two')
    assert first._bus is second._bus
    assert first._bus._refcount == 2


def test_a_different_run_gets_a_different_bus(transport_factory, run_id):
    from apps.transport.kafka_bus import shutdown_all_buses
    from apps.transport.kafka_transport import KafkaTransport
    from apps.transport.lane_admin import reap_run_lane_topics

    other_run = run_id + '_b'
    first = transport_factory('agent_one')
    second = KafkaTransport(SETTINGS, other_run, 'agent_one')
    try:
        assert first._bus is not second._bus
    finally:
        second.disconnect()
        shutdown_all_buses()
        try:
            reap_run_lane_topics(other_run, BOOTSTRAP)
        except Exception:
            pass


def test_a_lane_partition_is_released_only_when_the_LAST_handle_detaches(
        transport_factory, run_id):
    """Two co-resident agents sharing the broadcast partition is the normal case.

    If ``unsubscribe`` unassigned on the first detach instead of the last, one
    agent's nap would take the step broadcast away from all ~17 agents in the
    worker -- the exact "1 agent of 17 steps" failure, arriving from the
    subscription side instead of the consumer-group side.
    """
    from apps.transport.lane_topics import LANE_BCAST

    broadcast = f'{run_id}/scheduler_1/ORSimAgent'
    first = transport_factory('agent_a')
    second = transport_factory('agent_b')
    bus = first._bus

    first.loop_start()
    second.loop_start()
    first.subscribe(broadcast)
    second.subscribe(broadcast)
    assert bus._partition_refs[(LANE_BCAST, 0)] == 2

    first.unsubscribe(broadcast)             # the nap
    assert bus._partition_refs[(LANE_BCAST, 0)] == 1, \
        'the partition must stay assigned while another agent still wants it'
    assert bus._routes[broadcast] == [second]

    second.unsubscribe(broadcast)
    assert (LANE_BCAST, 0) not in bus._partition_refs
    assert broadcast not in bus._routes


def test_the_reply_lane_subscriber_assigns_every_partition(transport_factory, run_id):
    """Reply publishes are keyed by AGENT, so the scheduler cannot know where one landed.

    Keying by topic would pin ~2.6M replies onto one partition; keying by agent
    spreads them and preserves per-agent order.  The price is that the single
    reply-lane subscriber must take all of them -- which is free, because its
    fan-out is 1.
    """
    from apps.transport.lane_topics import LANE_REPLY

    scheduler_topic = f'{run_id}/scheduler_1/ORSimScheduler'
    scheduler = transport_factory('scheduler_1')
    scheduler.loop_start()
    scheduler.subscribe(scheduler_topic)

    expected = {(LANE_REPLY, p) for p in range(SETTINGS['KAFKA_REPLY_PARTITIONS'])}
    assert expected <= set(scheduler._bus._partition_refs)


def test_a_header_less_record_is_dropped_and_counted(transport_factory):
    """Never guessed from ``msg.topic()`` -- see ``lane_topics.orsim_topic_of``."""
    transport = transport_factory('agent_foreign')
    bus = transport._bus

    class _Raw:
        def headers(self):
            return []

        def topic(self):
            return 'orsim_app_something'

        def value(self):
            return b'{}'

    bus._on_record(_Raw())
    assert bus.foreign_messages == 1
    assert bus.messages_in == 0


def test_stats_exposes_the_four_counters(transport_factory):
    """A counter nobody prints is the same defect with extra steps."""
    stats = transport_factory('agent_stats')._bus.stats()
    for name in ('dropped_publishes', 'delivery_failures',
                 'foreign_messages', 'inbox_overflow'):
        assert name in stats


# ---------------------------------------------------------------------------
# Call-shape normalisation
# ---------------------------------------------------------------------------

def test_normalize_accepts_both_live_subscribe_shapes():
    from apps.transport.kafka_transport import _normalize

    assert _normalize('a/b') == ['a/b']
    assert _normalize([('a/b', 0), ('c/d', 1)]) == ['a/b', 'c/d']
    assert _normalize([]) == []
    assert _normalize(['a/b']) == ['a/b']
