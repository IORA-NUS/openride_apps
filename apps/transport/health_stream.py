"""The cross-process channel that carries transport counters into the run outcome.

WHY THIS EXISTS
---------------
``kafka_bus`` maintains an exact set of loss counters and prints them under the
greppable marker ``ORSIM TRANSPORT DEGRADED`` -- into
``journalctl --user -u openride-celery``, in ~64 separate worker processes.  The
run's own outcome (``run_config.status``, the perf summary, ``summary.json``) is
written by a DIFFERENT process, which hosts no buses and therefore sees none of
those counters.  So before this module a run in which every worker dropped
thousands of agent replies finished with ``status: completed, ok: true,
error: null`` and an unchanged ``run_config``: the exact shape CLAUDE.md records
twice already (the 3,247-lost-``facility_stream``-events run in section 8, the
8,032-vs-14,300-orders run in section 6.23), and the shape phase 2 exists to
end.  A counter nobody can reach from the run's outcome is the same defect as no
counter at all.

The channel is Kafka, because the sim is already a Kafka producer and the
workers already are too -- no new dependency, no new service, no new Eve
resource (which would need an api image rebuild to register, see CLAUDE.md 8).

TWO DESIGN POINTS THAT ARE LOAD-BEARING
---------------------------------------
1. **Snapshots are PERIODIC, not only at teardown.**  A bus lingers
   ``BUS_LINGER_S`` (30 s) after its last handle leaves before it reaps itself,
   and that is when the teardown snapshot would be published -- i.e. AFTER the
   sim process has finalised and written its outcome.  A snapshot every
   ``HEALTH_PUBLISH_INTERVAL_S`` means the finalize path always has a reading
   that is at most that stale, and the teardown snapshot is a bonus rather than
   the only hope.
2. **A SEPARATE producer, whose own failures never touch the bus counters.**
   Publishing health on the bus's own producer would charge a failed health
   publish to ``delivery_failures`` -- i.e. a broken diagnostic would
   manufacture the very verdict it reports.  This producer is also allowed to
   auto-create its topic: ``transport_health_stream`` is a single-partition
   append log whose partitioning is not load-bearing, unlike a lane topic, where
   an auto-created ``num.partitions=1`` replacement is the catastrophe
   ``lane_admin`` is built to prevent.  (``apps/config.py``'s ``topic_bootstrap``
   creates it properly, with retention, in the launching process.)

READ-BACK IS ASSIGN-ONLY, for the same reason the bus is: a ``subscribe()`` here
would mint a consumer group per read, and consumer-group sprawl is a cost this
project has already paid once (CLAUDE.md 6.11).
"""

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: The broker topic.  Resolved through ``apps.config.kafka_config['topics']`` so
#: the name lives in exactly one place and the bootstrap path creates it.
HEALTH_TOPIC_KEY = 'transport_health'
HEALTH_TOPIC_FALLBACK = 'transport_health_stream'

#: How long :func:`collect_transport_health` is willing to spend reading the
#: topic.  It runs on the sim's finalize path, where every second is run wall
#: time, and a missing answer degrades to "unknown" rather than to a wrong one.
DEFAULT_COLLECT_TIMEOUT_S = 5.0

_producers: Dict[str, object] = {}

#: UNPATCHED, for the same reason ``_KafkaBus._lock`` is: this lock is taken by
#: the bus's NATIVE poll thread (the periodic snapshot) as well as by greenlets
#: (the teardown snapshot, and ``flush_health`` at exit).  A patched
#: ``threading.Lock`` acquired and released by a non-hub OS thread is a hazard,
#: not a safety net -- see the EVENTLET section of ``kafka_bus``.  The critical
#: section is a dict lookup plus at most one non-blocking ``Producer``
#: construction, so the hub is never parked on it for a meaningful time.
try:  # pragma: no cover - eventlet is always present in this project's venv
    import eventlet.patcher

    _producer_lock = eventlet.patcher.original('threading').Lock()
except Exception:  # pragma: no cover
    _producer_lock = threading.Lock()

_publish_errors = 0


