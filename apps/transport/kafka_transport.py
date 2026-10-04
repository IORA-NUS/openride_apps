"""``KafkaTransport`` -- the per-agent HANDLE onto a shared process-level bus.

This class is deliberately tiny.  Every member is a few lines, because all the
machinery (the consumer, the poll thread, the dispatcher, the producer, the
partition refcounts, the counters) lives once per PROCESS in
:mod:`apps.transport.kafka_bus`.  ~1060 agents share 64 buses; a handle holds
only a bus reference, its own topic set, its own FIFO inbox, and its callback.

WHY IT SUBCLASSES THE ABC INSTEAD OF ``MessengerTransport.register(cls)``
------------------------------------------------------------------------
``register()`` makes ``isinstance`` answer True with NO ``__abstractmethods__``
check whatever, so a virtually-registered transport can pass
``Messenger.__init__``'s guard while missing ``publish`` entirely -- and the
resulting ``AttributeError`` surfaces inside a receive callback where
``except Exception: logging.warning; return`` eats it.  Subclassing makes the
six mandatory members a CLASS-DEFINITION-TIME check.

WHAT ``subscribe()`` COSTS -- ONE ROUND TRIP ON A 0 -> 1 PARTITION TRANSITION
-----------------------------------------------------------------------------
This section used to read "SUBSCRIPTIONS ARE PURE MEMORY -- ZERO NETWORK CALLS,
ZERO BLOCKING CALLS", and that was FALSE.  ``subscribe()`` -> ``attach_topic()``
-> ``_ensure_assigned()`` -> ``_log_end()`` resolves the partition's high
watermark from the broker, which is a real round trip: measured on a healthy
broker at median 2.49 ms / max 8.79 ms for ten fresh topics, and bounded by
``WATERMARK_RESOLVE_TIMEOUT_S`` (10 s) against a sick one.  The correct claim,
stated precisely so the next reader does not build on a false premise:

* ``consumer.subscribe()`` -- Kafka's REPLACING API -- is never called anywhere
  in this package.  THAT part was and remains true, and it is what makes the
  additive/idempotent property below hold.
* A subscribe whose partition is ALREADY assigned (another co-resident agent
  wants it, or it is the sticky broadcast partition) is pure memory.
* A subscribe that is the first local claim on a partition makes one watermark
  query.  Under eventlet that query runs in ``eventlet.tpool``, so it parks the
  CALLING greenlet only -- not the hub, and not the other ~17 agents in the
  worker.  Before that, it parked everything: a watchdog greenlet measured
  122,345 slices in the 0.5 s before the call and ZERO during it.

That cost is why the ABC's hardest pairing still resolves, rather than why it
never arose:

* LIFECYCLE point 4 -- "must be in effect when it returns".  Existence of the
  lane topics was established once, at bus construction, and the partition is
  assigned before this call returns, at an offset resolved here rather than by
  librdkafka after the fact.
* CONCURRENCY point 1 -- "re-entered from inside your own ``on_message``".
  ``OpenRideAgent._unsubscribe_broadcast`` / ``._resubscribe_broadcast`` and the
  step reply all run with ``on_receive_message`` still on the stack.  Since
  nothing here waits on the receive loop, the deadlock the ABC describes -- and
  that a phase-2 draft actually hit for a full 10 s timeout -- cannot form.

ADDITIVE AND IDEMPOTENT, which is the nap/wake correctness property.  ``_topics``
is a set added to and discarded from ONE ELEMENT AT A TIME; the whole set is
never re-declared.  The sequence that destroys a replacing transport --
``unsubscribe(broadcast)`` during a nap, then ``subscribe(broadcast)`` ALONE on
wake -- leaves ``{app_topic, broadcast}`` intact here, because the app topic was
never mentioned.  Against a replacing transport the agent's own app topic is
silently lost and, since the wake path is itself driven by an app-topic event,
the agent can never be woken again for the rest of the run.

A SIDE EFFECT WORTH RECORDING, because it looks like a loss and is a win: the
nap exists partly so RabbitMQ stops fanning the per-step broadcast out to that
agent.  Under Kafka the record still reaches the PROCESS and is dropped by the
in-process filter, so that one agent's network saving disappears -- but the
broker-side fan-out for the broadcast drops from ~1063 copies to 64
unconditionally, which is far larger than what the nap was recovering.  The CPU
saving of the nap (not running the step handler) is untouched.  The bus takes
this one step further and keeps the broadcast partition assigned for its own
lifetime once any local handle has wanted it (``_KafkaBus.detach_topic``), so
the first agent to WAKE does not pay an assign either.

BUFFERING BEFORE ``on_message`` IS ASSIGNED
-------------------------------------------
``ORSimAgent.start_listening`` subscribes FIRST and assigns the callback second,
on an already-running loop.  There is ONE ordered ``_inbox`` deque per handle:
``_deliver`` always appends to it, and ``_pump`` delivers from it only while a
callback is assigned.  Because there is one queue rather than a separate
"pending" list, no inversion between buffered and newly-arrived messages is
possible.  The ``on_message`` SETTER pumps, so buffered messages are dispatched
in order on the assigning greenlet.  (``check_on_message_unset_buffers``, STRICT
-- ``PahoTransport`` fails it on purpose; a transport whose subscribe costs a
round trip must not.)

``on_message`` IS A READ-THROUGH PROPERTY, AND THAT IS LOAD-BEARING.  The ABC
declares ``on_message = None`` as a CLASS attribute, which would shadow any
``__getattr__``; a transport that stores the callback elsewhere reads back
``None`` inside its own receive loop and drops every inbound message with
nothing raised (``check_on_message_reads_back``).
"""

