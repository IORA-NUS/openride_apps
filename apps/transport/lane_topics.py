"""The ORSim-topic <-> Kafka-topic mapping.  Pure: no I/O, no ``confluent_kafka``.

THE DECISION THIS FILE ENCODES
------------------------------
**The ORSim topic is not encoded into the Kafka topic name at all.**  It travels
VERBATIM in a Kafka record header (:data:`ORSIM_TOPIC_HEADER`), and the Kafka
topic-plus-partition is a pure ROUTING function that is deliberately many-to-one.

That inversion is the whole trick, and it is why the two constraints that make a
Kafka transport hard collapse to something a reviewer can check by reading one
screen:

*Constraint 1 -- ORSim topic names are ILLEGAL in Kafka and fail SILENTLY.*
Every framework topic contains ``'/'`` (``{run_id}/{scheduler_id}/ORSimAgent``,
``.../ORSimScheduler``, and one ``{run_id}/{agent_id}`` per agent).  Producing to
such a name against the live broker at localhost:9094 returns normally from
``produce()`` and fails only in an asynchronous delivery report
(``KafkaError{code=TOPIC_EXCEPTION, str="Broker: Invalid topic"}``).  Here no
illegal name is ever produced to, because the name on the wire is one of three
lane topics that are legal by construction.

*The TOPIC CONTRACT -- ``message.topic`` must come back BYTE-IDENTICAL.*  Both
inbound dispatchers compare by exact string equality with no ``else`` branch
(``ORSimAgent.on_receive_message`` does ``message_handlers.get(message.topic)``;
``ORSimScheduler.on_receive_message`` does ``if message.topic == f'{run_id}/...'``).
A mangling scheme has to be injective AND has to be inverted correctly for every
input the app will ever invent.  Here the function that gets inverted is
``t -> t.encode('utf-8')``.  UTF-8 encode/decode is a bijection on ``str``, so
recovery is byte-identical for every input in the grammar and for every input
OUTSIDE it -- the ride-hail legacy ``anaytics/location_stream`` shape included,
and any topic a future app invents.  A hand-written grammar-specific mangler
would be total only over the grammar it was written against.

WHY ``kafka_topic_for`` x ``partition_for`` MAY COLLIDE, AND WHY THAT IS FINE
-----------------------------------------------------------------------------
It is not injective and does not need to be, because it never participates in
recovery.  Its collisions are a BANDWIDTH property, not a correctness one, and
the reason is the dispatch predicate the bus uses:

    a handle receives a message  <=>  orsim_topic_of(msg) in handle._topics

-- exact string equality on the framework string, the identical predicate
``ORSimAgent.on_receive_message`` itself uses.  Two agents whose app topics hash
onto the same partition each see the other's records arrive at the shared bus and
each rejects them on the exact string.  Cross-delivery between agents is
therefore impossible by a *different* mechanism than injective naming: the
collision is resolved AFTER recovery instead of being prevented before encoding.
(That is also why ``check_broadcast_fanout``'s anti-cheat bystander, subscribed
to a different topic, receives nothing.)

What a collision DOES cost is bounded and known: the app lane carries roughly
200k records per 1000-truck run, and every process assigned to a colliding
partition decodes and rejects them.  That is why the app lane is hash-SHARDED
over many partitions (default 64) and a process assigns only the partitions its
own ~17 resident agents hash into (~15 of 64), rather than all of them.  Do not
"simplify" the app lane to one partition: that is a 64x amplification.

WHY THE REPLY LANE IS KEYED BY AGENT AND NOT BY TOPIC
-----------------------------------------------------
``partition_for`` is fed a ROUTING KEY, which is *not* always the topic:

* app and bcast lanes -- the routing key is the ORSim topic.
* reply lane -- the routing key is the PUBLISHING TRANSPORT'S OWN IDENTITY
  (the agent's ``unique_id``).

Every reply in the whole run goes to the single string
``{run_id}/{sched}/ORSimScheduler``, so hashing the topic would pin ~2.6M records
onto one partition.  Hashing the agent spreads them AND preserves the ordering
that matters: an agent's step-T and step-T+1 replies always land on the same
partition, so the barrier can never observe them inverted.

TWO RULES THAT LOOK LIKE NITPICKS AND ARE NOT
---------------------------------------------
1. **Never use Python's builtin ``hash()`` here.**  ``partition_for`` is computed
   independently by the publisher and by the subscriber, in DIFFERENT OS
   PROCESSES.  ``PYTHONHASHSEED`` randomisation would make them disagree, the
   record would be written to a partition nobody is assigned to, and it would
   vanish with no error anywhere -- the barrier then stalls with a clean log.
   ``tests/test_kafka_lane_topics.py`` pins a hard-coded blake2b expectation so a
   "simplification" to ``hash()`` fails loudly instead of silently.
2. **``lane_for`` requires ``parts[0] == run_id``** as well as the ``ORSim*``
   suffix.  Without that an agent that were ever *named* ``ORSimAgent`` (topic
   ``{run_id}/ORSimAgent``... or a foreign run's broadcast topic seen in a
   shared process) could be misrouted onto the broadcast lane.  The predicate is
   deliberately narrow and TOTAL: everything it does not recognise is app lane.

``n_partitions`` IS NEVER READ FROM LOCAL CONFIG
------------------------------------------------
This module computes ``partition_for(key, n)``; it never decides ``n``.  ``n``
comes from ``AdminClient.list_topics().topics[t].partitions`` at bus
construction (see :mod:`apps.transport.lane_admin`), because a stale env var on
one worker is exactly the publisher/subscriber disagreement described above.
"""

import hashlib
import re
from typing import Optional