def health_topic() -> str:
    """The broker topic name for transport health snapshots."""
    try:
        from apps.config import kafka_config

        return kafka_config.get('topics', {}).get(HEALTH_TOPIC_KEY,
                                                  HEALTH_TOPIC_FALLBACK)
    except Exception:  # pragma: no cover - config is always importable
        return HEALTH_TOPIC_FALLBACK


def _on_delivery(err, msg) -> None:
    """Health-publish failures are logged, rarely, and charged to NOTHING else."""
    global _publish_errors
    if err is None:
        return
    _publish_errors += 1
    if _publish_errors <= 3 or _publish_errors % 1000 == 0:
        logger.warning('transport health snapshot could not be delivered (%d): %s',
                       _publish_errors, err)


def _producer(bootstrap: str):
    """One lazily-built producer per bootstrap string.  Thread-safe."""
    with _producer_lock:
        producer = _producers.get(bootstrap)
        if producer is None:
            from confluent_kafka import Producer

            producer = Producer({
                'bootstrap.servers': bootstrap,
                # Diagnostics: latency does not matter, but a snapshot that sits
                # in a local queue past process exit is useless, so the timeout
                # is short and the linger is zero.
                'linger.ms': 0,
                'acks': 1,
                'message.timeout.ms': 5000,
                'client.id': f'orsim-health-{os.getpid()}',
            })
            _producers[bootstrap] = producer
        return producer


def publish_bus_health(bootstrap: str, run_id: str, stats: dict,
                       reason: str = 'periodic') -> None:
    """Publish ONE bus's counters.  Fire and forget; never raises.

    Called from the bus's poll thread (periodic) and from its teardown, so it
    must not block and must not touch an eventlet primitive.  ``produce`` is a
    local enqueue and ``poll(0)`` is non-blocking, which satisfies both.
    """
    if not bootstrap or not run_id:
        return
    record = {
        'run_id': run_id,
        'pid': os.getpid(),
        'reason': reason,
        'published_at': time.time(),
        'stats': stats,
    }
    try:
        producer = _producer(bootstrap)
        producer.produce(health_topic(), value=json.dumps(record).encode('utf-8'),
                         key=str(run_id).encode('utf-8'), on_delivery=_on_delivery)
        producer.poll(0)
    except Exception as exc:  # noqa: BLE001 - a diagnostic may never raise
        logger.debug('transport health publish skipped: %s', exc)


def flush_health(timeout: float = 2.0) -> int:
    """Flush every health producer in this process.  Returns records still queued."""
    with _producer_lock:
        producers = list(_producers.values())
    remaining = 0
    for producer in producers:
        try:
            remaining += producer.flush(timeout)
        except Exception as exc:  # noqa: BLE001
            logger.debug('transport health flush failed: %s', exc)
    return remaining


# --------------------------------------------------------------------------
# Read-back, for the sim's finalize path
# --------------------------------------------------------------------------