import logging
import weakref
from collections import deque
from typing import List, Tuple, Union

from orsim.messenger.transport import MessengerTransport

from apps.transport.kafka_bus import DEFAULT_INBOX_MAX, acquire_bus
from apps.transport.lane_topics import LANE_REPLY, lane_for

logger = logging.getLogger(__name__)


def _release_abandoned_bus(bus, identity: str) -> None:
    """Return a handle's bus claim when the handle was dropped without disconnect.

    Module-level and holding only the BUS, never the handle: a finalizer that
    closed over the handle would keep it alive and never fire.
    """
    try:
        # A DISTINCT marker on purpose.  ``ORSIM TRANSPORT DEGRADED`` means
        # "this run lost traffic"; this means "a handle leaked its bus claim",
        # which is a different question with a different owner (the scheduler's
        # boot-deadline guard reports the run-level consequence).  Mixing them
        # would dilute the one signal an operator greps for -- the mistake that
        # putting ``unrouted_messages`` in DEGRADED_COUNTERS already made once.
        logger.error(
            'ORSIM TRANSPORT LEAK: %s was garbage-collected without '
            'disconnect(); releasing its claim on bus %s so the bus can reap '
            'itself. This means an agent boot raised past its own teardown.',
            identity, getattr(bus, 'run_id', '?'))
        bus.release()
    except Exception:  # noqa: BLE001 - runs at GC and at interpreter exit
        pass


def _normalize(topic: Union[str, List[Tuple[str, int]]]) -> List[str]:
    """Accept BOTH live call shapes and return a list of topic strings.

    ``Messenger.__init__`` and ``OpenRideAgent._resubscribe_broadcast`` pass a
    ``str``; ``ORSimAgent.start_listening`` passes a bare list of
    ``(topic, qos)`` tuples.  A transport that handles only one of them
    subscribes to ``str(list)`` -- a topic nothing publishes to -- and receives
    nothing, silently (``check_subscribe_shapes``).

    qos is discarded: Kafka has no equivalent, and there is no broker call below
    this point for the shape to reach.  Normalisation happens HERE and nowhere
    lower, so exactly one function knows about the two shapes.
    """
    if isinstance(topic, str):
        return [topic]
    out = []
    for entry in topic or ():
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, (tuple, list)) and entry:
            out.append(entry[0])
        else:  # pragma: no cover - no call site produces this
            raise TypeError(f'unsupported subscribe() topic entry: {entry!r}')
    return out