#: The Kafka record header that carries the ORSim topic verbatim.  This string
#: is the wire contract between publisher and subscriber; changing it is a
#: breaking change for any run already in flight.
ORSIM_TOPIC_HEADER = 'orsim_topic'

#: The three lanes.  A lane is a *traffic class*, not a topic: it selects which
#: Kafka topic carries the record and how many partitions that topic has.
LANE_BCAST = 'bcast'
LANE_REPLY = 'reply'
LANE_APP = 'app'

#: Every lane, in the order :func:`apps.transport.lane_admin.ensure_run_lane_topics`
#: reports them.
LANES = (LANE_BCAST, LANE_REPLY, LANE_APP)

#: Kafka's legal topic-name alphabet.  Anything else in a run id is replaced,
#: which is why ``sanitize_run_id`` can never itself produce an illegal name.
_ILLEGAL = re.compile(r'[^a-zA-Z0-9._-]')

#: Kafka's own limit is 249 characters for the whole topic name.  The longest
#: prefix this module emits is ``'orsim_bcast_'`` (12 chars), so 180 leaves ample
#: headroom and keeps the truncation rule simple to state.
_MAX_RUN_ID_CHARS = 180


def sanitize_run_id(run_id: str) -> str:
    """Make ``run_id`` safe to embed in a Kafka topic name.

    Real run ids (``run_20260911_054140``) are already legal and pass through
    untouched -- which matters, because the reap filter in
    :mod:`apps.transport.lane_admin` keys off the ``run_`` prefix surviving.

    This is deliberately NOT injective (two run ids differing only in an illegal
    character collapse together).  It does not need to be: a run id collision
    would merely make two runs share lane topics, and the header-based dispatch
    predicate still keeps their traffic apart.  Making it injective would mean
    escaping, and an escaped name is one more thing for a reviewer to verify.

    Returns ``'unknown'`` rather than an empty string, because an empty segment
    would produce the topic name ``orsim_app_``, which Kafka accepts and which
    no reap filter would ever match.
    """
    return (_ILLEGAL.sub('_', run_id)[:_MAX_RUN_ID_CHARS]) or 'unknown'


def lane_for(orsim_topic: str, run_id: str) -> str:
    """Classify an ORSim topic into a lane.  TOTAL: never raises, always returns.

    Only the two scheduler topics of THIS run are special; everything else --
    per-agent app topics, ride-hail legacy topics, conformance topics, anything
    a future app invents -- is app lane.  See this module's docstring, rule 2,
    for why ``parts[0] == run_id`` is part of the predicate.
    """
    parts = orsim_topic.split('/')
    if len(parts) == 3 and parts[0] == run_id:
        if parts[2] == 'ORSimAgent':
            return LANE_BCAST
        if parts[2] == 'ORSimScheduler':
            return LANE_REPLY
    return LANE_APP


def kafka_topic_for(orsim_topic: str, run_id: str) -> str:
    """The legal Kafka topic name that carries ``orsim_topic`` for ``run_id``.

    Many-to-one on purpose: the whole app lane of a run shares one name.  The
    ORSim topic is recovered from the header, never from this name -- see
    :func:`orsim_topic_of`.
    """
    return f'orsim_{lane_for(orsim_topic, run_id)}_{sanitize_run_id(run_id)}'


def partition_for(routing_key: str, n_partitions: int) -> int:
    """Stable partition for ``routing_key``, identical in every OS process.

    blake2b, not ``hash()``: see this module's docstring, rule 1.  Digest size 8
    is plenty -- the result is taken modulo a partition count in the tens.
    """
    if n_partitions <= 1:
        return 0
    digest = hashlib.blake2b(routing_key.encode('utf-8'), digest_size=8).digest()
    return int.from_bytes(digest, 'big') % n_partitions


def orsim_topic_of(msg) -> Optional[str]:
    """Recover the ORSim topic from a delivered record, or ``None``.

    **This function must never look at ``msg.topic()``.**  Falling back to the
    Kafka topic name would hand ``'orsim_app_run_20260911_054140'`` to
    ``ORSimAgent.on_receive_message``, whose ``message_handlers.get()`` has no
    ``else`` branch -- so the record would be decoded, the payload cache
    updated, and the message silently discarded.  That is precisely the failure
    the TOPIC CONTRACT warns about, arriving by the back door.

    A header-less record therefore returns ``None`` and the bus DROPS it against
    a ``foreign_messages`` counter.  Header-less records are expected in normal
    operation only if something else ever writes to a lane topic; a non-zero
    counter is a signal, not noise.

    **TOTAL BY CONTRACT: this function must never raise.**  Its sole caller is
    ``_KafkaBus._on_record``, which runs on the process's ONE native poll
    thread; an exception escaping here kills that thread and every agent in the
    worker goes deaf for the rest of the run with clean counters and a clean
    log.  ``auto.create.topics.enable`` is true on this broker, so a lane topic
    is writable by anything, and a header whose bytes are not valid UTF-8 is
    exactly the input that used to raise ``UnicodeDecodeError`` out of the
    ``value.decode('utf-8')`` below.  A malformed header is FOREIGN, not fatal:
    it returns ``None`` and is counted like any other record that is not ours.
    Never decode with ``errors='replace'`` here -- a mangled topic string would
    be handed to a dispatcher that compares by exact equality, turning a drop
    into a misroute.
    """
    try:
        headers = msg.headers() or ()
    except Exception:  # noqa: BLE001 - see TOTAL BY CONTRACT above
        return None
    for entry in headers:
        try:
            key, value = entry
        except (TypeError, ValueError):
            continue
        if key != ORSIM_TOPIC_HEADER:
            continue
        if isinstance(value, (bytes, bytearray)):
            try:
                return value.decode('utf-8')
            except UnicodeDecodeError:
                return None
        return value if isinstance(value, str) else None
    return None
