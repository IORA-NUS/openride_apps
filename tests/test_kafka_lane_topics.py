"""Proof obligations for the topic mapping.  No broker, no confluent_kafka.

The mapping is the part of the Kafka transport a reviewer can check by reading,
so it is also the part that must be pinned by tests that cannot silently rot.
Four obligations:

1. ROUND TRIP IS BYTE-IDENTICAL for all three real ORSim topic grammars (and for
   strings outside every grammar).  Both inbound dispatchers compare by exact
   string equality with no ``else`` branch, so a rewritten topic is decoded and
   then silently discarded.
2. ``lane_for`` CLASSIFIES CORRECTLY and its default is ``app`` -- it must be
   TOTAL, because an unclassified topic has nowhere to go.
3. APP-LANE PARTITIONING IS STABLE ACROSS PROCESSES.  Pinned to hard-coded
   blake2b values so that a "simplification" to the builtin ``hash()`` fails
   loudly here instead of failing silently in production, where
   ``PYTHONHASHSEED`` randomisation would make publisher and subscriber disagree
   and records would land on partitions nobody is assigned to.
4. ``orsim_topic_of`` RETURNS ``None``, NEVER ``msg.topic()``, when the header is
   absent.

Plus the safety-critical one: the reap filter must not match any topic the
running system depends on.
"""

import pytest

from apps.transport import lane_admin
from apps.transport.lane_topics import (
    LANE_APP,
    LANE_BCAST,
    LANE_REPLY,
    ORSIM_TOPIC_HEADER,
    kafka_topic_for,
    lane_for,
    orsim_topic_of,
    partition_for,
    sanitize_run_id,
)

RUN_ID = 'run_20260911_054140'
BROADCAST = f'{RUN_ID}/scheduler_1/ORSimAgent'
SCHEDULER = f'{RUN_ID}/scheduler_1/ORSimScheduler'
AGENT_APP = f'{RUN_ID}/truck_0007'

#: The eleven topic names this broker actually carries, read from
#: localhost:9094 on 2026-09-11.  The reap filter is checked against these
#: verbatim, because a loose prefix match in a delete function is one keystroke
#: from removing run_status or trip_geo_stream while the dashboard consumes them.
LIVE_TOPICS = (
    '__consumer_offsets', 'bench_probe_rt', 'facility_stream',
    'kpi_breakdown_stream', 'kpi_stream', 'perf_stream', 'run_status',
    'service_control', 'service_events', 'service_status', 'trip_geo_stream',
)


class _FakeMessage:
    """Just enough of ``confluent_kafka.Message`` for the inverse function.

    ``topic()`` deliberately returns a LANE name -- the plausible wrong answer --
    so a test asserting ``orsim_topic_of(...) is None`` proves the function did
    not reach for it.
    """

    def __init__(self, headers, topic='orsim_app_run_20260911_054140'):
        self._headers = headers
        self._topic = topic

    def headers(self):
        return self._headers

    def topic(self):
        return self._topic


# ---------------------------------------------------------------------------
# 1. Round trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('orsim_topic', [
    BROADCAST,
    SCHEDULER,
    AGENT_APP,
    'anaytics/location_stream',          # the ride-hail legacy shape, typo and all
    'conformance_topic_a',               # no slash at all
    'run/with/many/slashes/and_ünïcode', # outside every grammar
    '',                                  # degenerate, still must survive
])
def test_round_trip_is_byte_identical(orsim_topic):
    """The header carries the topic verbatim; UTF-8 is a bijection on str.

    This is the whole injectivity argument, executed.  Note it holds for inputs
    OUTSIDE the ORSim grammar too, which a hand-written grammar-specific mangler
    would not give.
    """
    encoded = [(ORSIM_TOPIC_HEADER, orsim_topic.encode('utf-8'))]
    assert orsim_topic_of(_FakeMessage(encoded)) == orsim_topic


def test_round_trip_survives_a_str_valued_header():
    """Some client versions hand headers back as ``str`` rather than ``bytes``."""
    msg = _FakeMessage([(ORSIM_TOPIC_HEADER, BROADCAST)])
    assert orsim_topic_of(msg) == BROADCAST


def test_the_header_is_found_among_other_headers():
    msg = _FakeMessage([('x', b'1'), (ORSIM_TOPIC_HEADER, AGENT_APP.encode()), ('y', b'2')])
    assert orsim_topic_of(msg) == AGENT_APP


