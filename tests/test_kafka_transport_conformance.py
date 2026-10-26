"""``KafkaTransport`` against the phase-1 executable contract, on a REAL broker.

The ABC says it plainly and this file repeats it: *validate by RUNNING, not by
reading*.  Every semantic that the step barrier rests on is expressed as a check
in ``orsim.messenger.conformance``, and the transport's verdict is that suite's
verdict, not a prose argument.

WHAT THIS RUNS
--------------
``severities=(REQUIRED, STRICT)`` -- both, as the suite instructs for anything
that is not paho.  ``PahoTransport`` is grandfathered out of the one STRICT check
(``on_message_unset_buffers``) because its subscribe is local and the window is
microseconds; a transport whose subscribe costs a round trip does not get that
exemption, so ``KafkaTransport`` buffers.

TWO PARAMETRISATIONS, AND WHY THE SECOND ONE EXISTS
---------------------------------------------------
The suite's real-grammar topics carry a HARD-CODED run id
(``run_20260911_120000/scheduler_1/ORSimAgent``).  ``lane_for`` requires
``parts[0] == run_id``, so under a freshly minted run id those strings classify
as APP lane -- correct, total, and byte-identical on the round trip, but it means
the broadcast and reply lanes would never be exercised at all.

* ``fresh`` -- a new run id (and new lane topics) per check.  Maximum isolation;
  the shape the task specified.
* ``grammar`` -- the run id PINNED to the suite's own, so
  ``.../ORSimAgent`` really lands on the 1-partition broadcast lane and
  ``.../ORSimScheduler`` really lands on the 8-partition reply lane whose
  subscriber must assign ALL partitions because publishes are keyed by agent.
  That is the configuration a real run uses, and without it the two most
  interesting lanes are dead code in the test.

``pump()`` ONLY SLEEPS.  Per the suite's HARNESS RULES it may turn the crank on
delivery the transport already implements, and must never route, fan out, filter
or map anything.  A routing harness makes every check vacuous, which is why the
suite carries an anti-cheat (``check_broadcast_fanout``'s bystander).

WHAT A GREEN RESULT DOES NOT MEAN -- quote this caveat with the result
----------------------------------------------------------------------
The suite says so itself.  It cannot see topic legality against a real broker
(covered here, because these transports really do publish ``'/'``-bearing
strings to localhost:9094), it cannot see eventlet fitness (covered by
``tests/test_kafka_transport_eventlet_cooperation.py``, a separate child
process), and it cannot see load or throughput (NOT covered anywhere in phase 2
-- the app-lane fan-out amplification number is still unmeasured).
"""

import itertools
import os
import time

import pytest

from tests.kafka_run_ids import dated_run_id

BOOTSTRAP = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9094')

#: Small on purpose: the mapping is exercised, not the scale.  Production
#: defaults are 64/8 (``apps/config.py``); using them here would create 64
#: partitions per check.
TEST_SETTINGS = {
    'KAFKA_BOOTSTRAP_SERVERS': BOOTSTRAP,
    'KAFKA_APP_PARTITIONS': 8,
    'KAFKA_REPLY_PARTITIONS': 4,
    'KAFKA_LINGER_MS': 5,
}

#: The run id baked into ``conformance.BROADCAST_TOPIC`` / ``SCHEDULER_TOPIC`` /
#: ``AGENT_TOPIC``.  Pinning to it is what puts those topics on their real lanes.
GRAMMAR_RUN_ID = 'run_20260911_120000'


def _broker_is_up(timeout=5.0):
    try:
        from confluent_kafka.admin import AdminClient
        metadata = AdminClient({'bootstrap.servers': BOOTSTRAP}).list_topics(timeout=timeout)
        return bool(metadata and metadata.brokers)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _broker_is_up(),
    reason=(f'no Kafka broker at {BOOTSTRAP}; the conformance suite for '
            'KafkaTransport is a real-broker test by design -- an in-memory '
            'double would prove nothing about topic legality or delivery'),
)


@pytest.fixture(params=['fresh', 'grammar'])
def harness(request):
    """A ``TransportHarness`` over real ``KafkaTransport`` instances.

    Teardown disconnects every transport the suite left behind, shuts the
    process's buses down, and REAPS every lane topic this test minted -- lane
    topics accumulate at 3 per run id and a test that leaks them is the same
    defect as a killed run that leaks them.
    """
    from orsim.messenger.conformance import TransportHarness

    from apps.transport.kafka_bus import shutdown_all_buses
    from apps.transport.kafka_transport import KafkaTransport
    from apps.transport.lane_admin import ensure_run_lane_topics, reap_run_lane_topics

    mode = request.param
    counter = itertools.count()
    minted = []
    state = {'run_id': None}

    def _use(run_id):
        state['run_id'] = run_id
        if run_id not in minted:
            minted.append(run_id)
            ensure_run_lane_topics(
                run_id, BOOTSTRAP,
                reply_partitions=TEST_SETTINGS['KAFKA_REPLY_PARTITIONS'],
                app_partitions=TEST_SETTINGS['KAFKA_APP_PARTITIONS'],
            )

    def on_check_start(check):
        # `fresh` hands each check its own lane topics so a transport that
        # replayed history could not leak one check's traffic into the next and
        # make the report blame the wrong semantic.  `grammar` deliberately does
        # NOT, because the run id is what selects the lane -- and forward-only
        # delivery is exactly what makes sharing safe.
        if mode == 'fresh':
            _use(dated_run_id(f'conf{next(counter):03d}'))
        else:
            _use(GRAMMAR_RUN_ID)

    def new_transport():
        assert state['run_id'] is not None, 'on_check_start must run first'
        return KafkaTransport(
            TEST_SETTINGS, state['run_id'],
            f'conformance_agent_{next(counter):04d}')

    # The suite may build a transport before any check begins (it does not
    # today), and `new_transport` asserts otherwise; prime it.
    on_check_start(None)

    yield TransportHarness(
        new_transport=new_transport,
        # HARNESS RULES: this only turns the crank.  It performs no routing, no
        # fan-out, no filtering, no encoding and no topic mapping.
        pump=lambda deadline: time.sleep(min(deadline, 0.4)),
        loop_is_running=lambda transport: transport._loop_running,
        on_check_start=on_check_start,
        name=f'KafkaTransport[{mode}]',
    )

    shutdown_all_buses()
    for run_id in minted:
        try:
            reap_run_lane_topics(run_id, BOOTSTRAP)
        except Exception:       # teardown must never mask a verdict
            pass


def test_kafka_transport_is_conformant(harness, capsys):
    """Run every REQUIRED and STRICT check and fail with the suite's own report."""
    from orsim.messenger.conformance import REQUIRED, STRICT, run_conformance_suite

    report = run_conformance_suite(harness, severities=(REQUIRED, STRICT))
    with capsys.disabled():
        print()
        print(report.summary() if hasattr(report, 'summary') else report)
    report.raise_if_failed()
