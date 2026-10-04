"""One Kafka consumer per worker PROCESS, demuxed in memory to ~17 agent handles.

GREPPABLE INVARIANT: ``consumer.subscribe()`` IS NEVER CALLED IN THIS PACKAGE.
Partitions enter the assignment set only through ``incremental_assign`` with an
explicit numeric offset, and leave only through ``incremental_unassign``.  If you
find a ``subscribe(`` on a ``confluent_kafka.Consumer`` anywhere under
``apps/transport/``, it is a bug -- see BROADCAST below.

WHAT THIS OBJECT IS
-------------------
``~1060`` agents per run become ``64`` buses, not ``1060`` consumers.  A bus owns
one ``Consumer`` (assign-only), one native OS poll thread, one self-pipe, one
dispatcher greenlet, one ``Producer``, the lane/partition refcount table, the
``orsim_topic -> [handle]`` routing dict, and the counters.  It knows nothing
about agents: a "handle" is just something with ``_deliver`` and ``_pump``.
:class:`apps.transport.kafka_transport.KafkaTransport` is the handle.

BROADCAST -- why assign-only, and why a SHARED CONSTANT ``group.id``
--------------------------------------------------------------------
The step barrier rests on broadcast: ``ORSimScheduler.step`` publishes ONE
message and awaits N replies, so every subscriber of a topic must receive every
message.  A shared ``group.id`` with ``subscribe()`` delivers each record to
exactly ONE group member -- 1 agent of ~1060 steps, ``confirm_responses`` never
resolves, and the run crawls at one ``STEP_TIMEOUT`` per step without crashing.

The ABC offers two ways out: a unique ``group.id`` per agent, or manual
assignment.  The unique-``group.id`` route would mint ~1063 consumer groups per
run -- the consumer-group sprawl this project has already paid for once
(CLAUDE.md 6.11).  Manual assignment mints ZERO: with no ``subscribe()`` there is
no JoinGroup, no SyncGroup, no coordinator, no ``__consumer_offsets`` write and
no rebalance protocol at all.  The ``group.id`` below is a single constant that
exists only because librdkafka requires the config key; it participates in
nothing.  Verified against localhost:9094: two consumers sharing one
``group.id``, each manually assigned all partitions of a topic, EACH received all
three published records; a third assigned only partition 0 received none of the
records that landed on partition 1.  Assignment, not subscription, decides
delivery -- which is also why the partition arithmetic in ``lane_topics`` is
load-bearing rather than an optimisation.

Fan-out then happens at two levels:

1. ACROSS PROCESSES -- every agent-hosting process assigns partition 0 of
   ``orsim_bcast_<run>`` (1 partition, so "all partitions" is trivially the same
   set everywhere).  The scheduler's single ``step()`` publish is fetched
   independently by all 64 buses.  There is no group to divide it.
2. WITHIN A PROCESS -- ``_route`` fans a recovered message out to EVERY handle
   whose ``_topics`` contains the recovered string.  A LIST, not a single
   lookup.  Getting this wrong fails exactly like the group-consumer bug: 1
   agent of 17 steps and the barrier burns ``STEP_TIMEOUT``.

EVENTLET -- a real OS thread polls; a greenlet routes; PER-HANDLE greenlets deliver
-----------------------------------------------------------------------------------
Agents run in celery workers started with ``--pool eventlet`` and
``confluent_kafka`` is a C extension.  A receive loop that blocks inside C
freezes the eventlet hub and therefore every agent sharing that process.  Three
shapes were MEASURED on this box (eventlet 0.34.2, monkey-patched, a co-resident
``while: ticks += 1; eventlet.sleep(0)`` greenlet, 10 msg/s inbound, 6 s each):

    no receive loop at all (ceiling)     281,693 slices/s
    native thread + pipe + trampoline    196,768-243,188/s   (70-86% of ceiling)
    greenlet loop, poll(0) + sleep(2ms)  unreliable; its own producer starved
    greenlet loop, poll(1.0)             0 slices/s in 6.9 s -- TOTAL starvation

``poll(1.0)`` on the hub is the shape a first draft naturally writes and it is
disqualified outright: the co-resident greenlet was scheduled ZERO times.

The mechanism, precisely, because each step of it is easy to get subtly wrong:

1. ``nthr = eventlet.patcher.original('threading')``.  By the time a transport is
   constructed the monkeypatch is long applied, so ``threading.Thread`` is a
   greenlet; reaching the unpatched module explicitly is the ONLY way to get a
   real OS thread.  Blocking inside librdkafka's C code on that thread is fine:
   ``confluent_kafka`` releases the GIL around ``poll``, and the thread is not
   eventlet's to yield.
2. **Inside that native thread, ``time``, ``socket`` and ``threading`` are still
   the PATCHED modules.**  A patched ``time.sleep`` called from a non-hub OS
   thread misbehaves; it starved a producer thread and invalidated one
   measurement in the session that produced these numbers.  RULE: inside the
   poll thread use :data:`_native_sleep` and touch NO eventlet primitive
   whatsoever.  This is invisible when violated.
3. Handoff is ``deque.append`` (poll thread) / ``deque.popleft`` (dispatcher).
   Both are single GIL-atomic bytecodes on CPython, so no lock is needed and
   none is taken -- a patched ``threading.Lock`` held by a native thread is a
   hazard, not a safety net.  After appending, a non-blocking ``os.write`` on a
   non-blocking pipe; ``BlockingIOError`` is swallowed, because a full pipe
   means the dispatcher has not drained yet, which means it is already going to
   be woken.
4. The dispatcher blocks in ``eventlet.hubs.trampoline(rfd, read=True,
   timeout=...)`` -- the hub's own readiness primitive, so it burns ZERO CPU
   while idle and resumes the moment the byte lands (p50 0.9 ms).  The timeout
   is a liveness backstop, not the delivery path, and the ``eventlet.timeout.
   Timeout`` it raises is a **BaseException subclass**: ``except Exception``
   does not catch it and the dispatcher would die silently.  It is caught by
   name below.
5. ``on_message`` is ALWAYS invoked on a greenlet when the process is
   monkey-patched, never on the poll thread -- the handlers re-enter
   ``subscribe``/``unsubscribe``/``publish`` and mutate agent state the
   heartbeat greenlet also touches.
6. NOT monkey-patched (the scheduler's asyncio process, and plain pytest): the
   poll thread calls ``on_message`` DIRECTLY, with no pipe and no dispatcher.
   That is exactly paho's model, and ``OpenRideScheduler._handle_reply`` is
   already written for it.  Detection is
   ``eventlet.patcher.is_monkey_patched('socket')``, evaluated once here.

**THE DISPATCHER DOES ROUTING ONLY, AND THAT IS NOT A STYLE CHOICE.**  If it
called ``on_message`` inline, all ~17 agents in a worker would have their step
handlers SERIALISED behind one greenlet: agent B could not start until agent A's
handler returned or yielded, and while A is parked on an HTTP call the
dispatcher is parked with it.  Today paho gives each agent its own network
greenlet and they interleave on I/O.  Serialising them re-creates precisely the
greenlet reply-serialisation that CLAUDE.md 6.17b identifies as the cause of the
2-second barrier stalls -- i.e. it makes strictly worse the one thing this
migration is meant to improve, WHILE EVERY TEST STILL PASSES.  So ``_route``
appends to each handle's own FIFO inbox and ``eventlet.spawn_n``s that handle's
``_pump``; per-agent FIFO stays exact and cross-agent concurrency stays at
today's level.

FORWARD-ONLY -- three independent mechanisms
--------------------------------------------
1. **Every assignment starts at the current end of the log.**  A partition enters
   the assignment set only via :meth:`_KafkaBus._assign_partitions`, which
   resolves the high watermark and assigns at that explicit numeric offset (with
   ``OFFSET_END`` as the fallback if the watermark query fails).  There is no
   code path that assigns without an explicit offset.  Numeric-watermark rather
   than logical ``OFFSET_END`` is deliberate: ``OFFSET_END`` is resolved
   ASYNCHRONOUSLY by librdkafka after the assign returns, so a record produced
   in the gap can be appended *before* the watermark query is served and then
   skipped -- silent under-delivery, and precisely what
   ``check_subscribe_in_effect_on_return`` exists to catch.  Resolving it
   ourselves moves the race to the safe side: we may over-deliver by the handful
   of records appended between the query and the assign, and the dispatch-time
   topic filter drops those that are not ours.
2. **No offsets are ever committed or fetched.**  ``enable.auto.commit`` is
   False and ``commit()`` appears nowhere.  Because the bus assigns and never
   subscribes there is no group membership, so librdkafka performs no
   OffsetFetch on start and writes nothing to ``__consumer_offsets``.  This is
   what defeats the failure the ABC names: a celery worker restarted mid-run
   cannot resume from a committed offset, because none exists.  It rebuilds its
   bus, assigns at the end of the log, and can never re-reply for a time step
   the scheduler has already closed.
3. ``auto.offset.reset: 'latest'`` as belt-and-braces.  It should be
   unreachable given (1) and (2); it is set so that if it ever IS reached the
   behaviour is forward-only rather than a full-log replay.

A DELIBERATE, BOUNDED IMPRECISION -- do not "fix" this by adding offset tracking.
Filtering happens at DISPATCH, against ``handle._topics`` as it stands at that
instant.  If agent B subscribes to a topic whose partition the process was
already reading, and a record for that topic is sitting in the dispatcher's
deque from a few milliseconds earlier, B receives it.  The window is one
dispatcher batch -- sub-millisecond to ~9 ms at the measured p95/max.  That is
over-delivery bounded by the in-flight queue, not replay from a durable offset,
and it is what MQTT itself does when a SUBSCRIBE races an in-flight PUBLISH.

APP-LANE FAN-OUT AMPLIFICATION -- MEASURED 2026-09-11, and it is negligible
---------------------------------------------------------------------------
The plan flagged this as the one unmeasured number, so it was measured rather
than assumed.  Two agents whose app topics hash onto the same partition each see
the other's records at the shared bus and reject them on the exact recovered
string.  The marginal cost of that reject path -- header decode plus a routing
dict miss, i.e. everything ``_on_record`` and ``_route`` do for a record that is
not ours -- is **0.66 us/record** on this box (200,000 iterations; delivering one
of our own costs 1.36 us for comparison).

Arithmetic for a 1000-truck run at the default 64 app partitions: ~200,000
app-lane records, a process assigned ~15 of 64 partitions therefore sees
~47,000 of them and rejects ~46,000 -> **~30 ms of Python CPU per process for
the whole run**, ~2 CPU-seconds across all 64 processes, against a run that
lasts 370-970 s.  The end-to-end figure including librdkafka's own fetch work
was 16 us/record for produce AND consume AND reject in one process, which bounds
the consume side well under that.

The knob is the app-lane partition count (``KAFKA_APP_PARTITIONS``, default 64).
Lowering it raises the redundant-decode count LINEARLY.  Do not set it to 1 "for
simplicity": that is a 64x amplification, and the measurement above is what says
64 is comfortable, not a guess.

THE POLL THREAD IS THE PROCESS'S SINGLE POINT OF FAILURE -- GUARD IT TOTALLY
----------------------------------------------------------------------------
There is ONE consumer thread per process and ~17 agents behind it.  If it exits,
every one of them goes deaf for the rest of the run, the bus still answers
``is_closed() == False`` so every later agent attaches to the corpse too, the
counters freeze at their last value, and ``_teardown_resources`` -- the only
place the counters were ever printed -- is never reached.  A clean log, clean
counters, and ``summary.json`` still saying ``ok: true``: the CLAUDE.md 6.23
shape.  Three things now stand against that, and all three are load-bearing:

1. ``_on_record`` is called inside its own ``try``, counted against
   ``record_errors``.  One malformed record cannot cost the thread.
2. ``_poll_loop`` wraps its whole body in ``try/finally``, so the thread cannot
   exit without logging loudly and tearing the bus down.  EVERY exit road is
   loud when handles were still attached, not just the exception one: a clean
   loop exit used to leave a corpse with no error line anywhere.
3. ``is_closed()`` tests ``poll_thread.is_alive()``, so a corpse is never handed
   to a new handle -- ``acquire_bus`` builds a fresh bus instead.
4. THE SAME APPLIES TO THE DISPATCHER GREENLET, which is the other half of the
   same single point of failure on a patched process and for most of this
   module's life had none of these guards.  ``_dispatch_loop`` now wraps
   ``_dispatch_forever`` in the identical shape, counts ``dispatcher_deaths``,
   and closes the bus; ``is_closed()`` tests dispatcher liveness as well as the
   thread's; and ``stats()`` carries ``dispatcher_alive``.
5. ``acquire_bus`` only protects a NEW handle.  The handles already bound to a
   bus that dies are rebound by ``KafkaTransport._ensure_live_bus``, counted as
   ``bus_rebinds``, because nothing re-acquires a bus mid-run on its own.

``orsim_topic_of`` is likewise TOTAL by contract (a non-UTF-8 header returns
``None``, counted as foreign), because it is the code that runs on this thread.

BACKPRESSURE, NOT DROPS, ON THE HANDOFF DEQUE
----------------------------------------------
``_queue`` is filled by the NATIVE poll thread at fetch rate and drained by a
greenlet that cannot run while the hub is parked -- during a ``tpool`` round
trip, or while all ~17 co-resident agents sit in slow Eve HTTP calls inside a
herd step.  librdkafka's ``queued.max.messages.kbytes`` defaults to 64 MB PER
PARTITION and a bus assigns ~18, so the upstream buffer is order 1 GB; letting
that materialise as Python objects is an OOM vector in a celery worker with
nothing logged.  The per-handle ``_inbox`` solves its version of this by
DROPPING the oldest, which is right there (a wedged agent is already broken) and
wrong here (the record would be a step broadcast the barrier is waiting on).  So
the poll thread stops fetching above ``QUEUE_HIGH_WATER`` and lets Kafka's own
fetch backpressure do the work, counting ``queue_stalls``.

DELIVERY REPORTS ARE THE ONLY ERROR CHANNEL
-------------------------------------------
``produce()`` returns normally for a topic the broker will reject; the sole
signal is an asynchronous ``KafkaError``.  So every produce carries
``on_delivery=self._on_delivery``, every publish calls ``producer.poll(0)`` so
those callbacks are actually served, **the poll thread serves them too** (an
agent that has stopped publishing stops serving its own callbacks, and the
report for its last reply would otherwise never be counted), and the counters in
``DEGRADED_COUNTERS`` are logged at bus shutdown at ERROR level when non-zero.
This project has already shipped a run that lost 3,247 events and still reported
``ok: true``; a counter nobody prints is the same defect with extra steps.

THREE THINGS ABOUT THAT CHANNEL ARE EASY TO GET WRONG, AND ALL THREE WERE:

* **It is blind for as long as ``message.timeout.ms``, whose librdkafka default
  is 300 s.**  Five minutes is longer than ``STEP_TIMEOUT``, longer than the
  30 s ORSim heartbeat threshold, and longer than ``BUS_LINGER_S``, so a total
  publish outage moved no counter and wrote no line until long after the
  affected agents had been pruned.  It is set explicitly now; see
  ``_producer_config``.
* **A counter that normal operation moves is not a signal.**
  ``unrouted_messages`` was in ``DEGRADED_COUNTERS`` while this module's own
  arithmetic says a healthy process rejects ~46,000 app-lane records per run,
  so every bus of every clean run logged ``ORSIM TRANSPORT DEGRADED``.  See
  :data:`DEGRADED_COUNTERS`.
* **A loss the code names in a log line but counts nowhere is still silent.**
  ``flush()`` and ``_teardown_resources`` logged ``they are LOST`` against no
  counter at all, so ``transport_health()`` reported a clean process.  That is
  ``unflushed_messages``.

RESIDUAL RISK, STATED RATHER THAN HIDDEN
----------------------------------------
``confluent_kafka`` does not DOCUMENT ``Consumer`` as thread-safe, and this
design calls ``incremental_assign`` / ``incremental_unassign`` / ``assignment``
from the hub (and ``get_watermark_offsets`` from a ``tpool`` thread) while a
native thread sits inside ``poll()``.  It was verified empirically against localhost:9094 (no exception,
no disturbance to already-assigned partitions, correct delivery before, between
and after the calls), but that is evidence, not a guarantee, and a librdkafka
upgrade could change it.  If it ever misbehaves the fallback is to queue
assignment mutations onto the poll thread -- but note that would reintroduce the
deadlock CONCURRENCY point 1 warns about, so it must be a lock-free command
deque the poll thread drains at the top of each iteration, NEVER a call that
waits for the poll thread.
"""