# ---------------------------------------------------------------------------
# 2. lane_for is correct and TOTAL
# ---------------------------------------------------------------------------

def test_lane_for_classifies_the_three_grammars():
    assert lane_for(BROADCAST, RUN_ID) == LANE_BCAST
    assert lane_for(SCHEDULER, RUN_ID) == LANE_REPLY
    assert lane_for(AGENT_APP, RUN_ID) == LANE_APP


@pytest.mark.parametrize('topic', [
    'conformance_topic_a',
    'anaytics/location_stream',
    '',
    'a/b/c/d',
    'scheduler_1/ORSimAgent',                       # only two segments
    'run_20260101_000000/scheduler_1/ORSimAgent',   # a DIFFERENT run's broadcast
    f'{RUN_ID}/scheduler_1/ORSimAgentX',
])
def test_lane_for_defaults_to_app(topic):
    """Anything unrecognised is app lane.  The function must never fail to answer."""
    assert lane_for(topic, RUN_ID) == LANE_APP


def test_an_agent_named_orsimagent_is_not_misrouted_onto_the_broadcast_lane():
    """``parts[0] == run_id`` is half the predicate, and this is why.

    Without it, a topic that merely ENDS in ``ORSimAgent`` -- another run's
    broadcast seen in a shared process, or an agent someone named ``ORSimAgent``
    -- would be published onto this run's broadcast lane and fanned out to every
    agent in the run.
    """
    foreign = 'run_19990101_000000/scheduler_1/ORSimAgent'
    assert lane_for(foreign, RUN_ID) == LANE_APP
    assert lane_for(foreign, 'run_19990101_000000') == LANE_BCAST


def test_kafka_topic_names_are_legal_and_lane_scoped():
    assert kafka_topic_for(BROADCAST, RUN_ID) == f'orsim_bcast_{RUN_ID}'
    assert kafka_topic_for(SCHEDULER, RUN_ID) == f'orsim_reply_{RUN_ID}'
    assert kafka_topic_for(AGENT_APP, RUN_ID) == f'orsim_app_{RUN_ID}'
    for topic in (BROADCAST, SCHEDULER, AGENT_APP):
        assert '/' not in kafka_topic_for(topic, RUN_ID)


def test_sanitize_run_id_never_produces_an_illegal_or_empty_name():
    assert sanitize_run_id(RUN_ID) == RUN_ID          # real run ids pass through
    assert sanitize_run_id('a/b c:d') == 'a_b_c_d'
    assert sanitize_run_id('') == 'unknown'
    assert sanitize_run_id('///') == '___'
    assert len(sanitize_run_id('x' * 500)) == 180


# ---------------------------------------------------------------------------
# 3. Partitioning is stable ACROSS PROCESSES
# ---------------------------------------------------------------------------

#: Hard-coded blake2b expectations.  DO NOT regenerate these from the
#: implementation if they start failing -- that defeats the whole point.  A
#: change here means the hash function changed, which means a publisher on the
#: old code and a subscriber on the new one now disagree about where a record
#: lives, and the record vanishes with no error.  That is a wire-format break and
#: needs a run boundary, not a test edit.
PINNED_PARTITIONS = {
    ('run_20260911_054140/truck_0007', 8): 6,
    ('run_20260911_054140/truck_0007', 16): 14,
    ('run_20260911_054140/truck_0007', 64): 30,
    ('run_20260911_054140/scheduler_1/ORSimAgent', 8): 4,
    ('run_20260911_054140/scheduler_1/ORSimAgent', 64): 52,
    ('truck_0007', 64): 62,
    ('facility_000', 64): 59,
}


@pytest.mark.parametrize('key_and_n,expected', sorted(PINNED_PARTITIONS.items()))
def test_partition_for_is_pinned_to_blake2b(key_and_n, expected):
    """A switch to the builtin ``hash()`` fails HERE, loudly, instead of in production.

    ``PYTHONHASHSEED`` randomisation makes ``hash()`` differ per process, and
    ``partition_for`` is evaluated independently by the publisher and the
    subscriber in different processes.  The failure mode is silent message loss
    plus a stalled barrier with a clean log -- the worst shape this project has.
    """
    routing_key, n_partitions = key_and_n
    assert partition_for(routing_key, n_partitions) == expected


