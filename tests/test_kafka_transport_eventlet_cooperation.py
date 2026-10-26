"""The acceptance bar the conformance suite explicitly CANNOT assert.

The suite runs unpatched and knows nothing about greenlets.  But agents run in
celery workers started with ``--pool eventlet`` (``scripts/run-celery.sh``), and
``confluent_kafka`` is a C extension: a receive loop that blocks inside C freezes
the eventlet hub and therefore EVERY agent sharing that process (~17 at the
current 64x20 split).  That is the hardest constraint in this phase and it is
invisible to every other test in the repo, so it gets one of its own.

WHAT IS MEASURED
----------------
A co-resident greenlet in a ``while: ticks += 1; eventlet.sleep(0)`` loop --
standing in for another agent that wants CPU -- is timed twice under identical
conditions:

    A. with no transport at all           (the ceiling)
    B. with a live KafkaTransport receiving a steady publish stream

and B must reach at least ``MIN_RATIO`` of A.  Measured on this box today
(eventlet 0.34.2), the shapes that were compared before the design was chosen:

    no receive loop at all (ceiling)     281,693 slices/s
    native thread + pipe + trampoline    196,768-243,188/s  (70-86%)
    greenlet loop, poll(0) + sleep(2ms)  unreliable -- starved its own producer
    greenlet loop, poll(1.0)             0 slices/s in 6.9 s -- TOTAL starvation

``poll(1.0)`` on the hub is the shape a first draft naturally writes, and it
scores ZERO: the co-resident greenlet was never scheduled at all.  That is the
regression this test exists to catch, and a 50% bar catches it with room to
spare while tolerating this box's ordinary noise.

WHY A CHILD PROCESS
-------------------
``eventlet.monkey_patch()`` is irreversible and process-global.  Calling it
inside the pytest session would patch ``socket``, ``time`` and ``threading`` for
every test that runs afterwards, which is exactly the kind of action-at-a-
distance that makes a suite untrustworthy.  ``subprocess`` keeps it contained,
and it has a second benefit: the child exercises the MONKEY-PATCHED code path
(native poll thread -> self-pipe -> trampoline -> per-handle greenlet) while the
conformance suite exercises the unpatched one (poll thread calls ``on_message``
directly).  Both paths ship, so both are tested.

THE TRAP THE CHILD PROGRAM GUARDS AGAINST, restated because it is invisible when
violated: inside the native poll thread, ``time``, ``socket`` and ``threading``
are still the PATCHED modules.  A patched ``time.sleep`` called from a non-hub OS
thread misbehaves; it starved a producer thread and invalidated one measurement
during the session that produced the numbers above.  ``kafka_bus`` reaches for
``eventlet.patcher.original('time').sleep`` for exactly this reason.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.kafka_run_ids import dated_run_id

BOOTSTRAP = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9094')
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The bar.  The design measures 70-86%; the disqualified shape measures 0%.
MIN_RATIO = 0.50

#: Seconds per arm.  Long enough that scheduler noise averages out, short enough
#: that the test stays runnable in a normal suite.
SAMPLE_SECONDS = 6.0


def _broker_is_up(timeout=5.0):
    try:
        from confluent_kafka.admin import AdminClient
        metadata = AdminClient({'bootstrap.servers': BOOTSTRAP}).list_topics(timeout=timeout)
        return bool(metadata and metadata.brokers)
    except Exception:
        return False


_CHILD_PROGRAM = r'''
import json, os, sys, time

import eventlet
eventlet.monkey_patch()

BOOTSTRAP = sys.argv[1]
SECONDS = float(sys.argv[2])
RUN_ID = sys.argv[3]

SETTINGS = {
    'KAFKA_BOOTSTRAP_SERVERS': BOOTSTRAP,
    'KAFKA_APP_PARTITIONS': 4,
    'KAFKA_REPLY_PARTITIONS': 2,
    'KAFKA_LINGER_MS': 5,
}


def spin(seconds):
    """The co-resident agent stand-in: yield to the hub as fast as possible."""
    ticks = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        ticks += 1
        eventlet.sleep(0)
    return ticks


def baseline():
    return spin(SECONDS) / SECONDS


def with_transport():
    from apps.transport.kafka_transport import KafkaTransport
    from apps.transport.kafka_bus import get_bus, shutdown_all_buses

    topic = RUN_ID + '/truck_0001'
    latencies = []
    received = []

    receiver = KafkaTransport(SETTINGS, RUN_ID, 'truck_0001')

    def on_message(client, userdata, message):
        sent = json.loads(message.payload.decode('utf-8'))['t']
        latencies.append(time.time() - sent)
        received.append(1)

    receiver.on_message = on_message
    receiver.loop_start()
    receiver.subscribe(topic)

    sender = KafkaTransport(SETTINGS, RUN_ID, 'scheduler_1')
    sender.loop_start()

    stop = []

    def publisher():
        # ~10 msg/s, the cadence the design was measured at.
        while not stop:
            sender.publish(topic, json.dumps({'t': time.time()}))
            eventlet.sleep(0.1)

    pub = eventlet.spawn(publisher)
    eventlet.sleep(0.5)          # let the first records flow before timing

    rate = spin(SECONDS) / SECONDS

    stop.append(True)
    pub.kill()
    stats = dict(get_bus(BOOTSTRAP, RUN_ID).stats())
    receiver.disconnect()
    sender.disconnect()
    shutdown_all_buses()
    return rate, latencies, len(received), stats


base = baseline()
rate, latencies, count, stats = with_transport()
latencies.sort()


def pct(p):
    if not latencies:
        return None
    return latencies[min(len(latencies) - 1, int(len(latencies) * p))]


print('RESULT ' + json.dumps({
    'baseline_slices_per_s': base,
    'transport_slices_per_s': rate,
    'ratio': (rate / base) if base else 0.0,
    'received': count,
    'latency_p50_ms': (pct(0.50) or 0) * 1000.0,
    'latency_p95_ms': (pct(0.95) or 0) * 1000.0,
    'latency_max_ms': (max(latencies) * 1000.0) if latencies else 0.0,
    'bus': stats,
}))
'''


@pytest.mark.skipif(not _broker_is_up(),
                    reason=f'no Kafka broker at {BOOTSTRAP}')
def test_a_co_resident_greenlet_keeps_running_under_a_live_transport(capsys):
    """>= 50% of the no-transport scheduling rate, measured, not asserted.

    Also prints the delivery latency distribution.  The design trades ONE extra
    hop (native poll -> deque -> hub wake -> per-handle greenlet) for hub
    cooperation, and that hop is a real added latency against paho's
    sub-millisecond in-process path.  It is reported here rather than assumed
    away: on a barrier whose settle is 2 s and whose herd already runs
    470-1838 ms it is not material, but it IS real.
    """
    from apps.transport.lane_admin import reap_run_lane_topics

    run_id = dated_run_id('evtcoop')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        p for p in (os.getenv('PYTHONPATH'), '/home/user/orsim', REPO_ROOT) if p))
    try:
        completed = subprocess.run(
            [sys.executable, '-c', _CHILD_PROGRAM, BOOTSTRAP, str(SAMPLE_SECONDS), run_id],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            timeout=SAMPLE_SECONDS * 4 + 90,
        )
    finally:
        try:
            reap_run_lane_topics(run_id, BOOTSTRAP)
        except Exception:
            pass

    assert completed.returncode == 0, (
        f'the eventlet child failed:\nSTDOUT:\n{completed.stdout}\n'
        f'STDERR:\n{completed.stderr}')

    line = next((l for l in completed.stdout.splitlines() if l.startswith('RESULT ')), None)
    assert line, f'child produced no RESULT line:\n{completed.stdout}\n{completed.stderr}'
    result = json.loads(line[len('RESULT '):])

    with capsys.disabled():
        print()
        print('eventlet cooperation:')
        print(f'  baseline           {result["baseline_slices_per_s"]:>12,.0f} slices/s')
        print(f'  with transport     {result["transport_slices_per_s"]:>12,.0f} slices/s'
              f'   ({result["ratio"]:.0%} of ceiling)')
        print(f'  messages received  {result["received"]}')
        print(f'  delivery latency   p50 {result["latency_p50_ms"]:.2f} ms  '
              f'p95 {result["latency_p95_ms"]:.2f} ms  '
              f'max {result["latency_max_ms"]:.2f} ms')

    assert result['received'] > 0, (
        'the receiving transport got nothing, so the cooperation number is '
        'meaningless -- a transport that receives nothing cooperates perfectly')
    assert result['bus']['patched'] is True, (
        'the child did not exercise the monkey-patched path; the whole point of '
        'running it in a subprocess was to reach that code')
    assert result['ratio'] >= MIN_RATIO, (
        f'a co-resident greenlet ran at {result["ratio"]:.0%} of its no-transport '
        f'rate, below the {MIN_RATIO:.0%} bar. The receive loop is starving the '
        'eventlet hub -- which starves every agent in the worker. The classic '
        'cause is calling Consumer.poll() from a greenlet instead of from an '
        'unpatched OS thread (that shape measured 0%).')