import atexit
import logging
import os
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from confluent_kafka import OFFSET_END, Consumer, KafkaException, Producer, TopicPartition

from apps.transport.lane_admin import ensure_run_lane_topics, lane_topic_names
from apps.transport.lane_topics import (
    LANE_BCAST,
    LANE_REPLY,
    ORSIM_TOPIC_HEADER,
    lane_for,
    orsim_topic_of,
    partition_for,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# eventlet detection and the unpatched primitives the poll thread must use.
# Resolved at import time; the monkeypatch, if any, is applied long before a
# transport is constructed (celery applies it in its pool bootstrap).
# --------------------------------------------------------------------------
try:  # pragma: no cover - eventlet is always present in this project's venv
    import eventlet
    import eventlet.patcher

    _EVENTLET = eventlet
    _native_threading = eventlet.patcher.original('threading')
    #: The ONLY sleep the poll thread may call.  See the module docstring,
    #: mechanism point 2 -- a patched ``time.sleep`` on a non-hub OS thread
    #: misbehaves, and the symptom is a starved thread somewhere else entirely.
    _native_sleep = eventlet.patcher.original('time').sleep
except Exception:  # pragma: no cover - defensive; eventlet missing is survivable
    _EVENTLET = None
    _native_threading = threading
    _native_sleep = time.sleep


def _is_monkey_patched() -> bool:
    """True when this process's stdlib has been eventlet-patched."""
    if _EVENTLET is None:
        return False
    try:
        return bool(_EVENTLET.patcher.is_monkey_patched('socket'))
    except Exception:  # pragma: no cover
        return False


#: How long the poll thread blocks in librdkafka per iteration.  Bounded so the
#: linger/expiry check below runs at a predictable cadence; it costs nothing,
#: because the thread is not on the hub.
POLL_TIMEOUT_S = 0.2

#: Liveness backstop for the dispatcher's ``trampoline``.  Delivery is driven by
#: the self-pipe byte, not by this.
DISPATCH_TICK_S = 0.5

#: How long a bus with zero handles stays alive before reaping itself.  Order
#: agents churn constantly; tearing a bus down and rebuilding it per departure
#: would cost a metadata round trip each time.
BUS_LINGER_S = 30.0

#: Per-handle inbox bound.  Silently unbounded growth on a wedged agent is worse
#: than a loud drop -- see ``inbox_overflow``.
DEFAULT_INBOX_MAX = 10_000

#: How long ``_log_end`` keeps asking for a partition's high watermark before
#: giving up and using the racier ``OFFSET_END``.  Sized for leader election on
#: a just-created topic, which is the only case that has ever needed it.
WATERMARK_RESOLVE_TIMEOUT_S = 10.0

#: Per-ATTEMPT budget for one ``get_watermark_offsets`` call.  Was 5.0, which
#: meant the 10 s budget above was spent in two uninterruptible blocks; under
#: eventlet each of those parked the hub and froze every co-resident agent
#: (MEASURED: a watchdog greenlet ran 122,345 slices in the 0.5 s before the
#: call and ZERO during a 5.0007 s call).  At 1 s the same budget is spread over
#: ~10 attempts with a cooperative yield between them -- and when the process is
#: patched the call itself is run off the hub in ``eventlet.tpool``, so the
#: yield-between-attempts is the second line of defence, not the only one.
WATERMARK_QUERY_TIMEOUT_S = 1.0

#: Backpressure bound for the poll-thread -> dispatcher handoff deque.  The
#: per-handle ``_inbox`` is bounded and DROPS; this one must not drop, because
#: the record it would lose is a step broadcast the barrier is waiting on.  So
#: the poll thread stops fetching instead: it skips ``consumer.poll()`` while the
#: deque is over the mark, which converts unbounded Python-object growth into
#: ordinary Kafka fetch backpressure.  The deque is filled by a NATIVE thread at
#: fetch rate and drained by a greenlet that cannot run while the hub is parked,
#: and librdkafka's own ``queued.max.messages.kbytes`` default is 64 MB PER
#: PARTITION over ~18 partitions -- order 1 GB of upstream buffer, which is an
#: OOM vector in a celery worker if it is allowed to materialise as _Message
#: objects.
QUEUE_HIGH_WATER = 50_000

#: How long the poll thread naps when it is holding off for backpressure.  Short
#: enough that the deque drains promptly once the hub is free again.
QUEUE_BACKPRESSURE_SLEEP_S = 0.01

#: Shared, constant, and inert: see BROADCAST above.
CONSUMER_GROUP_ID = 'orsim-transport'

#: Explicit producer delivery budget, in ms.  librdkafka's own default is
#: 300,000 -- five minutes of total silence on the ONLY error channel this
#: design has.  Sized at a few barrier steps so an undeliverable record becomes
#: a counted ``delivery_failures`` while the run can still be called bad.  See
#: :meth:`_KafkaBus._producer_config`.
DEFAULT_MESSAGE_TIMEOUT_MS = 10_000

#: How often a live bus publishes its counters to the cross-process health
#: topic.  The run's own ``summary.json`` is written by a process that hosts no
#: buses, and a bus does not tear down (and therefore did not publish) until
#: ``BUS_LINGER_S`` after its last handle leaves -- which is AFTER finalize.  A
#: periodic snapshot is what makes the counters reach the run outcome at all;
#: see :mod:`apps.transport.health_stream`.
HEALTH_PUBLISH_INTERVAL_S = 30.0

#: How many times :func:`acquire_bus` will re-try when the bus it found was
#: reaped out from under it between the check and the retain.  Two would do; the
#: extra attempts cost nothing and the alternative -- returning a dead bus -- is
#: one silently deaf agent for the rest of the run.
ACQUIRE_MAX_ATTEMPTS = 5


#: Counters whose non-zero value means the run LOST transport traffic, or came
#: close enough that its KPIs are not quotable as clean.  One named tuple so the
#: teardown log, :meth:`_KafkaBus.is_degraded` and :func:`transport_health`
#: cannot drift apart.
#:
#: ``unrouted_messages`` IS DELIBERATELY NOT IN HERE, and that is a correction
#: of a measured defect rather than a simplification.  App-lane partitions are
#: shared by hash, so a process routinely fetches records whose ORSim topic
#: belongs to an agent in a DIFFERENT process -- this module's own arithmetic
#: (see APP-LANE FAN-OUT AMPLIFICATION above) puts that at ~46,000 rejects per
#: process per run at the default 64 app partitions, and the sticky broadcast
#: partition adds one more guaranteed reject per step once every co-resident
#: agent is napping.  With it in this list ``is_degraded()`` was unconditionally
#: True on every HEALTHY bus: all ~64 workers logged the greppable
#: ``ORSIM TRANSPORT DEGRADED`` marker on every clean run, so the one signal
#: that is supposed to separate a lossy run from a clean one fired 100% of the
#: time and told an operator nothing.  REPRODUCED against localhost:9094 with a
#: single foreign app-lane record and with a napping-handle broadcast, in both
#: cases with every genuine loss counter at zero.  Crying wolf is the same
#: defect as silence (CLAUDE.md 8 / 6.23) with extra steps.
#:
#: The signature it was meant to catch -- a partition-count disagreement, where
#: publisher and subscriber compute different partitions from the same key and
#: records vanish with no error -- is now expressed as :func:`routing_blackhole`,
#: a RATIO-shaped test rather than a non-zero test.
DEGRADED_COUNTERS = ('dropped_publishes', 'delivery_failures',
                     'foreign_messages', 'inbox_overflow', 'consume_errors',
                     'record_errors', 'unflushed_messages', 'handler_errors',
                     'dispatcher_deaths', 'bus_rebinds')

#: Counters that are reported but never by themselves a verdict: normal
#: operation moves all of them.
DIAGNOSTIC_COUNTERS = ('unrouted_messages', 'queue_stalls',
                       'messages_in', 'messages_out')

#: How many records a bus must have FETCHED before "nothing was ever routed to a
#: local handle" is evidence of a partition-count disagreement rather than of a
#: bus that has simply not been used yet.  A real run's bus routes its first
#: step broadcast within a second or two of boot, so any floor above a handful
#: is safe; this one is sized so a short test or a just-booted process is never
#: accused.
ROUTING_BLACKHOLE_MIN_MESSAGES = 1_000


def routing_blackhole(stats: dict) -> bool:
    """True when a bus fetched plenty and routed NOTHING.

    This is the partition-count-disagreement signature, and it is the whole
    reason ``unrouted_messages`` was ever in the degraded list: a bus that holds
    ``app=64`` while the publisher holds ``app=1`` (the lane-topic resurrection
    hazard ``lane_admin`` exists to prevent) still fetches records all run and
    delivers none of them, with every loss counter at zero.

    Stated as a ratio rather than as ``unrouted_messages != 0`` because the
    non-zero form is TRUE ON EVERY HEALTHY RUN -- see :data:`DEGRADED_COUNTERS`.
    ``messages_out == 0`` is the ratio at its extreme (100% unrouted) and is the
    only form with no false positives: a healthy agent bus routes every step
    broadcast to all ~17 of its handles, so ``messages_out`` passes
    ``messages_in`` within the first few steps.
    """
    return (stats.get('messages_in', 0) >= ROUTING_BLACKHOLE_MIN_MESSAGES
            and stats.get('messages_out', 0) == 0)


def degraded_from(stats: dict) -> bool:
    """The ONE degraded verdict, over one bus's ``stats()`` or a sum of them."""
    if any(stats.get(name) for name in DEGRADED_COUNTERS):
        return True
    return routing_blackhole(stats)


class _Message:
    """The minimal paho-shaped message object ORSim's two handlers read.

    ``.topic`` is the RECOVERED ORSim string (byte-identical to what was
    published), never the Kafka topic name; ``.payload`` is ``bytes``, because
    both handlers call ``.decode('utf-8')`` on it.  Nothing else on a paho
    message is used anywhere in ORSim or in the apps.
    """

    __slots__ = ('topic', 'payload')

    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload

    def __repr__(self):  # pragma: no cover - diagnostics only
        return f'<_Message topic={self.topic!r} bytes={len(self.payload)}>'


class _KafkaBus:
    """The per-(bootstrap, run_id) per-process shared machinery.

    Not constructed directly: use :func:`acquire_bus`, which enforces the
    one-per-process rule and the refcount.
    """

    def __init__(self, bootstrap: str, run_id: str, settings: Optional[dict] = None):
        settings = settings or {}
        self.bootstrap = bootstrap
        self.run_id = run_id
        self.key = (bootstrap, run_id)

        # Counters.  Plain ints mutated from at most two threads; the GIL makes
        # ``+= 1`` non-atomic in principle, but these are diagnostics, and a
        # lock on the hot path would cost more than a lost increment.
        self.dropped_publishes = 0
        self.delivery_failures = 0
        self.foreign_messages = 0
        self.inbox_overflow = 0
        self.unrouted_messages = 0
        self.consume_errors = 0
        self.messages_in = 0
        self.messages_out = 0
        #: Records the producer still held when its flush budget ran out.  The
        #: code called this loss ``they are LOST`` in a log line and counted it
        #: NOWHERE, so ``is_degraded()`` answered False on a run that had
        #: irrecoverably dropped agent replies -- the converse hole to the
        #: ``unrouted_messages`` false positive, and the worse of the two.
        #: Incremented by ``flush()`` when its deadline expires and again by
        #: ``_teardown_resources`` for whatever the final 2 s flush still holds;
        #: the same record can therefore be counted twice, which is acceptable
        #: for a verdict that only asks "was anything lost at all?".
        self.unflushed_messages = 0
        #: ``on_message`` handlers that RAISED.  A handler that fails on every
        #: record (a truncated payload, a ``json.loads`` on a half-written
        #: value) leaves the agent silent and the scheduler prunes it; before
        #: this counter existed ``_pump`` logged and moved on with every counter
        #: reading clean.
        self.handler_errors = 0
        #: The dispatcher greenlet exited while the bus was open.  Total,
        #: silent, permanent deafness for every agent in the process -- the one
        #: half of the delivery path that had no guard at all.
        self.dispatcher_deaths = 0
        #: Handles that had to be re-bound onto a fresh bus because the one they
        #: held had died.  Non-zero means this process went deaf for at least as
        #: long as it took the owning agent to notice.
        self.bus_rebinds = 0
        #: How many poll iterations were skipped for handoff backpressure.  Not
        #: a loss counter -- nothing is dropped -- but a non-zero value means the
        #: hub was parked long enough for the fetch rate to outrun the
        #: dispatcher, which is the signal that precedes a real stall.
        self.queue_stalls = 0
        #: Records that reached ``_on_record`` and raised on the way to the
        #: dispatcher.  Before the guard below existed, ONE of these killed the
        #: process's only poll thread.
        self.record_errors = 0

        self._inbox_max = int(settings.get('KAFKA_INBOX_MAX', DEFAULT_INBOX_MAX))
        self._patched = _is_monkey_patched()

        # Blocking broker work FIRST: if the lane topics cannot be confirmed the
        # constructor raises and no thread, socket or fd has been created yet.
        # LIFECYCLE point 2 -- fail loudly at construction.
        self.lane_partitions: Dict[str, int] = ensure_run_lane_topics(
            run_id,
            bootstrap,
            reply_partitions=int(settings.get('KAFKA_REPLY_PARTITIONS', 8)),
            app_partitions=int(settings.get('KAFKA_APP_PARTITIONS', 64)),
        )
        self.lane_topics: Dict[str, str] = lane_topic_names(run_id)

        self._consumer = Consumer(self._consumer_config())
        self._producer = Producer(self._producer_config(settings))

        self._routes: Dict[str, List] = {}
        self._partition_refs: Dict[Tuple[str, int], int] = {}
        self._queue = deque()

        # An UNPATCHED lock, because it is taken by the native poll thread as
        # well as by greenlets.  Every critical section it guards is pure-Python
        # dict/int work plus at most one local librdkafka call -- no I/O, no
        # nesting, no callback -- so the hub is never parked on it for a
        # meaningful time.  A green lock here would be a correctness bug: the
        # poll thread cannot yield to the hub.
        self._lock = _native_threading.Lock()

        # A SECOND lock, and a deliberately different KIND.  It serialises the
        # consumer's assignment mutations (``incremental_assign`` /
        # ``incremental_unassign`` and the watermark query that precedes them),
        # which is what makes "check whether this partition is assigned, then
        # assign it" one transaction.  Without that, two co-resident agents
        # attaching the same partition concurrently both see it unassigned and
        # the second ``incremental_assign`` raises ``_CONFLICT``.
        #
        # It is the PATCHED lock on purpose, unlike ``_lock``: every caller
        # (``attach_topic`` / ``detach_topic``, i.e. an agent's subscribe or
        # unsubscribe) is hub-side, the poll thread never takes it, and a green
        # lock lets a waiting greenlet yield instead of parking the hub.  Never
        # take it while holding ``_lock`` -- the two critical sections are
        # strictly sequential below, never nested.
        self._assign_lock = threading.Lock()

        self._refcount = 0
        self._reap_at: Optional[float] = None
        self._stopping = False
        self._closed = False
        self._started = False
        self._torn_down = False
        self._pending_publishes = False

        self._rfd = self._wfd = None
        self._poll_thread = None
        self._dispatcher = None
        self._dispatcher_alive = False
        self._shutdown_requested = False
        self._next_health_publish = 0.0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _consumer_config(self) -> dict:
        """Assign-only consumer config.  ``subscribe()`` is never called on it."""
        return {
            'bootstrap.servers': self.bootstrap,
            # Required by librdkafka; inert under assign-only (no JoinGroup is
            # ever sent).  Shared and constant on purpose -- see BROADCAST.
            'group.id': CONSUMER_GROUP_ID,
            'enable.auto.commit': False,
            'auto.offset.reset': 'latest',
            # Never let a client resurrect a lane topic.  This broker runs with
            # ``auto.create.topics.enable=true`` and ``num.partitions=1``, so a
            # single request naming a deleted lane topic recreates it with ONE
            # partition -- after which buses built before the deletion hold
            # app=64 and buses built after hold app=1, publishers and
            # subscribers compute different partitions, and records vanish with
            # no error anywhere.  That is the failure shape ``lane_admin``'s own
            # docstring calls the worst this project has.  Topic existence is
            # established exactly once, by ``ensure_run_lane_topics``.
            'allow.auto.create.topics': False,
            # Small fetch wait: the barrier cares about latency, not throughput.
            'fetch.wait.max.ms': 10,
            'socket.nagle.disable': True,
            'client.id': f'orsim-bus-{os.getpid()}',
        }

    def _producer_config(self, settings: dict) -> dict:
        """Producer config, with ordering chosen over peak throughput.

        ``max.in.flight.requests.per.connection: 1`` is the deliberate one.
        Per-agent reply ORDER is load-bearing: the reply lane is keyed by agent
        so an agent's step-T and step-T+1 replies land on the same partition,
        and the barrier must never observe them inverted.  Without idempotence
        (off here for the same reason ``KAFKA_ENABLE_IDEMPOTENCE`` is off
        project-wide -- it waits on the transaction coordinator at startup), a
        retry with several requests in flight can reorder them.

        ``linger.ms`` DEFAULTS TO 0, and the 5 ms it used to default to was
        measured as a pure cost.  The barrier critical path is two
        single-record publishes -- the scheduler's one broadcast per step, and
        each agent's one reply -- and neither has anything to batch with, so
        linger is latency paid twice per step.  MEASURED end to end against
        localhost:9094 (one unpatched scheduler transport, a monkey-patched
        child with 17 co-resident handles whose handler sleeps 100 ms to stand
        in for the in-run Eve PATCH, 120 steps, n=3 per arm): broadcast -> all
        17 replies p50 = 113.6 / 113.3 / 113.6 ms at linger=5 against
        103.9 / 103.4 / 103.3 ms at linger=0 -- disjoint ranges, ~10 ms per
        step, ~25 s on a 2592-step run, and NOT absorbed by the 50 ms
        ``MIN_STEP_WALL_TIME_MS`` floor because quiet steps already measure
        ~51 ms (CLAUDE.md 6.22).  Ordering is untouched: it rests on
        ``max.in.flight.requests.per.connection: 1``, not on linger.  The old
        throughput argument for linger=5 (the reply lane needs ~2.6M records
        over a ~400 s run spread across 64 processes -- order 100 records/s per
        producer) was never a constraint librdkafka needed help with: it still
        batches whatever is queued when a request slot frees.

        ``message.timeout.ms`` IS SET EXPLICITLY, and that is the critical one.
        Delivery reports are the ONLY error channel this design has (see the
        module docstring), and librdkafka's default ``message.timeout.ms`` is
        **300 s** -- so a total publish outage moved ZERO counters and wrote
        ZERO log lines for five minutes, far longer than ``STEP_TIMEOUT``, the
        30 s ORSim heartbeat threshold or ``BUS_LINGER_S``.  REPRODUCED with
        this exact config: 5 records to a nonexistent lane topic gave
        ``flush(20) -> 1 still queued`` with the delivery callback fired ZERO
        times in 20 s, while the same produce with ``message.timeout.ms=3000``
        reported ``KafkaError{_MSG_TIMED_OUT}`` at 3.003 s.  That is the same
        error code CLAUDE.md 8 records losing 3,247 ``facility_stream`` events
        to.  The value must stay >= ``linger.ms`` (librdkafka enforces it) and
        should stay on the order of a few steps of the barrier's own budget, so
        an undeliverable record becomes a counted ``delivery_failures`` inside
        one or two steps instead of five minutes.

        ``retries`` DEFAULTS TO 0, and that is a correctness choice over a
        robustness one.  ``retries > 0`` without ``enable.idempotence`` permits
        broker-side DUPLICATES (a ProduceRequest whose ack is lost after the
        broker appended is the canonical case), and nothing in ORSim or in the
        apps dedupes on ``time_step``: a duplicated step broadcast makes every
        agent in all 64 processes run its step handler twice for one time step
        -- the state machine advances twice and the trip/PATCH writes are
        duplicated -- and a duplicated reply makes the barrier count one agent
        twice.  Both shift KPIs with no counter moving and no log line.  A LOST
        record, by contrast, is reported on the delivery callback, counted in
        ``delivery_failures``, and makes the run non-quotable: visible beats
        silent, which is this module's standing rule.  MQTT qos=0 has no
        application-level retry either, so 0 is also the closer match to the
        transport being replaced.  ``KAFKA_PRODUCER_RETRIES`` exists for a
        broker whose leader elections turn out to need it -- but raising it
        re-admits duplicates, so pair it with a ``time_step`` guard in the step
        handler rather than taking it on its own.
        """
        return {
            'bootstrap.servers': self.bootstrap,
            'linger.ms': int(settings.get('KAFKA_LINGER_MS', 0)),
            'acks': 1,
            'enable.idempotence': False,
            'retries': int(settings.get('KAFKA_PRODUCER_RETRIES', 0)),
            'message.timeout.ms': int(settings.get('KAFKA_MESSAGE_TIMEOUT_MS',
                                                   DEFAULT_MESSAGE_TIMEOUT_MS)),
            'max.in.flight.requests.per.connection': 1,
            'compression.type': 'none',
            'socket.nagle.disable': True,
            # The producer is the side that actually resurrected a reaped lane
            # topic in the repro: ~20 straggler publishes from a bus that
            # outlived the reap recreated it at ONE partition.  See the
            # consumer config above for why that is catastrophic rather than
            # untidy.  With this false, those publishes fail loudly against
            # ``delivery_failures`` instead of silently re-partitioning the run.
            'allow.auto.create.topics': False,
            'client.id': f'orsim-bus-{os.getpid()}',
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> '_KafkaBus':
        """Start the poll thread (and, when patched, the dispatcher greenlet).

        Separate from ``__init__`` so that :func:`acquire_bus` can lose a
        construction race and discard the loser without ever having started a
        thread.
        """
        if self._started:
            return self
        self._started = True
        if self._patched:
            self._rfd, self._wfd = os.pipe()
            os.set_blocking(self._rfd, False)
            os.set_blocking(self._wfd, False)
            self._dispatcher_alive = True
            self._dispatcher = _EVENTLET.spawn(self._dispatch_loop)
        self._poll_thread = _native_threading.Thread(
            target=self._poll_loop,
            name=f'orsim-bus-poll-{self.run_id}',
            daemon=True,
        )
        self._poll_thread.start()
        return self

    def is_closed(self) -> bool:
        """True when this bus must not be handed to a new handle.

        Includes a LIVENESS test, not just the flag.  The poll thread is the
        process's only consumer; if it ever exits while the flag says open, the
        bus is a corpse that ``acquire_bus`` would happily hand to every agent
        that boots afterwards -- each of them deaf for the rest of the run, with
        every counter frozen at its last value and nothing logged.  Reporting
        the corpse as closed makes ``acquire_bus`` drop it and build a fresh
        one, which is the only recovery available in-process.
        """
        if self._closed:
            return True
        thread = self._poll_thread
        if self._started and thread is not None and not thread.is_alive():
            logger.error(
                'orsim bus %s: poll thread is DEAD while the bus reads open; '
                'treating the bus as closed so a fresh one is built. stats=%s',
                self.run_id, self.stats())
            self._closed = True
            return True
        # The DISPATCHER is the other half of the delivery path on a patched
        # process, and it had no liveness test at all: a bus whose dispatcher
        # had died answered "open" and was handed to every agent that booted
        # afterwards, each of them silently deaf.  Treating it as closed is the
        # same recovery the poll-thread test above buys.
        if not self._dispatcher_is_alive():
            logger.error(
                'orsim bus %s: dispatcher greenlet is DEAD while the bus reads '
                'open; treating the bus as closed so a fresh one is built. stats=%s',
                self.run_id, self.stats())
            self._closed = True
            return True
        return False

    def retain(self) -> bool:
        """Claim one handle's share of this bus.  FAILS CLOSED.

        Returns False -- and does NOT bump the refcount -- when the bus is
        already closing or closed.  ``acquire_bus`` checks ``is_closed()`` and
        then calls this in a SEPARATE lock acquisition, while the reaper runs on
        a genuinely preemptive OS thread, so the check and the act are not
        atomic with respect to it.  A ``retain`` that silently succeeded on a
        reaped bus would resurrect its refcount and bind the new handle to a
        bus with no consumer, no poll thread and a destroyed producer: that one
        agent receives nothing for the rest of the run and the barrier burns
        ``STEP_TIMEOUT`` on it every step, with nothing raised and nothing
        counted.  Returning False collapses check-then-act into one critical
        section on the bus's own lock.
        """
        with self._lock:
            if self._closed or self._stopping:
                return False
            self._refcount += 1
            self._reap_at = None
            return True

    def release(self) -> None:
        """Drop one handle.  At zero the bus LINGERS before reaping itself.

        The linger is invisible to ``check_disconnect_stops_receive_loop``,
        which is about the HANDLE: its topics are gone from the routing table so
        delivery to it has stopped, and ``loop_is_running(t)`` answers
        ``t._loop_running``, which is False.  Worth saying plainly: this design
        has ZERO per-agent threads, so the per-agent thread leak that check
        exists to catch cannot occur here at all.
        """
        with self._lock:
            self._refcount -= 1
            if self._refcount <= 0:
                self._refcount = 0
                self._reap_at = time.monotonic() + BUS_LINGER_S

    def _expired(self) -> bool:
        """Poll-thread-side linger check.  True means "stop and tear down"."""
        with self._lock:
            if self._closed or self._stopping:
                return True
            if self._reap_at is None or time.monotonic() < self._reap_at:
                return False
            if self._refcount > 0:          # someone re-attached during the linger
                self._reap_at = None
                return False
            self._stopping = True
            self._closed = True
            return True

    def shutdown(self, join_timeout: float = 2.0) -> None:
        """Stop everything now.  Idempotent; safe from a greenlet or a thread.

        ``join_timeout`` bounds a native-thread join, which under eventlet parks
        the hub for its duration.  That is acceptable only because this is a
        teardown path (tests, process exit, a lost construction race); the
        normal end of a bus is the self-reap on the poll thread, which joins
        nothing.
        """
        with self._lock:
            self._shutdown_requested = True
            if self._closed and not self._started:
                return
            already = self._stopping
            self._stopping = True
            self._closed = True
        if not already:
            self._wake_dispatcher()
        thread = self._poll_thread
        if thread is not None and thread is not _native_threading.current_thread():
            thread.join(join_timeout)
            if thread.is_alive():  # pragma: no cover - librdkafka wedged
                logger.error('orsim bus poll thread for %s did not stop within %ss',
                             self.run_id, join_timeout)
        self._teardown_resources()

    def discard(self) -> None:
        """Destroy a bus that was BUILT BUT NEVER REGISTERED.  Registry-free.

        The only caller is :func:`acquire_bus`'s lost-construction-race path.
        It closes this bus's own consumer and producer and touches no shared
        state at all -- in particular it does not go near ``_REGISTRY``, where
        the winner is sitting under the identical key.
        """
        with self._lock:
            if self._torn_down:
                return
            self._torn_down = True
            self._shutdown_requested = True
            self._stopping = True
            self._closed = True
        try:
            self._producer.flush(0)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._consumer.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug('orsim bus %s: discard consumer close failed: %s',
                         self.run_id, exc)

    def _teardown_resources(self) -> None:
        """Close the consumer, flush+drop the producer, close the pipe, deregister.

        Called on the poll thread at self-reap, or on the caller's thread from
        :meth:`shutdown`.  Guarded so either road is safe and the second is a
        no-op.
        """
        with self._lock:
            if self._torn_down:
                return
            self._torn_down = True

        # IDENTITY-CHECKED, and that check is load-bearing.  ``acquire_bus``'s
        # lost-construction-race path calls ``shutdown()`` on a bus that has the
        # SAME key as the winner, so an unconditional pop evicts the LIVE winner
        # from the registry: the one-bus-per-process invariant -- the central
        # design decision of this whole phase -- silently collapses, the next
        # agent to boot builds a SECOND consumer/producer/poll thread for the
        # same run, and the evicted bus becomes invisible to ``get_bus`` /
        # ``all_bus_stats`` / ``shutdown_all_buses``, so its counters are never
        # surfaced and its producer is never flushed.  MEASURED: 17 greenlets
        # booting concurrently left ``len(_REGISTRY) == 0`` with a live bus
        # serving all 17 handles.  The same guard also covers the smaller
        # variant where a self-reaping bus tears down after a replacement has
        # already been registered for its key.
        with _REGISTRY_LOCK:
            if _REGISTRY.get(self.key) is self:
                _REGISTRY.pop(self.key, None)

        try:
            remaining = self._producer.flush(2.0)
            if remaining:
                # COUNTED, not merely logged.  This is the one loss the code
                # itself labelled ``they are LOST`` while leaving every counter
                # at zero, so ``transport_health()`` reported ``degraded:
                # False`` on a process that had irrecoverably dropped agent step
                # replies -- the scheduler then prunes or heartbeat-kills those
                # agents, the run finishes with fewer orders, and summary.json
                # says ok: true.  ``delivery_failures`` cannot cover it:
                # ``allow.auto.create.topics=False`` means a produce to a
                # missing or leaderless topic gets no delivery report until
                # ``message.timeout.ms``, i.e. after this bus is gone.
                self.unflushed_messages += remaining
                logger.error(
                    'orsim bus %s: %d message(s) still unflushed at shutdown; '
                    'they are LOST', self.run_id, remaining)
        except Exception as exc:  # noqa: BLE001
            logger.warning('orsim bus %s: producer flush failed at shutdown: %s',
                           self.run_id, exc)
        try:
            self._consumer.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning('orsim bus %s: consumer close failed: %s', self.run_id, exc)

        for fd in (self._rfd, self._wfd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._rfd = self._wfd = None

        self._log_counters()

    #: See the module-level :data:`DEGRADED_COUNTERS`.  Kept as a class
    #: attribute because callers (and tests) reach it through the class.
    DEGRADED_COUNTERS = DEGRADED_COUNTERS

    def is_degraded(self) -> bool:
        """True when this bus lost, dropped or failed to deliver anything.

        A thin wrapper over :func:`degraded_from` so this bus, the teardown log
        line and the process-wide :func:`transport_health` can never disagree.
        """
        return degraded_from(self.stats())

    def _log_counters(self, reason: str = 'closed') -> None:
        """Surface the counters somewhere an operator actually sees them.

        Reachable from THREE places, because for most of this class's life it
        had only one: the poll thread's self-reap, which is precisely the path
        that a dead poll thread makes unreachable.  It is now also called from
        the poll loop's ``finally`` and from the process ``atexit`` hook, so a
        killed worker still prints.  The marker string below is deliberately
        greppable: these lines land in ``journalctl --user -u openride-celery``,
        not in ``sim.log`` and not in the 0-byte ``celery_log.txt``.
        """
        stats = self.stats()
        if degraded_from(stats):
            logger.error('ORSIM TRANSPORT DEGRADED: bus %s %s: %s',
                         self.run_id, reason, stats)
        else:
            logger.info('orsim bus %s %s: %s', self.run_id, reason, stats)
        self._publish_health(reason, stats)

    def _maybe_publish_health(self) -> None:
        """Publish a periodic counter snapshot.  Poll thread; never raises.

        The counters are per-PROCESS and the run's ``summary.json`` is written
        by a process that hosts no buses, so without a cross-process channel a
        run in which 64 workers each dropped thousands of publishes finishes
        with ``status: completed, ok: true, error: null`` and the only evidence
        in a journal nobody greps (CLAUDE.md 6.21 records that channel being
        missed twice in one session).  ``_teardown_resources`` alone is not
        enough either: a bus does not tear down until ``BUS_LINGER_S`` after its
        last handle leaves, which is AFTER the run finalises, so a snapshot
        every :data:`HEALTH_PUBLISH_INTERVAL_S` is what makes the numbers
        reachable while the sim is still writing its outcome.
        """
        now = time.monotonic()
        if now < self._next_health_publish:
            return
        self._next_health_publish = now + HEALTH_PUBLISH_INTERVAL_S
        self._publish_health('periodic', self.stats())

    def _publish_health(self, reason: str, stats: Optional[dict] = None) -> None:
        """Hand one snapshot to :mod:`apps.transport.health_stream`.  Never raises."""
        try:
            from apps.transport.health_stream import publish_bus_health

            publish_bus_health(self.bootstrap, self.run_id,
                               stats if stats is not None else self.stats(),
                               reason=reason)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            logger.debug('orsim bus %s: health publish skipped: %s', self.run_id, exc)

    def stats(self) -> dict:
        """Counters plus enough shape to diagnose a stall.  Cheap; safe anywhere."""
        return {
            'run_id': self.run_id,
            'pid': os.getpid(),
            'patched': self._patched,
            'handles': self._refcount,
            'topics_routed': len(self._routes),
            'partitions_assigned': len(self._partition_refs),
            'poll_alive': bool(self._poll_thread is not None
                               and self._poll_thread.is_alive()),
            # The second half of the delivery path on a patched process, and the
            # half that used to be unobservable: a dead dispatcher is total
            # silent deafness, so it gets a field of its own rather than being
            # inferred from a frozen queue depth.
            'dispatcher_alive': self._dispatcher_is_alive(),
            'queue_depth': len(self._queue),
            'queue_stalls': self.queue_stalls,
            'record_errors': self.record_errors,
            'messages_in': self.messages_in,
            'messages_out': self.messages_out,
            'dropped_publishes': self.dropped_publishes,
            'delivery_failures': self.delivery_failures,
            'foreign_messages': self.foreign_messages,
            'inbox_overflow': self.inbox_overflow,
            'unrouted_messages': self.unrouted_messages,
            'consume_errors': self.consume_errors,
            'unflushed_messages': self.unflushed_messages,
            'handler_errors': self.handler_errors,
            'dispatcher_deaths': self.dispatcher_deaths,
            'bus_rebinds': self.bus_rebinds,
        }

    def _dispatcher_is_alive(self) -> bool:
        """True when this bus can still deliver.  Trivially True when unpatched.

        An unpatched bus (the asyncio scheduler process, plain pytest) has no
        dispatcher at all -- the poll thread calls ``on_message`` directly -- so
        "the dispatcher is dead" is not a state it can be in.
        """
        if not self._patched or not self._started:
            return True
        dispatcher = self._dispatcher
        if dispatcher is None:
            return True
        return bool(self._dispatcher_alive and not getattr(dispatcher, 'dead', False))

    # ------------------------------------------------------------------
    # Receive side
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """The native OS thread.  NO eventlet primitive may be touched in here.

        See the module docstring, mechanism points 1 and 2.  ``_native_sleep``
        is the only sleep permitted; ``time.sleep`` would be the PATCHED one.

        **THIS FUNCTION MAY NEVER EXIT BY EXCEPTION.**  It is the process's ONLY
        consumer thread.  For most of this class's life only ``consumer.poll()``
        was guarded and ``self._on_record(msg)`` sat outside the ``try`` with no
        outer guard at all, so any exception raised while RECOVERING a record
        killed the thread: the bus then reported ``is_closed() == False``, stayed
        in the registry so every later agent attached to it, every counter froze
        at its last value, and ``_teardown_resources`` -- the only place the
        counters were ever printed -- was never reached.  MEASURED: one record
        produced to a lane topic with a non-UTF-8 ``orsim_topic`` header left the
        process deaf for the rest of the run with a clean log and clean
        counters.  Two guards now stand between that and the thread: a per-record
        ``try`` below, and the whole-body ``try/finally`` here.
        """
        try:
            self._poll_forever()
        except BaseException:       # noqa: BLE001 - a silent exit is the bug
            logger.exception(
                'ORSIM TRANSPORT DEGRADED: bus %s poll thread died; this process '
                'is deaf for the rest of the run. stats=%s', self.run_id, self.stats())
            raise
        finally:
            # EVERY exit is loud when handles were still attached, not only the
            # exception path.  A clean loop exit -- a spurious ``_expired()``,
            # say -- used to produce a corpse with no error line anywhere: the
            # handles bound to it kept answering success while receiving
            # nothing, and ``_log_counters`` printed at INFO because no counter
            # had moved.  A reap with zero handles is the NORMAL end of a bus
            # and stays quiet; so does an explicit ``shutdown()``.
            orphaned = self._refcount
            if orphaned > 0 and not self._shutdown_requested:
                logger.error(
                    'ORSIM TRANSPORT DEGRADED: bus %s poll thread exited while %d '
                    'handle(s) were still attached; every agent behind them is deaf '
                    'for the rest of the run. stats=%s',
                    self.run_id, orphaned, self.stats())
            with self._lock:
                self._stopping = True
                self._closed = True
            self._wake_dispatcher()
            self._teardown_resources()

    def _poll_forever(self) -> None:
        """The loop body of :meth:`_poll_loop`.  Split out so the guard is total."""
        consumer = self._consumer
        while not self._stopping:
            # BACKPRESSURE, not a drop: see QUEUE_HIGH_WATER.  Skipping the
            # fetch lets librdkafka's own buffers fill and the broker throttle
            # us, rather than materialising an unbounded number of _Message
            # objects while the hub is parked and the dispatcher cannot run.
            if self._patched and len(self._queue) >= QUEUE_HIGH_WATER:
                self.queue_stalls += 1
                if self.queue_stalls == 1 or self.queue_stalls % 1000 == 0:
                    logger.error(
                        'orsim bus %s: handoff queue at %d (>= %d); pausing fetch '
                        '(%d times so far). The dispatcher greenlet is not being '
                        'scheduled -- suspect a parked hub.',
                        self.run_id, len(self._queue), QUEUE_HIGH_WATER,
                        self.queue_stalls)
                self._wake_dispatcher()
                _native_sleep(QUEUE_BACKPRESSURE_SLEEP_S)
                if self._expired():
                    break
                continue
            try:
                msg = consumer.poll(POLL_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any single error
                self.consume_errors += 1
                if self.consume_errors <= 5 or self.consume_errors % 1000 == 0:
                    logger.error('orsim bus %s: consumer.poll failed (%d): %s',
                                 self.run_id, self.consume_errors, exc)
                _native_sleep(0.05)
                msg = None
            if msg is not None:
                try:
                    err = msg.error()
                except Exception as exc:  # noqa: BLE001
                    err = exc
                if err is not None:
                    self.consume_errors += 1
                    if self.consume_errors <= 5 or self.consume_errors % 1000 == 0:
                        logger.error('orsim bus %s: consume error (%d): %s',
                                     self.run_id, self.consume_errors, err)
                else:
                    try:
                        self._on_record(msg)
                    except Exception as exc:  # noqa: BLE001 - ONE bad record must
                        # not cost the process its only consumer thread.
                        self.record_errors += 1
                        if self.record_errors <= 5 or self.record_errors % 1000 == 0:
                            logger.error(
                                'orsim bus %s: record handling failed (%d): %s',
                                self.run_id, self.record_errors, exc, exc_info=True)
            # SERVE DELIVERY CALLBACKS HERE TOO, not only from ``publish``.
            # Delivery reports are the only error channel this design has, and
            # an agent that has stopped publishing (napping indefinitely, or
            # finished) stops serving them -- so the ``_MSG_TIMED_OUT`` for its
            # last reply would never be counted and never logged.  ``poll(0)``
            # is non-blocking, ``Producer`` is thread-safe, and ``_on_delivery``
            # touches nothing but counters and the logger, which this thread
            # already uses.
            if self._pending_publishes:
                try:
                    self._producer.poll(0)
                except Exception:  # noqa: BLE001 - diagnostics must not cost the thread
                    pass
            self._maybe_publish_health()
            if self._expired():
                break

    def _on_record(self, msg) -> None:
        """Recover the ORSim topic and hand the record on.  Poll thread only."""
        topic = orsim_topic_of(msg)
        if topic is None:
            # Never guessed from msg.topic(): see lane_topics.orsim_topic_of.
            self.foreign_messages += 1
            if self.foreign_messages <= 5 or self.foreign_messages % 1000 == 0:
                logger.warning(
                    'orsim bus %s: dropped a record on %r with no %r header (%d so far)',
                    self.run_id, msg.topic(), ORSIM_TOPIC_HEADER, self.foreign_messages)
            return
        self.messages_in += 1
        item = _Message(topic, msg.value() or b'')
        if self._patched:
            self._queue.append(item)        # GIL-atomic; no lock, by design
            self._wake_dispatcher()
        else:
            # Unpatched (asyncio scheduler process, plain pytest): deliver on
            # this thread, which is exactly paho's model.
            self._route(item)

    def _wake_dispatcher(self) -> None:
        wfd = self._wfd
        if wfd is None:
            return
        try:
            os.write(wfd, b'x')
        except (BlockingIOError, InterruptedError, OSError):
            # A full pipe means the dispatcher has not drained yet, which means
            # it is already going to be woken.  A closed pipe means teardown.
            pass

    def _dispatch_loop(self) -> None:
        """Guarded wrapper around :meth:`_dispatch_forever`.

        **THE DISPATCHER MAY NOT DIE QUIETLY**, for the same reason the poll
        thread may not (see :meth:`_poll_loop`): on a monkey-patched process it
        is the second half of the ONLY delivery path, so its death is total,
        silent, permanent deafness for every agent in the worker.  Before this
        guard existed it had none at all -- ``is_closed()`` tested only the poll
        thread, ``stats()`` had no dispatcher field, and the only thing that
        would eventually have complained (``queue_stalls``) needs 50,000 queued
        records to fire, against the ~50,000 a whole process sees in a run.
        REPRODUCED: with the dispatcher dead, three agents received 0 of 5
        broadcasts while ``is_closed()``, ``is_degraded()`` and
        ``transport_health()['degraded']`` all answered False and the teardown
        line printed at INFO.

        ``GreenletExit`` is a ``BaseException``, so ``except Exception`` would
        miss an external ``kill()`` -- the body is caught by ``BaseException``
        and the bookkeeping sits in ``finally`` so every exit road is covered.
        The bus is CLOSED rather than torn down here: ``_teardown_resources``
        makes blocking librdkafka calls, and doing those on the hub is what the
        whole eventlet section of this module exists to avoid.  Closing is
        enough -- the poll thread notices ``_stopping`` within
        ``POLL_TIMEOUT_S`` and runs the ordinary teardown, which logs the
        counters with ``dispatcher_deaths`` now among them, and ``is_closed()``
        answers True immediately so no further handle is bound to the corpse.
        """
        try:
            self._dispatch_forever()
        except BaseException:       # noqa: BLE001 - a silent exit is the bug
            if not self._stopping:
                logger.exception('ORSIM TRANSPORT DEGRADED: bus %s dispatcher '
                                 'greenlet raised', self.run_id)
            raise
        finally:
            self._dispatcher_alive = False
            if not self._stopping:
                self.dispatcher_deaths += 1
                logger.error(
                    'ORSIM TRANSPORT DEGRADED: bus %s dispatcher greenlet EXITED '
                    'while the bus was open; every agent in this process is deaf. '
                    'Closing the bus so a fresh one is built. stats=%s',
                    self.run_id, self.stats())
                with self._lock:
                    self._stopping = True
                    self._closed = True

    def _dispatch_forever(self) -> None:
        """The one greenlet per process that does ROUTING ONLY.

        It must never call ``on_message`` -- see the module docstring's
        capitalised paragraph.  Everything here is O(handles for this topic).
        """
        from eventlet.hubs import trampoline
        from eventlet.timeout import Timeout

        while not self._stopping:
            try:
                trampoline(self._rfd, read=True, timeout=DISPATCH_TICK_S)
            except Timeout:
                # BaseException subclass: `except Exception` would miss it and
                # the dispatcher would die silently, taking every agent in this
                # process deaf with it.
                pass
            except Exception as exc:  # noqa: BLE001
                if self._stopping:
                    break
                logger.warning('orsim bus %s: dispatcher trampoline error: %s',
                               self.run_id, exc)
                _EVENTLET.sleep(0.01)
            try:
                os.read(self._rfd, 65536)
            except (BlockingIOError, InterruptedError, OSError, TypeError):
                pass
            self._drain()
        self._drain()

    def _drain(self) -> None:
        queue = self._queue
        while True:
            try:
                item = queue.popleft()      # GIL-atomic; the poll thread appends
            except IndexError:
                return
            self._route(item)

    def _route(self, item: _Message) -> None:
        """Fan one recovered message out to EVERY handle that wants that topic.

        A LIST, not a single lookup: the ~17 co-resident agents each get their
        own delivery of a broadcast.  A record for a topic no local handle wants
        is normal -- app-lane partitions are shared by hash -- and is counted
        (``unrouted_messages``, a DIAGNOSTIC counter: see
        :data:`DEGRADED_COUNTERS` for why it is not a verdict) rather than
        logged.

        ``messages_out`` counts records actually ENQUEUED on a handle's inbox.
        It used to be incremented whenever ``_deliver`` merely returned, which
        included the inbox-overflow path where ``_deliver`` had just evicted a
        message -- so the two counters could not be reconciled against the
        handler deliveries they were supposed to describe.
        """
        handles = self._routes.get(item.topic)
        if not handles:
            self.unrouted_messages += 1
            return
        for handle in tuple(handles):       # copy: attach/detach may run concurrently
            try:
                if handle._deliver(item):
                    self.messages_out += 1
            except Exception as exc:  # noqa: BLE001 - one bad handle must not stop the rest
                logger.error('orsim bus %s: delivery to %r failed: %s',
                             self.run_id, getattr(handle, '_identity', handle), exc)
        for handle in tuple(handles):
            self._schedule_pump(handle)

    def _schedule_pump(self, handle) -> None:
        """Run the handle's inbox drain OFF the dispatcher, on its own greenlet."""
        if self._patched:
            _EVENTLET.spawn_n(handle._pump)
        else:
            handle._pump()

    # ------------------------------------------------------------------
    # Subscription side: pure bookkeeping plus at most one incremental_assign
    # ------------------------------------------------------------------

    def _partitions_for(self, orsim_topic: str, lane: str) -> List[int]:
        """Which partitions of ``lane`` a subscriber of ``orsim_topic`` must read.

        The reply lane is the asymmetric one: it is keyed by the PUBLISHING
        agent's identity (so ~2.6M replies spread instead of piling onto one
        partition), which means a subscriber of the scheduler topic cannot know
        where any given reply landed and must take ALL of them.  That is fine
        because the reply lane has exactly one subscriber in the whole run -- the
        scheduler -- so its fan-out is 1.
        """
        count = self.lane_partitions[lane]
        if lane == LANE_REPLY:
            return list(range(count))
        return [partition_for(orsim_topic, count)]

    def attach_topic(self, orsim_topic: str, handle) -> None:
        """Route ``orsim_topic`` to ``handle``; assign its partition(s) if new.

        Makes ZERO blocking calls in the common case: if another co-resident
        agent already holds the partition, this is a dict update and an integer
        increment.  Only a 0 -> 1 transition touches the consumer, and then only
        with ``incremental_assign`` -- never a whole-set re-declaration, which is
        what ``check_unsubscribe_removes_exactly_one_topic`` forbids ("not even
        transiently").

        **ALL OR NOTHING.**  If the assign fails, everything this call added is
        rolled back and the exception propagates, so the caller's RETRY is a
        clean 0 -> 1 transition again.  The version without the rollback
        converted one transient ``incremental_assign`` failure into permanent,
        invisible deafness: the refcount stayed at 1 with the partition
        unassigned, so the retry was a no-op AND every other agent in the worker
        that later wanted the same partition bumped the count to 2, 3, ... and
        never triggered the 0 -> 1 assign either.  Measured: 0 of 6 expected
        broadcast deliveries, with every counter reading 0.

        REFUSES A HANDLE THAT HAS ALREADY DISCONNECTED.  ``KafkaTransport``
        rechecks its own liveness after this returns (see its ``subscribe``), so
        this guard only has to cover the straightforward case -- a subscribe
        issued on a handle that is already closed -- but both halves matter:
        without one of them a disconnect that lands mid-subscribe leaves the
        partition assigned with a refcount nobody owns for the bus's whole life.
        """
        if getattr(handle, '_closed', False):
            raise RuntimeError(
                f'orsim bus {self.run_id}: refusing to attach {orsim_topic!r} to a '
                'handle that has already disconnected; its refcount would never '
                'be released.')
        lane = lane_for(orsim_topic, self.run_id)
        kafka_topic = self.lane_topics[lane]
        wanted = self._partitions_for(orsim_topic, lane)

        with self._lock:
            handles = self._routes.setdefault(orsim_topic, [])
            appended = handle not in handles
            if appended:
                handles.append(handle)
            for part in wanted:
                key = (lane, part)
                self._partition_refs[key] = self._partition_refs.get(key, 0) + 1
        try:
            self._ensure_assigned(kafka_topic, lane, wanted)
        except Exception:
            self._rollback_attach(orsim_topic, lane, wanted, handle, appended)
            raise

    def _rollback_attach(self, orsim_topic, lane, wanted, handle, appended) -> None:
        """Undo exactly what :meth:`attach_topic` added.  Never raises."""
        with self._lock:
            for part in wanted:
                key = (lane, part)
                count = self._partition_refs.get(key, 0) - 1
                if count <= 0:
                    self._partition_refs.pop(key, None)
                else:
                    self._partition_refs[key] = count
            if appended:
                handles = self._routes.get(orsim_topic)
                if handles is not None and handle in handles:
                    handles.remove(handle)
                    if not handles:
                        self._routes.pop(orsim_topic, None)

    def _watermark_once(self, kafka_topic: str, part: int):
        """ONE ``get_watermark_offsets`` call, OFF THE HUB when patched.

        ``get_watermark_offsets`` is an uninterruptible C block.  Called
        directly from a greenlet it parks the eventlet hub for its entire
        timeout and freezes every co-resident agent -- MEASURED with a watchdog
        greenlet: 122,345 slices in the 0.5 s before the call and **zero** in
        5.0007 s of the call itself, the same total-starvation signature the
        shipped eventlet test names as disqualifying for ``poll(1.0)``.  ~17
        agents frozen for seconds miss ``STEP_SETTLE_SECONDS`` and
        ``STEP_TIMEOUT``, and a compounding freeze crosses ORSim's 30 s
        heartbeat threshold, which gets agents permanently auto-shut-down.

        ``eventlet.tpool.execute`` runs it on a real OS thread and parks only
        the CALLING greenlet, which is exactly the trade this method wants:
        the agent that is subscribing waits, everybody else keeps running.
        Unpatched (pytest, the asyncio scheduler process) there is no hub to
        protect and the call is made inline.
        """
        tp = TopicPartition(kafka_topic, part)
        if self._patched and _EVENTLET is not None:
            from eventlet import tpool
            return tpool.execute(
                self._consumer.get_watermark_offsets, tp,
                WATERMARK_QUERY_TIMEOUT_S, False)
        return self._consumer.get_watermark_offsets(
            tp, timeout=WATERMARK_QUERY_TIMEOUT_S, cached=False)

    def _log_end(self, kafka_topic: str, part: int) -> int:
        """The partition's current high watermark, or ``OFFSET_END`` if it cannot be had.

        RETRIED, because the interesting failure is transient and its
        consequence is silent.  A partition that was created moments ago exists
        in metadata before a leader is elected, and the watermark query then
        answers ``NOT_LEADER_FOR_PARTITION``.  Falling straight through to
        ``OFFSET_END`` reintroduces the asynchronous-resolution race this method
        exists to remove, and the symptom is the first few records published to
        a fresh lane never being delivered.  (``lane_admin`` also waits for
        leaders before returning, so this retry is the second line of defence,
        not the only one.)

        The 10 s budget is now spread over ~10 one-second attempts with a
        cooperative yield between them, instead of two uninterruptible five-
        second blocks -- see :data:`WATERMARK_QUERY_TIMEOUT_S` and
        :meth:`_watermark_once`.
        """
        deadline = time.monotonic() + WATERMARK_RESOLVE_TIMEOUT_S
        last = None
        while True:
            try:
                _low, high = self._watermark_once(kafka_topic, part)
                if high is not None and high >= 0:
                    return high
                last = f'high watermark came back as {high!r}'
            except Exception as exc:  # noqa: BLE001
                last = exc
            if time.monotonic() >= deadline:
                break
            self._sleep(0.1)
        logger.warning(
            'orsim bus %s: watermark for %s[%d] unresolved after %.1fs (%s); '
            'falling back to OFFSET_END, which is racy on the under-delivery side',
            self.run_id, kafka_topic, part, WATERMARK_RESOLVE_TIMEOUT_S, last)
        return OFFSET_END

    def _sleep(self, seconds: float) -> None:
        """Yield cooperatively when patched, block briefly when not."""
        if self._patched and _EVENTLET is not None:
            _EVENTLET.sleep(seconds)
        else:
            time.sleep(seconds)

    def _current_assignment(self) -> set:
        """``{(kafka_topic, partition)}`` librdkafka believes it holds RIGHT NOW.

        Local and cheap -- no broker round trip -- and it is the ground truth,
        which is why the assign path consults it instead of a shadow set that
        can drift.  It drifts in both directions in practice: a failed
        ``incremental_unassign`` is caught-and-logged, leaving a partition
        assigned while its refcount reaches 0, and that is precisely the state
        that makes the NEXT ``incremental_assign`` raise ``_CONFLICT``.
        """
        try:
            return {(tp.topic, tp.partition)
                    for tp in (self._consumer.assignment() or ())}
        except Exception as exc:  # noqa: BLE001 - fall back to "assume nothing"
            logger.debug('orsim bus %s: assignment() failed: %s', self.run_id, exc)
            return set()

    def _ensure_assigned(self, kafka_topic: str, lane: str, partitions: List[int]) -> None:
        """Assign every one of ``partitions`` at the CURRENT END of its log.

        Resolving the high watermark ourselves rather than handing librdkafka
        ``OFFSET_END`` is what makes ``subscribe()`` genuinely "in effect when it
        returns": ``OFFSET_END`` is resolved asynchronously AFTER assign returns,
        so a record produced in that gap can be appended before the watermark
        query is served and then skipped.  See FORWARD-ONLY point 1.

        IDEMPOTENT AND SERIALISED, which is what makes the refcount bump and the
        assign one transaction.  The check ("is this partition already
        assigned?") and the act ("assign it") happen under ``_assign_lock``, so
        two co-resident agents attaching the same partition concurrently cannot
        both decide to assign it -- the second finds it in
        :meth:`_current_assignment` and returns.  Without that, the second
        ``incremental_assign`` raises ``_CONFLICT``, which is a REAL and
        reachable error on this broker, not a hypothetical one.

        CORRECTION TO A CLAIM THAT USED TO LIVE HERE: this is *not* confined to
        agent boot.  The broadcast refcount is PROCESS-wide, so once the LAST
        co-resident agent naps indefinitely it reaches 0, and the first agent to
        wake pays a fresh 0 -> 1 assign -- from inside ``on_message``.  The
        watermark query no longer parks the hub (see :meth:`_watermark_once`),
        and broadcast partitions are now sticky (see :meth:`detach_topic`), so
        that path costs nothing; but the reasoning that used to justify a
        blocking call here was simply wrong.
        """
        with self._assign_lock:
            held = self._current_assignment()
            needed = [p for p in partitions if (kafka_topic, p) not in held]
            if not needed:
                return
            assignments = [
                TopicPartition(kafka_topic, part, self._log_end(kafka_topic, part))
                for part in needed
            ]
            try:
                self._consumer.incremental_assign(assignments)
            except Exception as exc:  # noqa: BLE001
                # One benign shape: librdkafka already holds the partition (a
                # ``_CONFLICT`` from a detach whose unassign silently failed).
                # Re-read the ground truth before declaring failure.
                held = self._current_assignment()
                if all((kafka_topic, part) in held for part in needed):
                    logger.warning(
                        'orsim bus %s: incremental_assign(%s[%s]) reported %s but the '
                        'partitions ARE assigned; treating as success',
                        self.run_id, kafka_topic, needed, exc)
                    return
                logger.error('orsim bus %s: incremental_assign(%s) failed: %s',
                             self.run_id, assignments, exc)
                raise

    def detach_topic(self, orsim_topic: str, handle) -> None:
        """Reverse of :meth:`attach_topic`; unassigns only on a 1 -> 0 transition.

        EXCEPT on the broadcast lane, whose ONE partition is STICKY: once any
        local handle has wanted it, this bus keeps it assigned for its own
        lifetime.  The refcount bookkeeping is unchanged (so "the partition is
        released only when the last handle detaches" still reads true in the
        table), but the consumer is not disturbed.

        Why: the broadcast refcount is process-wide, so
        ``OpenRideAgent._unsubscribe_broadcast`` drives it to 0 as soon as the
        LAST co-resident agent naps indefinitely, and the first waker then pays
        a full 0 -> 1 assign -- from inside ``on_message``, with a broker round
        trip in it.  Holding the partition removes that round trip entirely.
        The cost is that a process where every agent is asleep still FETCHES the
        step broadcast and drops it in ``_route`` (counted as
        ``unrouted_messages``), which is the same in-process filter the nap
        already relies on: the broker-side fan-out saving -- ~1063 copies down
        to 64 -- is what the nap was really buying and it is untouched.
        """
        lane = lane_for(orsim_topic, self.run_id)
        kafka_topic = self.lane_topics[lane]
        wanted = self._partitions_for(orsim_topic, lane)

        release: List[int] = []
        with self._lock:
            handles = self._routes.get(orsim_topic)
            if handles is not None:
                if handle in handles:
                    handles.remove(handle)
                if not handles:
                    self._routes.pop(orsim_topic, None)
            for part in wanted:
                key = (lane, part)
                count = self._partition_refs.get(key, 0) - 1
                if count <= 0:
                    self._partition_refs.pop(key, None)
                    release.append(part)
                else:
                    self._partition_refs[key] = count
        if lane == LANE_BCAST:
            return                      # sticky: see this method's docstring
        if release and not self._closed:
            with self._assign_lock:
                try:
                    self._consumer.incremental_unassign(
                        [TopicPartition(kafka_topic, part) for part in release])
                except Exception as exc:  # noqa: BLE001 - teardown must not raise
                    logger.warning('orsim bus %s: incremental_unassign(%s[%s]) failed: %s',
                                   self.run_id, kafka_topic, release, exc)

    def handle_is_routed(self, orsim_topic: str, handle) -> bool:
        """True when ``handle`` still holds a route (and so a refcount) for the topic.

        Exists so ``KafkaTransport.subscribe`` can undo a raced attach WITHOUT
        risking a second release: in that race ``disconnect`` runs while the
        attach is parked, and its ``drop_handle`` may already have released the
        claim.  A blind second ``detach_topic`` would decrement a refcount it
        does not own and attempt an unassign for a partition that is no longer
        assigned.
        """
        with self._lock:
            handles = self._routes.get(orsim_topic)
            return bool(handles is not None and handle in handles)

    def drop_handle(self, handle) -> None:
        """Release every route this handle still holds.  Belt-and-braces for disconnect.

        Goes through :meth:`detach_topic` rather than deleting the route entry
        directly, because the route is only HALF the bookkeeping: the partition
        refcount is the other half, and dropping the route while leaving the
        refcount standing is exactly how a partition ends up assigned for the
        bus's whole lifetime with nobody owning it.  That leak was reachable --
        an agent whose heartbeat greenlet disconnected it while its own
        ``subscribe`` was parked in the watermark query reached ``drop_handle``
        with the in-flight topic still routed and its refcount already bumped.

        Normally there is nothing left to do here: ``KafkaTransport.disconnect``
        detaches each of its topics first, so this finds an empty list.
        """
        with self._lock:
            leftovers = [topic for topic, handles in self._routes.items()
                         if handle in handles]
        for topic in leftovers:
            try:
                self.detach_topic(topic, handle)
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                logger.warning('orsim bus %s: drop_handle detach of %r failed: %s',
                               self.run_id, topic, exc)
        # Belt and braces: a topic whose lane cannot be classified (so
        # ``detach_topic`` raised) must still lose the route, or the bus keeps
        # delivering to a disconnected handle.
        with self._lock:
            for topic in list(self._routes):
                handles = self._routes[topic]
                if handle in handles:
                    handles.remove(handle)
                if not handles:
                    self._routes.pop(topic, None)

    # ------------------------------------------------------------------
    # Send side
    # ------------------------------------------------------------------

    def _on_delivery(self, err, msg) -> None:
        """The ONLY channel on which a rejected produce is ever reported.

        ``produce()`` returned normally for a ``'/'``-bearing topic against the
        live broker; the sole signal was this callback carrying
        ``KafkaError{TOPIC_EXCEPTION}``.  Ignoring it is what makes the whole
        failure silent.
        """
        if err is None:
            return
        self.delivery_failures += 1
        if self.delivery_failures <= 10 or self.delivery_failures % 1000 == 0:
            logger.error(
                'orsim bus %s: delivery FAILED on %s (%d total): %s',
                self.run_id, msg.topic() if msg is not None else '?',
                self.delivery_failures, err)

    def publish(self, orsim_topic: str, payload: bytes, routing_key: str) -> None:
        """Produce one record.  Never raises; backpressure is absorbed and counted.

        DURABILITY CONTRACT: ``publish()``'s most important call site is outside
        any ``try``, so raising here would kill an agent mid-step.  A
        ``BufferError`` (local queue full) is retried once after serving
        delivery callbacks, which is the documented remedy; anything still
        failing is counted in ``dropped_publishes`` and logged.
        """
        lane = lane_for(orsim_topic, self.run_id)
        kafka_topic = self.lane_topics[lane]
        partition = partition_for(routing_key, self.lane_partitions[lane])
        headers = [(ORSIM_TOPIC_HEADER, orsim_topic.encode('utf-8'))]

        for attempt in (0, 1):
            try:
                self._producer.produce(
                    kafka_topic,
                    value=payload,
                    partition=partition,
                    headers=headers,
                    on_delivery=self._on_delivery,
                )
                self._pending_publishes = True
                break
            except BufferError:
                if attempt == 0:
                    try:
                        self._producer.poll(0)   # serve callbacks, free queue space
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                self.dropped_publishes += 1
                logger.error(
                    'orsim bus %s: local producer queue full, DROPPED a publish to '
                    '%r (%d total)', self.run_id, orsim_topic, self.dropped_publishes)
            except KafkaException as exc:
                self.dropped_publishes += 1
                logger.error('orsim bus %s: publish to %r rejected (%d total): %s',
                             self.run_id, orsim_topic, self.dropped_publishes, exc)
            except Exception as exc:  # noqa: BLE001 - publish MUST NOT raise
                self.dropped_publishes += 1
                logger.error('orsim bus %s: publish to %r failed (%d total): %s',
                             self.run_id, orsim_topic, self.dropped_publishes, exc)
            break

        try:
            self._producer.poll(0)      # serve on_delivery callbacks
        except Exception:  # noqa: BLE001
            pass

    def flush(self, timeout: float = 2.0) -> int:
        """Flush the shared producer COOPERATIVELY.

        ``Producer.flush(timeout)`` blocks the calling OS thread inside C; under
        eventlet that is the hub, and every agent in the process stops.  So this
        polls ``flush(0)`` -- which is non-blocking and returns the number of
        messages still queued -- and yields between attempts.
        """
        if not self._pending_publishes:
            return 0
        deadline = time.monotonic() + timeout
        remaining = -1
        while True:
            try:
                remaining = self._producer.flush(0)
            except Exception as exc:  # noqa: BLE001
                logger.warning('orsim bus %s: flush failed: %s', self.run_id, exc)
                return -1
            if remaining <= 0 or time.monotonic() >= deadline:
                break
            self._sleep(0.005)
        if remaining > 0:
            # Counted as well as logged: see ``_teardown_resources``.  The same
            # records may be counted again there if the final flush cannot clear
            # them either, which is fine -- the verdict asks whether anything
            # was lost, not exactly how much.
            self.unflushed_messages += remaining
            logger.error(
                'orsim bus %s: %d message(s) still queued after a %.1fs flush; '
                'they may be lost', self.run_id, remaining, timeout)
        else:
            self._pending_publishes = False
        return max(0, remaining)


# --------------------------------------------------------------------------
# The one-per-process registry
# --------------------------------------------------------------------------

#: ``(bootstrap, run_id) -> _KafkaBus``.  ONE bus per run per OS process is the
#: whole point: ~1060 agents become 64 consumers, each demuxing to ~17 handles.
_REGISTRY: Dict[Tuple[str, str], _KafkaBus] = {}

#: Unpatched, for the same reason as ``_KafkaBus._lock``: the poll thread
#: deregisters its own bus at self-reap.
_REGISTRY_LOCK = _native_threading.Lock()


def acquire_bus(bootstrap: str, run_id: str, settings: Optional[dict] = None) -> _KafkaBus:
    """Return the process's bus for ``(bootstrap, run_id)``, retained by one handle.

    Construction happens OUTSIDE the registry lock, because it does broker I/O
    (topic creation plus a metadata confirm) and can take seconds; holding an
    unpatched lock across that would park the hub and every co-resident agent.
    The cost is that two greenlets can build a bus for the same key at the same
    time -- resolved by the second registry check below, where the loser is
    DISCARDED (never ``shutdown()``, which would deregister the winner: see
    ``_teardown_resources``) before it has started a thread.  Building a bus
    twice is idempotent (topic creation already is) and costs one discarded
    consumer/producer pair.

    The race is not theoretical: ``_KafkaBus.__init__`` yields the eventlet hub
    on the topic-creation slow path (``_confirm_partition_counts``'s
    ``time.sleep(0.25)`` is green under monkey-patching), so all ~17 agents
    booting as greenlets in one worker can enter it together.  Measured: 16
    losers, 16 pops of the winner's key, and a registry that ended EMPTY while a
    live bus served every handle.

    Retries a bounded number of times, because the bus it finds can be reaped by
    the poll thread between ``is_closed()`` and ``retain()`` -- see
    :meth:`_KafkaBus.retain`, which fails closed for exactly that window.
    """
    key = (bootstrap, run_id)
    for _attempt in range(ACQUIRE_MAX_ATTEMPTS):
        with _REGISTRY_LOCK:
            bus = _REGISTRY.get(key)
            if bus is not None and bus.is_closed():
                if _REGISTRY.get(key) is bus:
                    _REGISTRY.pop(key, None)
                bus = None
        if bus is not None:
            # retain() FAILS CLOSED: the reaper runs on a preemptive OS thread,
            # so the bus can be torn down between the is_closed() check above
            # and this call.  A False answer is treated exactly like a registry
            # miss -- loop round and build a fresh one -- instead of silently
            # binding this handle to a bus with no consumer and no poll thread.
            if bus.retain():
                return bus
            with _REGISTRY_LOCK:
                if _REGISTRY.get(key) is bus:
                    _REGISTRY.pop(key, None)
            continue

        fresh = _KafkaBus(bootstrap, run_id, settings)

        loser = None
        with _REGISTRY_LOCK:
            existing = _REGISTRY.get(key)
            if existing is not None and not existing.is_closed():
                loser, fresh = fresh, existing
            else:
                _REGISTRY[key] = fresh
        # OUTSIDE the registry lock, and that is a LOCK-ORDER rule, not a style
        # choice: retain() takes the bus's own ``_lock``, and every other path in
        # this module that holds both takes ``_lock`` first.  Nesting them the
        # other way round here would be a genuine deadlock against a bus that is
        # tearing itself down on the poll thread.  The invariant to preserve:
        # never call a bus method while holding _REGISTRY_LOCK, except
        # ``is_closed()``, which takes no lock at all.
        retained = fresh.retain()
        if loser is not None:
            # The loser was NEVER registered, so it must be torn down without
            # touching the registry.  ``_teardown_resources`` is identity-checked
            # for exactly this reason; ``discard()`` says so at the call site
            # too, because "shut down the loser" evicting the live winner is the
            # kind of thing that reads as correct right up until it is measured.
            loser.discard()
            if retained:
                return fresh
            continue
        if not retained:
            # Unreachable in practice -- a bus built two statements ago is
            # neither closed nor stopping -- but a registered-and-unstarted bus
            # would be handed to every later caller, so it is removed rather
            # than left behind.
            with _REGISTRY_LOCK:
                if _REGISTRY.get(key) is fresh:
                    _REGISTRY.pop(key, None)
            fresh.discard()
            continue
        return fresh.start()

    raise RuntimeError(
        f'could not acquire an ORSim Kafka bus for {run_id} after '
        f'{ACQUIRE_MAX_ATTEMPTS} attempts: every candidate was closed or reaped '
        'before it could be retained.')


def release_bus(bus: _KafkaBus) -> None:
    """Drop one handle's claim on ``bus``.  See :meth:`_KafkaBus.release`."""
    bus.release()


def get_bus(bootstrap: str, run_id: str) -> Optional[_KafkaBus]:
    """The live bus for a key, or ``None``.  For tests and diagnostics only."""
    return _REGISTRY.get((bootstrap, run_id))


def all_bus_stats() -> List[dict]:
    """Counters for every live bus in this process -- the operator-facing view."""
    with _REGISTRY_LOCK:
        buses = list(_REGISTRY.values())
    return [bus.stats() for bus in buses]


def transport_health(run_id: Optional[str] = None) -> dict:
    """Aggregated transport counters for this PROCESS, plus a degraded verdict.

    The one call an operator (or a future phase-3 run-finalize path) needs.
    ``degraded`` True means this process lost, dropped or failed to route
    transport traffic, which makes the run's KPIs unquotable as clean: the
    scheduler sees the affected agents as unresponsive, prunes or heartbeat-kills
    them, the run finishes with fewer orders, and ``summary.json`` still reports
    ``status: completed, ok: true, error: null`` -- indistinguishable from the
    3,247-lost-events run in CLAUDE.md 8 and the 8,032-vs-14,300-orders run in
    6.23.

    THE GAP IS CLOSED, and this is how.  This call is still per-PROCESS -- it
    can only see the buses in its own registry -- so every bus ALSO publishes
    this same counter set to a Kafka topic keyed by run id, periodically while
    it lives and once more at teardown (see
    :meth:`_KafkaBus._maybe_publish_health` and
    :mod:`apps.transport.health_stream`).  The sim's finalize path reads them
    back with ``health_stream.collect_transport_health`` and folds the verdict
    into the run's perf summary and ``summary.json``, so a run that lost
    transport traffic is no longer indistinguishable from a clean one.  The
    periodic snapshot is the load-bearing half: a bus does not tear down until
    ``BUS_LINGER_S`` after its last handle leaves, i.e. after the run has
    already finalised.
    """
    rows = [row for row in all_bus_stats()
            if run_id is None or row['run_id'] == run_id]
    totals = {name: 0 for name in DEGRADED_COUNTERS}
    for name in DIAGNOSTIC_COUNTERS:
        totals[name] = 0
    for row in rows:
        for name in totals:
            totals[name] += row.get(name, 0)
    return {
        'pid': os.getpid(),
        'buses': len(rows),
        'degraded': degraded_from(totals),
        'routing_blackhole': routing_blackhole(totals),
        'totals': totals,
        'per_bus': rows,
    }


def log_transport_health(reason: str = 'process exit') -> None:
    """Print every live bus's counters.  Safe to call more than once.

    Registered with :mod:`atexit` below, because for most of this class's life
    ``_log_counters`` ran ONLY from the poll thread's self-reap 30 s after the
    last handle released -- so a killed celery worker, or a process whose poll
    thread had died, printed nothing at all.
    """
    with _REGISTRY_LOCK:
        buses = list(_REGISTRY.values())
    for bus in buses:
        try:
            bus._log_counters(reason)
        except Exception:  # noqa: BLE001 - diagnostics must never raise at exit
            pass
    # ``_log_counters`` publishes each snapshot to the cross-process health
    # topic; a killed worker must not take the last one with it.
    try:
        from apps.transport.health_stream import flush_health

        flush_health(2.0)
    except Exception:  # noqa: BLE001
        pass


atexit.register(log_transport_health)


def shutdown_all_buses(join_timeout: float = 2.0) -> None:
    """Tear down every bus in this process.  Test teardown and process exit."""
    with _REGISTRY_LOCK:
        buses = list(_REGISTRY.values())
    for bus in buses:
        try:
            bus.shutdown(join_timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning('bus shutdown failed: %s', exc)
    # Identity-checked, like every other registry removal in this module: a bus
    # registered by another greenlet WHILE we were joining threads is live and
    # must not be orphaned by a blanket clear().
    with _REGISTRY_LOCK:
        for bus in buses:
            if _REGISTRY.get(bus.key) is bus:
                _REGISTRY.pop(bus.key, None)