def test_partition_for_is_stable_in_a_subprocess_with_a_different_hash_seed():
    """The property the pin exists to protect, demonstrated rather than asserted.

    Two child interpreters with different ``PYTHONHASHSEED`` values must agree.
    Builtin ``hash()`` would not.
    """
    import os
    import subprocess
    import sys

    code = (
        'from apps.transport.lane_topics import partition_for;'
        "print(partition_for('run_20260911_054140/truck_0007', 64))"
    )
    outputs = set()
    for seed in ('0', '1', '12345'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run([sys.executable, '-c', code], env=env,
                                capture_output=True, text=True, check=True)
        outputs.add(result.stdout.strip())
    assert outputs == {'30'}


def test_partition_for_degenerates_safely():
    assert partition_for('anything', 1) == 0
    assert partition_for('anything', 0) == 0


def test_partition_for_is_in_range():
    for n in (1, 2, 8, 64, 97):
        for i in range(200):
            assert 0 <= partition_for(f'truck_{i:04d}', n) < n


# ---------------------------------------------------------------------------
# 4. The inverse never guesses
# ---------------------------------------------------------------------------

def test_orsim_topic_of_returns_none_when_the_header_is_absent():
    """It must NOT fall back to ``msg.topic()``.

    Doing so would hand ``'orsim_app_run_...'`` to
    ``ORSimAgent.on_receive_message``, whose ``message_handlers.get()`` has no
    ``else`` branch: the record would be decoded, the payload cache updated, and
    the message silently discarded.  The bus counts these as
    ``foreign_messages`` and drops them.
    """
    assert orsim_topic_of(_FakeMessage([])) is None
    assert orsim_topic_of(_FakeMessage(None)) is None
    assert orsim_topic_of(_FakeMessage([('other', b'x')])) is None


# ---------------------------------------------------------------------------
# The reap filter -- the safety-critical part
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name', LIVE_TOPICS)
def test_the_reap_filter_matches_no_topic_the_running_system_uses(name):
    assert lane_admin.is_reapable_lane_topic(name) is False


@pytest.mark.parametrize('name', [
    'orsim_bcast_run_20260911_054140',
    'orsim_reply_run_20260911_054140',
    'orsim_app_run_20260911_054140',
])
def test_the_reap_filter_matches_lane_topics(name):
    assert lane_admin.is_reapable_lane_topic(name) is True


@pytest.mark.parametrize('name', [
    'orsim_',
    'orsim_app_',
    'orsim_app_notarun',
    'orsim_other_run_20260911_054140',
    'xorsim_app_run_20260911_054140',
    'orsim_app_run_20260911_054140/extra',
    'ORSIM_APP_RUN_20260911_054140',
])
def test_the_reap_filter_rejects_near_misses(name):
    """A name that merely starts with ``orsim_`` does not qualify."""
    assert lane_admin.is_reapable_lane_topic(name) is False


def test_delete_topics_refuses_anything_the_filter_rejects(monkeypatch, caplog):
    """``_delete_topics`` re-applies the filter rather than trusting its callers.

    It is the only function in the package that can destroy data the dashboard
    reads, so it does not take a caller's word for it.
    """
    called = []
    monkeypatch.setattr(lane_admin, '_admin',
                        lambda bootstrap: called.append(bootstrap) or object())
    with caplog.at_level('ERROR'):
        assert lane_admin._delete_topics(['run_status', 'trip_geo_stream'], None) == []
    assert called == []          # no AdminClient was even constructed
    assert 'refusing to delete' in caplog.text


def test_lane_topic_names_are_pure():
    names = lane_admin.lane_topic_names(RUN_ID)
    assert names == {
        'bcast': f'orsim_bcast_{RUN_ID}',
        'reply': f'orsim_reply_{RUN_ID}',
        'app': f'orsim_app_{RUN_ID}',
    }


def test_the_ops_sweep_skips_topics_it_cannot_date():
    """Undatable is not the same as stale.  Kafka exposes no topic creation time."""
    assert lane_admin._run_started_at('orsim_app_run_not_a_timestamp') is None
    assert lane_admin._run_started_at('orsim_app_run_20260911_054140') is not None
    assert lane_admin._run_started_at('orsim_app_run_20261332_999999') is None