class KafkaTransport(MessengerTransport):
    """One agent's (or one scheduler's) view of the process-level Kafka bus.

    Args:
        settings (dict): the ``messenger_settings`` dict ``ORSimEnv.set_backend``
            holds -- needs ``KAFKA_BOOTSTRAP_SERVERS``; optionally
            ``KAFKA_APP_PARTITIONS`` / ``KAFKA_REPLY_PARTITIONS`` /
            ``KAFKA_LINGER_MS`` / ``KAFKA_INBOX_MAX``.
        run_id (str): scopes the lane topics AND the lane classification (see
            ``lane_topics.lane_for``, which requires ``parts[0] == run_id``).
        identity (str): this transport's own ``unique_id``.  It is the ROUTING
            KEY for reply-lane publishes, which is what spreads ~2.6M replies
            across the reply partitions while keeping each agent's own replies
            in order.

    Connected by the end of ``__init__``, as LIFECYCLE point 1 requires:
    :func:`~apps.transport.kafka_bus.acquire_bus` creates or confirms the lane
    topics and raises if it cannot, and that propagates out of
    ``Messenger.__init__`` exactly as today's ``ConnectionRefusedError`` does.
    """

    def __init__(self, settings: dict, run_id: str, identity: str):
        settings = settings or {}
        bootstrap = settings.get('KAFKA_BOOTSTRAP_SERVERS')
        if not bootstrap:
            raise ValueError(
                'KafkaTransport needs KAFKA_BOOTSTRAP_SERVERS in messenger_settings; '
                'apps/config.py stamps it onto messenger_backend so it rides the '
                'celery task payload rather than being re-read from os.environ.')

        # Every field before acquire_bus(), because the on_message SETTER pumps
        # and the bus can deliver the instant a topic is attached.
        self._identity = identity
        self._run_id = run_id
        self._topics = set()
        self._inbox = deque()
        self._inbox_max = int(settings.get('KAFKA_INBOX_MAX', DEFAULT_INBOX_MAX))
        self._pumping = False
        self._loop_running = False
        self._closed = False
        self._published = False
        self.__dict__['_on_message'] = None
        self._bootstrap = bootstrap
        self._settings = settings
        self._rebinds = 0

        self._bus = acquire_bus(bootstrap, run_id, settings)

        # LAST-RESORT RELEASE.  ``disconnect()`` is the real teardown and it
        # detaches this finalizer; this exists for the one path that skips
        # disconnect entirely -- an exception escaping agent boot.
        # ``ORSimAgent.start_listening`` calls ``subscribe`` unguarded and
        # spawns its teardown heartbeat greenlet only afterwards, so a raise
        # there (``incremental_assign`` can fail, and the docstring in
        # ``kafka_bus._ensure_assigned`` calls ``_CONFLICT`` reachable rather
        # than hypothetical) left the handle's ``retain()`` standing forever:
        # the bus could never reach its ``BUS_LINGER_S`` self-reap, so one extra
        # ``Consumer`` plus one native poll thread stayed pinned in a
        # long-lived celery worker for every affected agent, still assigned to
        # lane topics that are reaped after 6 h.  MEASURED: five handles created
        # and dropped without ``disconnect()`` left ``handles: 5`` and
        # ``_reap_at: None``.  ``apps.container_logistics.openride_agent`` also
        # guards the boot path itself; this covers the agent classes that do not
        # inherit from it.
        self._release_on_gc = weakref.finalize(
            self, _release_abandoned_bus, self._bus, identity)

    # ------------------------------------------------------------------
    # Bus liveness: the handle's own recovery path
    # ------------------------------------------------------------------

    def _ensure_live_bus(self) -> bool:
        """Re-bind onto a fresh bus if this handle's bus has died.  Never raises.

        ``acquire_bus`` only ever protected a NEW handle: it drops a closed bus
        from the registry and builds a replacement, so an agent booting after
        the death gets a working bus.  The handles ALREADY bound to the corpse
        were never rebuilt and never told, and in a real run no agent
        re-acquires a bus after boot -- so there was no in-process recovery at
        all.  REPRODUCED: with the poll loop ended, ``is_closed()`` correctly
        answered True and a new handle did get a fresh bus, while the three
        original handles still had ``loop_is_running True``, ``_closed False``,
        ``subscribe`` and ``publish`` both returning ``(0, 0)``, and received
        none of the broadcasts the healthy bus delivered.  ~17 agents go silent,
        the scheduler prunes them, the run reports ok: true.

        ONE rebind per death, and it is counted on the bus that will actually be
        logged.  ``bus_rebinds`` is in ``DEGRADED_COUNTERS``, so a process that
        had to do this cannot report clean -- incrementing it on the corpse
        would be invisible, because the corpse already printed its counters.
        """
        if self._closed:
            return False
        bus = self._bus
        try:
            if not bus.is_closed():
                return True
        except Exception:  # noqa: BLE001 - treat an unanswerable bus as dead
            pass

        topics = sorted(self._topics)
        logger.error(
            'ORSIM TRANSPORT DEGRADED: %s: its bus for run %s is DEAD; '
            'rebinding onto a fresh bus and re-attaching %s',
            self._identity, self._run_id, topics)
        try:
            fresh = acquire_bus(self._bootstrap, self._run_id, self._settings)
        except Exception as exc:  # noqa: BLE001 - a publish must never raise
            logger.error('%s: could not rebind onto a fresh bus: %s',
                         self._identity, exc)
            return False

        self._bus = fresh
        self._rebinds += 1
        fresh.bus_rebinds += 1
        try:
            bus.release()       # tidy: the corpse's refcount is nobody's now
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, '_release_on_gc', None) is not None:
            self._release_on_gc.detach()
        self._release_on_gc = weakref.finalize(
            self, _release_abandoned_bus, fresh, self._identity)

        self._topics = set()
        for name in topics:
            try:
                fresh.attach_topic(name, self)
                self._topics.add(name)
            except Exception as exc:  # noqa: BLE001
                logger.error('%s: re-attach of %r after a bus rebind failed: %s',
                             self._identity, name, exc)
        return True

    # ------------------------------------------------------------------
    # on_message: read-through property.  See the module docstring.
    # ------------------------------------------------------------------

    @property
    def on_message(self):
        """The callback last assigned, or ``None``.

        Backed by ``self.__dict__['_on_message']`` and read THROUGH, because the
        ABC's ``on_message = None`` class attribute would otherwise shadow any
        other arrangement and make every read inside the bus answer ``None``.
        """
        return self.__dict__.get('_on_message')

    @on_message.setter
    def on_message(self, value):
        self.__dict__['_on_message'] = value
        # Dispatch anything that arrived during the subscribe -> assign window,
        # in order, on the assigning greenlet.
        self._pump()

    # ------------------------------------------------------------------
    # The six mandatory members
    # ------------------------------------------------------------------

    def loop_start(self, **_kwargs):
        """Begin delivering to this handle.  The bus is already polling.

        There is no per-handle thread to start -- that is the design, not an
        omission -- so this is a flag plus a drain of anything buffered while
        the handle was quiet.  A ``loop_start`` after ``disconnect`` is refused:
        the handle's subscriptions and its claim on the bus are already gone, so
        "running" would be a lie that only shows up in ``loop_is_running``.
        """
        if self._closed:
            return None
        self._loop_running = True
        self._pump()
        return None

    def loop_stop(self, force: bool = False, **_kwargs):
        """Stop delivering to this handle.  Idempotent; safe before or after disconnect."""
        self._loop_running = False
        return None

    def subscribe(self, topic: Union[str, List[Tuple[str, int]]], qos: int = 0, **_kwargs):
        """ADD topics.  In effect on return.  See the module docstring for the cost.

        Re-subscribing to a topic already held is a NO-OP, not a second
        delivery: every ORSim handler is written for exactly-one delivery, and a
        duplicated step broadcast makes an agent reply twice for one time step.

        **THE BUS IS TOLD FIRST; ``_topics`` RECORDS ONLY WHAT SUCCEEDED.**  The
        other order -- add to ``_topics``, then attach -- turns every RETRY of a
        failed subscribe into a silent no-op, because the ``already held`` guard
        above matches.  ``OpenRideAgent._resubscribe_broadcast`` catches, logs
        and retries exactly like that, and it sets ``_broadcast_subscribed =
        True`` on the retry that did nothing.  Measured consequence of one
        transient ``incremental_assign`` failure under the old order: the handle
        believed it was subscribed, the routing table said so too, the
        consumer's assignment was ``[]``, 0 of 6 expected broadcasts arrived,
        and every counter -- ``messages_in``, ``delivery_failures``,
        ``dropped_publishes``, ``consume_errors``, ``unrouted_messages`` -- read
        zero.  Deafness that no log line and no counter can see is the exact
        failure shape this package exists to avoid.

        Raising is therefore correct and deliberate: a failure the caller can
        retry beats a lie it cannot detect.  ``attach_topic`` rolls its own
        partition refcount and route entry back before the exception leaves, so
        the retry is a clean first claim rather than an inherited leak.

        A SUBSCRIBE THAT RACES ITS OWN HANDLE'S DISCONNECT UNDOES ITSELF.  The
        watermark query inside ``attach_topic`` runs in ``eventlet.tpool``,
        which PARKS the calling greenlet -- so another greenlet runs between
        ``attach_topic``'s bookkeeping (route added, partition refcount bumped)
        and the ``incremental_assign`` that finishes it.  The agent's own
        heartbeat greenlet lands exactly there: ``handle_heartbeat_failure`` ->
        ``stop_listening`` -> ``Messenger.disconnect`` ->
        :meth:`disconnect`, which iterates ``list(self._topics)`` -- and this
        topic is not in it yet, because it is added only after ``attach_topic``
        returns.  So no ``detach_topic`` ran for it and the partition was left
        assigned with a refcount nobody owned, for the bus's whole lifetime.
        REPRODUCED under monkey-patching: ``_closed=True`` with
        ``_topics=['{run}/agent_a']``, ``bus._routes={}``,
        ``bus._partition_refs={('app', 6): 1}`` and the partition still in the
        consumer's assignment.  Re-checking ``self._closed`` after the attach
        returns and detaching if it lost the race closes the window from the
        side that actually knows.
        """
        if self._closed:
            logger.warning('%s: subscribe to %r after disconnect, ignored',
                           self._identity, topic)
            return (0, 0)
        self._ensure_live_bus()
        for name in _normalize(topic):
            if name in self._topics:
                continue
            self._bus.attach_topic(name, self)
            if self._closed:
                # Lost the race against our own disconnect while parked in the
                # watermark query. Give back exactly what the attach took --
                # and only if the disconnect's own ``drop_handle`` has not
                # already taken it back, or this would release a refcount the
                # handle no longer owns.
                try:
                    if self._bus.handle_is_routed(name, self):
                        self._bus.detach_topic(name, self)
                except Exception as exc:  # noqa: BLE001 - teardown must not raise
                    logger.warning('%s: undo of a raced subscribe to %r failed: %s',
                                   self._identity, name, exc)
                return (0, 0)
            self._topics.add(name)
        self._pump()
        return (0, 0)   # paho-shaped; the return value is read by nobody

    def unsubscribe(self, topic: str, **_kwargs):
        """Remove exactly ONE topic, disturbing no other -- not even transiently.

        The bus drops a partition only on a 1 -> 0 refcount transition and does
        it with ``incremental_unassign``, so no other partition's read position
        is touched and no rebalance occurs.
        """
        for name in _normalize(topic):
            if name not in self._topics:
                continue
            self._topics.discard(name)
            self._bus.detach_topic(name, self)
        return (0, 0)

    def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False, **_kwargs):
        """Produce one record.  NEVER raises -- see the bus's DURABILITY note.

        The routing key is the topic for the app and broadcast lanes, and THIS
        TRANSPORT'S OWN IDENTITY for the reply lane.  Every reply in the run goes
        to the single string ``{run_id}/{sched}/ORSimScheduler``, so keying on
        the topic would pin ~2.6M records onto one partition; keying on the
        agent spreads them and still guarantees that one agent's step-T and
        step-T+1 replies share a partition, so the barrier can never see them
        inverted.
        """
        if self._closed:
            self._bus.dropped_publishes += 1
            logger.warning('%s: publish to %r after disconnect, dropped',
                           self._identity, topic)
            return (0, 0)

        if payload is None:
            body = b''
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            body = bytes(payload)
        else:
            # PAYLOAD CONTRACT: all 12 call sites hand in json.dumps(...) output
            # and both handlers do payload.decode('utf-8'), so the round trip is
            # ASYMMETRIC -- str in, bytes out.
            body = str(payload).encode('utf-8')

        # Every agent publishes its reply every step, so this is where a dead
        # bus is noticed in a real run -- see ``_ensure_live_bus``.
        self._ensure_live_bus()

        key = self._identity if lane_for(topic, self._run_id) == LANE_REPLY else topic
        self._bus.publish(topic, body, key)
        self._published = True
        return (0, 0)

    def disconnect(self, **_kwargs):
        """An agent's ENTIRE teardown.  Idempotent, in either order with ``loop_stop``.

        ``Messenger.disconnect`` calls ``loop_stop`` only ``if self.channel_id is
        not None`` and agents build their Messenger with ``channel_id=None``, so
        for every agent in every run this is the only teardown call that ever
        arrives.  It therefore has to do all four things: stop delivery, release
        the subscriptions (and with them any partition this handle was the last
        user of), FLUSH what it already accepted, and drop its claim on the bus.

        The flush is not optional and not theoretical:
        ``OpenRideAgent.start_listening`` publishes the boot reply and, for a
        failed agent, immediately tears down.  With librdkafka's default 5 ms
        linger that reply is still in the local queue and is PURGED on destroy;
        the scheduler then never records the agent and burns ``STEP_TIMEOUT``
        waiting for it.
        """
        if self._closed:
            return None
        self._closed = True
        self._loop_running = False

        for name in list(self._topics):
            try:
                self._bus.detach_topic(name, self)
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                logger.warning('%s: detach of %r during disconnect failed: %s',
                               self._identity, name, exc)
        self._topics.clear()
        self._bus.drop_handle(self)
        self._inbox.clear()

        if self._published:
            self._bus.flush()
        self._bus.release()
        # The GC fallback exists only for a handle that never got here.
        finalizer = getattr(self, '_release_on_gc', None)
        if finalizer is not None:
            finalizer.detach()
        return None

    # ------------------------------------------------------------------
    # Receive side: called by the bus, never by the framework
    # ------------------------------------------------------------------

    def _deliver(self, message) -> bool:
        """Append to this handle's ordered inbox.  Called by the bus's dispatcher.

        Bounded: an agent whose greenlet is wedged must not grow this without
        limit.  Dropping the OLDEST is the lesser evil -- the newest message is
        the one the barrier is waiting on -- and it is counted and logged.

        Returns True only when the message was enqueued WITHOUT evicting
        another, which is what the bus's ``messages_out`` counts.  It used to
        count every call that did not raise, including the eviction path, so
        ``messages_out`` could not be reconciled against the deliveries the
        agent actually saw.
        """
        if self._closed:
            return False
        if len(self._inbox) >= self._inbox_max:
            self._bus.inbox_overflow += 1
            self._inbox.popleft()
            if self._bus.inbox_overflow <= 5 or self._bus.inbox_overflow % 1000 == 0:
                logger.error(
                    '%s: inbox full at %d, dropped the OLDEST message (%d total). '
                    'This agent is not draining -- suspect a wedged greenlet.',
                    self._identity, self._inbox_max, self._bus.inbox_overflow)
            self._inbox.append(message)
            return False
        self._inbox.append(message)
        return True

    def _pump(self) -> None:
        """Drain this handle's inbox in order, on ONE greenlet at a time.

        Spawned per handle by the bus's dispatcher, and also called directly by
        ``subscribe``, ``loop_start`` and the ``on_message`` setter.  The
        ``_pumping`` flag makes a re-entrant call a no-op rather than a nested
        drain: ``on_message`` re-enters ``subscribe()``, which calls this, with
        ``on_receive_message`` still on the stack.  The outer loop then picks up
        whatever was appended, so per-agent FIFO stays exact.

        A handler that raises is logged and skipped.  Letting it escape would
        kill the pumping greenlet and, in the unpatched case, the bus's poll
        thread -- the "agent silently deaf for the rest of the run" failure the
        ABC warns about.
        """
        if self._pumping:
            return
        self._pumping = True
        try:
            while self._inbox and self._loop_running:
                callback = self.on_message
                if callback is None:
                    return          # buffer, do not drop: CONCURRENCY point 2
                message = self._inbox.popleft()
                try:
                    callback(self, None, message)
                except Exception as exc:  # noqa: BLE001
                    # COUNTED, not only logged.  A handler that raises on every
                    # record (a truncated payload makes ORSim's own
                    # ``json.loads`` throw inside the callback) leaves the agent
                    # permanently silent: the scheduler counts it unresponsive
                    # every step and eventually heartbeat-kills it, which is the
                    # 15-killed-facility-agents shape of CLAUDE.md 6.23 -- while
                    # every transport counter read clean.
                    self._bus.handler_errors += 1
                    logger.error('%s: on_message raised for %r (%d handler errors): %s',
                                 self._identity, message.topic,
                                 self._bus.handler_errors, exc, exc_info=True)
        finally:
            self._pumping = False

    def __repr__(self):  # pragma: no cover - diagnostics only
        return (f'<KafkaTransport {self._identity} run={self._run_id} '
                f'topics={sorted(self._topics)} loop={self._loop_running}>')