def collect_transport_health(run_id: str, bootstrap: Optional[str] = None,
                             timeout: float = DEFAULT_COLLECT_TIMEOUT_S) -> dict:
    """Aggregate every worker's latest snapshot for ``run_id``.

    Returns a dict shaped like :func:`apps.transport.kafka_bus.transport_health`
    plus ``processes`` and ``available``.  ``available`` False means the topic
    could not be read at all -- which must be reported as UNKNOWN rather than as
    clean, because "no snapshots" and "no losses" are the same bytes.

    Reads the whole (short-retention, single-partition) topic from the
    beginning and keeps the LATEST snapshot per publishing process, which is
    what makes a periodic snapshot and a teardown snapshot from the same process
    count once rather than twice.
    """
    from apps.transport.kafka_bus import (DEGRADED_COUNTERS, DIAGNOSTIC_COUNTERS,
                                          degraded_from, routing_blackhole)

    empty = {
        'run_id': run_id,
        'available': False,
        'degraded': None,
        'processes': 0,
        'snapshots': 0,
        'totals': {},
        'per_process': [],
    }
    if bootstrap is None:
        try:
            from apps.config import kafka_config

            bootstrap = kafka_config['bootstrap_servers']
        except Exception:  # pragma: no cover
            return empty
    if not bootstrap:
        return empty

    records = _read_all(bootstrap, timeout)
    if records is None:
        return empty

    latest: Dict[str, dict] = {}
    seen_for_run = 0
    for record in records:
        if record.get('run_id') != run_id:
            continue
        seen_for_run += 1
        stats = record.get('stats') or {}
        # One process can host several buses in principle (different runs), but
        # filtering by run_id above leaves exactly one bus per process -- so the
        # publishing process IS the identity.  ``stats['pid']`` is preferred
        # over the envelope's so that a relayed snapshot is still attributed to
        # the process that produced the counters; in a real worker they are the
        # same number.
        pid = stats.get('pid', record.get('pid'))
        key = f'{pid}'
        previous = latest.get(key)
        if previous is None or record.get('published_at', 0) >= previous.get('published_at', 0):
            latest[key] = {'published_at': record.get('published_at', 0),
                           'reason': record.get('reason'),
                           'pid': pid,
                           'stats': stats}

    names = list(DEGRADED_COUNTERS) + list(DIAGNOSTIC_COUNTERS)
    totals = {name: 0 for name in names}
    degraded_processes = []
    for key, row in latest.items():
        stats = row['stats']
        for name in names:
            totals[name] += int(stats.get(name, 0) or 0)
        if degraded_from(stats):
            degraded_processes.append(row['pid'])

    return {
        'run_id': run_id,
        'available': True,
        # A process-level verdict, not a sum-level one: one worker that lost
        # traffic makes the run unquotable even if the totals look small, and
        # ``routing_blackhole`` on a SUM can be masked by a healthy peer.
        'degraded': bool(degraded_processes) or degraded_from(totals),
        'degraded_pids': sorted(p for p in degraded_processes if p is not None),
        'routing_blackhole': any(routing_blackhole(row['stats'])
                                 for row in latest.values()),
        'processes': len(latest),
        'snapshots': seen_for_run,
        'totals': totals,
        'per_process': [
            {'pid': row['pid'], 'reason': row['reason'], 'stats': row['stats']}
            for row in sorted(latest.values(), key=lambda r: str(r['pid']))
        ],
    }


def _read_all(bootstrap: str, timeout: float) -> Optional[List[dict]]:
    """Every record on the health topic, or None if it cannot be read.

    Assign-only (no ``subscribe``, no group membership, no committed offsets --
    see the module docstring), from ``OFFSET_BEGINNING`` on every partition, to
    EOF or the timeout.
    """
    try:
        from confluent_kafka import OFFSET_BEGINNING, Consumer, TopicPartition
    except Exception:  # pragma: no cover - confluent_kafka is in requirements
        return None

    topic = health_topic()
    consumer = None
    try:
        consumer = Consumer({
            'bootstrap.servers': bootstrap,
            'group.id': 'orsim-transport-health-reader',
            'enable.auto.commit': False,
            'auto.offset.reset': 'earliest',
            'enable.partition.eof': True,
            'allow.auto.create.topics': False,
            'client.id': f'orsim-health-reader-{os.getpid()}',
        })
        metadata = consumer.list_topics(topic, timeout=min(5.0, timeout))
        info = metadata.topics.get(topic)
        if info is None or info.error is not None or not info.partitions:
            return None
        parts = [TopicPartition(topic, p, OFFSET_BEGINNING) for p in info.partitions]
        consumer.assign(parts)

        out: List[dict] = []
        at_eof = set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(at_eof) < len(parts):
            msg = consumer.poll(0.2)
            if msg is None:
                continue
            err = msg.error()
            if err is not None:
                # KafkaError._PARTITION_EOF is how "caught up" is reported when
                # enable.partition.eof is on; anything else is skipped, because
                # a half-read diagnostic beats no diagnostic.
                if getattr(err, 'code', lambda: None)() == _eof_code():
                    at_eof.add(msg.partition())
                continue
            try:
                out.append(json.loads((msg.value() or b'').decode('utf-8')))
            except Exception:  # noqa: BLE001 - one bad record is not the answer
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug('transport health read failed: %s', exc)
        return None
    finally:
        if consumer is not None:
            try:
                consumer.close()
            except Exception:  # noqa: BLE001
                pass


def _eof_code():
    from confluent_kafka import KafkaError

    return KafkaError._PARTITION_EOF
