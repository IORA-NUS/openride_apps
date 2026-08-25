"""One Kafka consumer, one group id, all six dataplane topics.

The sink this replaces ran four consumers in four groups in four unsupervised threads. Here
there is exactly one consumer subscribed to the six single-partition topics; routing is by
``msg.topic()`` back to a logical key, and every failure mode (bad JSON, missing key, empty
value, unknown topic, exploding handler) is *counted*, never raised. One poisoned payload
cannot end the loop.

**Offsets are committed manually, and the whole rule is four lines:**

    drain a poll batch -> the handlers write it to DuckDB -> commit that batch's offsets
    a handler raises   -> do NOT advance that partition   -> Kafka redelivers it
    the batch write raises -> roll ITS OWN topics' commit points back to the last good flush

``_note_offset`` advances a partition's commit point to ``offset + 1`` when the handler
returned cleanly and nothing below is *known* to be unwritten. A record whose handler raised
marks its partition blocked at its own offset for the life of the process, so nothing above
it can ever be committed and a restart replays it and everything after it. At-least-once
redelivery costs nothing: every store write in this package is an idempotent upsert on its
primary key.

An offset the broker skips is not an unwritten record. ``run_status`` is declared
``cleanup.policy=compact`` and keyed by ``run_id``, and every run publishes many ``RUNNING``
messages under one key, so once a segment is cleaned the surviving offsets are non-contiguous
*by construction*. Requiring the commit point to sit exactly on the delivered offset froze
that topic on the first gap — six clean terminals at compacted offsets [100, 107, …, 259]
committed 101 against a head of 260 — and every restart then replayed and re-finalized the
whole retained region. A gap is stepped over; a *failure* is what blocks.

``flush_writes`` is the one gate that lets a handler defer its write to the end of the batch
without weakening any of that: it is called from ``commit_safe``, the only place an offset can
move, so "a row a handler accepted is unwritten" and "an offset was committed" cannot both be
true. There is still no queue: a batch that cannot be written is dropped and replayed.

That replaces a 169-line offset ledger, a bounded write queue, a writer thread, a retry
backoff, a quarantine and a high/low-water pause. Durability is guaranteed by the commit
happening after the write returns — not by tracking which offsets are safe.

``trip_geo`` is the one topic whose handler makes nothing durable: truck positions are a
*sampled* view of the hot tier whose durable form is the frame series. A replay from an older
offset would re-drive the sampler and mint different ``frame_idx`` values for the same
instants, so those offsets commit on receipt. The frame series is made durable by the capture
sweep and the archive, not by these offsets.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from apps.config import kafka_config

logger = logging.getLogger(__name__)

try:  # orjson is available and faster; stdlib json is a perfectly good fallback.
    import orjson as _orjson
except Exception:  # pragma: no cover - orjson is installed in this env
    _orjson = None

try:
    from confluent_kafka import KafkaError as _KafkaError

    _PARTITION_EOF = _KafkaError._PARTITION_EOF
except Exception:  # pragma: no cover - keeps import-time safe without librdkafka
    _PARTITION_EOF = -191


# (logical_topic, run_id, payload) -> None
Handler = Callable[[str, str, dict], None]

# How often the cached partition assignment is refreshed from the client.
ASSIGNMENT_REFRESH_S = 1.0

# How often the broker lag behind that assignment is re-measured, and how long ONE partition's
# watermark lookup may block the poll thread.
#
# The lookup is a real metadata round trip (``cached=False``), issued per partition from
# ``_poll_bookkeeping`` — which runs ON the poll loop. Uncached, unthrottled, at the 0.5 s
# client default, that measured a 9.4x throughput collapse against a slow broker: 2 360 msg/s
# healthy, 250 msg/s with each lookup burning its timeout, ~19 of every 20 s spent inside the
# lookups. Two bounds fix it and neither weakens the signal: the whole refresh is issued at
# most once per interval and read from cache by ``stats()`` in between, and 50 ms is 10-50x a
# healthy localhost metadata round trip, so a lookup that exceeds it is itself evidence.
# Worst case falls from 3.0 s of blocking per second (6 partitions x 0.5 s) to 0.3 s, and the
# HEALTHY case — a broker answering in ~1 ms — costs 6 ms per second, 0.6% of the poll thread.
#
# The interval is the freshness of a fault signal, so it is deliberately far tighter than the
# 300 s window it feeds: a lag that only updates every few seconds also takes that long to
# clear, and every rule here is a function of pending data that must fall silent the moment
# the backlog drains.
#
# ``cached=True`` would cost nothing at all and is the wrong trade: librdkafka refreshes its
# cached watermarks from FETCH responses, so a dead fetcher freezes them at their last good
# value — going stale in exactly the case this signal exists to catch.
BROKER_LAG_REFRESH_S = 5.0
WATERMARK_TIMEOUT_S = 1.0
#: Minimum gap between rewinds of the same partition after a failed write.
SEEK_BACKOFF_S = 5.0

# How far the backlog must stand ABOVE the lowest point it has reached since it last fell
# before a refresh counts as "losing ground". This is a growth floor, not a level: a healthy
# live run measures a lag of 11 routinely and a poll batch is 200 records, so judging the
# magnitude directly is a false-503 generator. What separates busy from losing is that a busy
# process keeps coming back down and a losing one does not — so the level only decides
# whether a *failure to fall* is worth counting, and 1 000 records is ~7.6 s of backlog at the
# measured live rate of 132 msg/s, three orders of magnitude clear of the healthy signal.
BROKER_LAG_RISE_RECORDS = 1000

# Records handled between two flushes. A DuckDB transaction costs ~6.5 ms whatever it carries,
# so writing once per *record* caps ingest at ~141 msg/s against a measured live rate of 132,
# while once per poll batch costs 0.05 ms/record. No queue, no writer thread, no ledger: drain
# what the broker already has, write it, commit it.
POLL_BATCH = 200

# Counters that mean "a consumed record was thrown away or could not be made durable". They
# share one clock (``last_discard_age_s``) so /health can judge the *rate* of loss with one
# rule instead of carrying six body fields no verdict looks at. ``handler_errors`` is in here
# because a handler that raises is now a write that did not happen: the record is not lost
# (its offset is not committed) but the store is not taking it either, and that must be loud.
DISCARD_COUNTS = frozenset(
    {"decode_errors", "missing_key", "empty_value", "unknown_topic", "no_handler",
     "handler_errors"}
)


def _loads(raw: bytes) -> Any:
    if _orjson is not None:
        return _orjson.loads(raw)
    return json.loads(raw.decode("utf-8"))


class DataplaneConsumer:
    """Poll six topics with a single consumer and dispatch to a handler table.

    ``consumer_factory`` is the injection seam: tests pass a fake so no test ever constructs a
    real ``confluent_kafka.Consumer``. ``handle_message`` accepts anything exposing
    ``.topic()/.key()/.value()/.error()``; ``.partition()/.offset()`` are used when present
    and a message that cannot report an offset is simply untracked.
    """

    LOGICAL_TOPICS = ("run_status", "kpi", "kpi_breakdown", "trip_geo", "facility_stream", "perf")

    def __init__(
        self,
        *,
        handlers: Mapping[str, Handler],
        group_id: str = "dataplane",
        bootstrap: Optional[str] = None,
        auto_offset_reset: str = "latest",
        consumer_factory: Optional[Callable[[dict], Any]] = None,
        commit_interval_s: float = 2.0,
        flush: Optional[Callable[[], None]] = None,
        flush_topics: Iterable[str] = (),
    ) -> None:
        self.handlers: Dict[str, Handler] = dict(handlers)
        self._flush = flush
        self.group_id = group_id
        self.bootstrap = bootstrap or kafka_config["bootstrap_servers"]
        self.auto_offset_reset = auto_offset_reset
        self._consumer_factory = consumer_factory or self._default_consumer_factory
        self.commit_interval_s = float(commit_interval_s)

        topics = kafka_config.get("topics", {})
        # logical key -> broker topic name, in the pinned order.
        self._logical_to_topic: Dict[str, str] = {
            logical: topics.get(logical, logical) for logical in self.LOGICAL_TOPICS
        }
        self._topic_to_logical: Dict[str, str] = {
            broker: logical for logical, broker in self._logical_to_topic.items()
        }
        # Broker topics whose handler DEFERS its write to ``flush``. A failed flush drops the
        # rows of these topics and of no others, so it may roll back only their commit points:
        # every other handler in the batch wrote synchronously and its offset is legitimately
        # committable. Rolling the whole map back wedged all six topics — trip_geo included,
        # whose replay mints duplicate frames — for the life of the process on one transient
        # store error. Empty (nobody declared) keeps the conservative roll-back-everything.
        self._flush_topics = {self._logical_to_topic.get(t, t) for t in flush_topics}

        self._lock = threading.RLock()
        self._consumer: Any = None
        self._last_message_at: Optional[float] = None
        self._last_commit_at = 0.0
        # (topic, partition) -> next offset to commit / highest offset consumed / last
        # offset the broker accepted. Three plain dicts; there is nothing else.
        self._commit_at: Dict[Tuple[str, int], int] = {}
        self._max_seen: Dict[Tuple[str, int], int] = {}
        self._committed: Dict[Tuple[str, int], int] = {}
        # When each partition's commit point last MOVED. A point that stands still while
        # records pile up behind it is a partition wedged by a handler that raised — the one
        # fault in this class no verdict could read, because ``commit_lag`` is only judged
        # together with a silence a live run never has.
        self._commit_moved_at: Dict[Tuple[str, int], float] = {}
        # Where each commit point stood at the last flush that returned. A flush that raises
        # rolls the points back to here: everything noted since covered a row that never
        # reached the store, so the partition must freeze exactly where a per-record write
        # that raised would have frozen it.
        self._flush_floor: Dict[Tuple[str, int], int] = {}
        # Partitions holding an offset that is known NOT to be written — a handler that
        # raised, or rows a failed flush dropped. This is what freezes a commit point, and
        # it is the whole difference between "a record below is unwritten" (never step over
        # it) and "the broker skipped an offset" (compaction: nothing to write, step over).
        self._blocked: Dict[Tuple[str, int], int] = {}
        self._last_seek_at: Dict[Tuple[str, int], float] = {}
        # Records that could not report an offset (fakes, embeddings). They cannot be gated
        # per-partition, so they are committed blind.
        self._untracked = 0
        self._last_assigned = None
        # (topic, partition) -> records the BROKER still holds past our commit point, cached
        # from the poll-bookkeeping tick. See ``_refresh_broker_lag``.
        self._broker_lag: Dict[Tuple[str, int], int] = {}
        # Assigned partitions whose watermark could not be measured on the last refresh. An
        # unmeasured partition is UNKNOWN, and unknown is not zero: dropping the key made a
        # broker that cannot answer read as a broker with nothing waiting, and the failure is
        # correlated (an unreachable broker answers neither fetches nor watermarks), so the
        # one signal that can see a dead fetcher went quiet exactly when it had to fire.
        self._broker_lag_unknown = 0
        # Where a partition we have NEVER received a record on sits: the broker head we saw
        # the first time we measured it, which is where ``auto.offset.reset`` put us when we
        # joined. Without it a process that ingested nothing measured nothing — no position
        # to subtract, so no watermark call, so lag {} — which is the 2026-07-01 shape and
        # the state after every restart during an idle period.
        self._broker_lag_base: Dict[Tuple[str, int], int] = {}
        self._broker_lag_at = 0.0
        # The derivative of the backlog, which is the only part of it worth a verdict. The
        # lowest total since it last fell, how many consecutive refreshes it has since failed
        # to come back down to that floor, and the message count at the last refresh (a
        # backlog nobody is consuming is the DEAD case, which ``ingest_stalled`` owns).
        self._broker_lag_floor: Optional[int] = None
        self._broker_lag_rising = 0
        self._broker_lag_msgs = 0
        self._assigned = 0
        self._assignment_at = 0.0
        self._discards = 0
        self._last_discard_at: Optional[float] = None
        self._counts: Dict[str, int] = {
            "messages": 0,
            "handler_errors": 0,
            "decode_errors": 0,
            "kafka_errors": 0,
            "eof": 0,
            "unknown_topic": 0,
            "missing_key": 0,
            "empty_value": 0,
            "no_handler": 0,
            "commits": 0,
            "commit_errors": 0,
        }
        self._by_topic: Dict[str, int] = {logical: 0 for logical in self.LOGICAL_TOPICS}

    # ------------------------------------------------------------------ wiring

    @staticmethod
    def _default_consumer_factory(conf: dict) -> Any:
        from confluent_kafka import Consumer  # imported here so import-time never needs librdkafka

        return Consumer(conf)

    def consumer_config(self) -> dict:
        return {
            "bootstrap.servers": self.bootstrap,
            "group.id": self.group_id,
            "auto.offset.reset": self.auto_offset_reset,
            # Manual commits only: an offset must never advance past a row the store has
            # not durably accepted. See the module docstring.
            "enable.auto.commit": False,
        }

    def topic_map(self) -> Dict[str, str]:
        """Broker topic name -> logical key."""
        return dict(self._topic_to_logical)

    def broker_topics(self) -> list:
        return [self._logical_to_topic[logical] for logical in self.LOGICAL_TOPICS]

    def _ensure_consumer(self) -> Any:
        """Build, subscribe, and only THEN publish the consumer.

        Publishing before ``subscribe()`` left a half-initialised object cached when the
        subscribe raised: the supervised restart then found ``self._consumer is not None``,
        skipped the subscribe, and polled an unsubscribed consumer forever while /health
        stayed 200 (measured: 1 consumer built, 0 subscribes, 700 polls). The candidate is
        closed on failure so the next restart rebuilds cleanly.
        """
        with self._lock:
            if self._consumer is not None:
                return self._consumer
        candidate = self._consumer_factory(self.consumer_config())
        try:
            candidate.subscribe(self.broker_topics())
        except Exception:
            try:
                candidate.close()
            except Exception:  # noqa: BLE001 - a failed close must not mask the real error
                logger.debug("closing an unsubscribed consumer failed", exc_info=True)
            raise
        with self._lock:
            self._consumer = candidate
        logger.info(
            "dataplane consumer subscribed group=%s topics=%s", self.group_id, self.broker_topics()
        )
        return candidate

    # ------------------------------------------------------------------ message path

    @staticmethod
    def _decode_key(key: Any) -> Optional[str]:
        if key is None:
            return None
        if isinstance(key, (bytes, bytearray)):
            try:
                return key.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return str(key)

    def _bump(self, name: str, *, discard: bool = False) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1
            if discard or name in DISCARD_COUNTS:
                self._discards += 1
                self._last_discard_at = time.monotonic()

    def note_discard(self, reason: str) -> None:
        """A *handler* threw a consumed record away. Recorded here so one rule judges them all.

        A handler that returns without writing has its offset committed like any other, so the
        record is gone from Kafka as surely as an undecodable one. Same fault, same clock.
        """
        self._bump(reason, discard=True)

    @staticmethod
    def _coords(msg) -> Optional[Tuple[str, int, int]]:
        """``(topic, partition, offset)`` of this record, or None if it cannot report one."""
        partition = getattr(msg, "partition", None)
        offset = getattr(msg, "offset", None)
        if partition is None or offset is None:
            return None
        try:
            p, o, topic = partition(), offset(), msg.topic()
        except Exception:  # noqa: BLE001 - a client that cannot answer is simply untracked
            return None
        if p is None or o is None or topic is None:
            return None
        try:
            return (str(topic), int(p), int(o))
        except (TypeError, ValueError):
            return None

    def _note_offset(self, coords: Tuple[str, int, int], ok: bool) -> None:
        """THE offset rule. Advance only over a record whose handler wrote it.

        ``_commit_at`` starts at the first offset seen on the partition and moves to
        ``offset + 1`` when the handler wrote the record and the partition is not *blocked* —
        i.e. when nothing below it is known to be unwritten. One failure blocks the partition
        there, so the failed record and everything after it are replayed on the next start.

        Blocked, not merely "not contiguous": the broker legitimately delivers non-contiguous
        offsets on the compacted ``run_status`` topic, and a gap the broker never delivers is
        not an unwritten record. Insisting on contiguity jammed that topic on the first
        compacted segment, with nothing failing anywhere.

        A record *below* the commit point means the broker rewound us (a rebalance, or a
        replay after a failed commit), so the commit point follows it back down rather than
        refusing to move for the life of the process.
        """
        topic, partition, offset = coords
        key = (topic, partition)
        with self._lock:
            if offset > self._max_seen.get(key, -1):
                self._max_seen[key] = offset
            nxt = self._commit_at.get(key)
            if nxt is None or offset < nxt:
                nxt = self._commit_at[key] = offset
                self._commit_moved_at[key] = time.monotonic()
                if offset < self._flush_floor.get(key, offset + 1):
                    self._flush_floor[key] = offset
            if ok and (nxt == offset or key not in self._blocked):
                self._commit_at[key] = offset + 1
                self._commit_moved_at[key] = time.monotonic()
                self._blocked.pop(key, None)
            elif not ok:
                self._blocked.setdefault(key, offset)

    def handle_message(self, msg) -> bool:
        """Route one message. Returns True iff a handler ran cleanly. Never raises.

        Two outcomes are deliberately NOT the same thing, and conflating them is how a single
        undecodable record used to wedge a partition for the life of a deployment:

        * **the record was thrown away** (unknown topic, missing key, empty value, undecodable
          JSON, or a handler that returned without writing). It will be thrown away again on
          every redelivery, so holding its partition buys nothing: its offset is committed,
          it is counted as a discard, and ``ingest_discarding`` judges the rate;
        * **the handler raised.** That is a write that did not happen — a store outage, a
          payload the store cannot represent. Its offset is *not* advanced, so the record and
          everything after it on that partition are replayed on the next start.
        """
        coords = None
        try:
            err = msg.error()
            if err is not None:
                code = None
                try:
                    code = err.code()
                except Exception:  # noqa: BLE001
                    code = None
                if code == _PARTITION_EOF:
                    self._bump("eof")
                    return False
                self._bump("kafka_errors")
                logger.error("dataplane consumer error: %s", err)
                return False
            coords = self._coords(msg)
        except Exception:  # noqa: BLE001 - handle_message is contractually total
            self._bump("handler_errors")
            logger.exception("handle_message failed")
            return False

        try:
            ok = self._route(msg)
            committable = True
        except Exception:  # noqa: BLE001 - a bad payload must never end the loop
            self._bump("handler_errors")
            logger.exception("handler failed")
            ok = False
            committable = False

        if coords is None:
            with self._lock:
                self._untracked += 1
        else:
            self._note_offset(coords, committable)
        return ok

    def _route(self, msg) -> bool:
        topic = msg.topic()
        logical = self._topic_to_logical.get(topic)
        if logical is None:
            self._bump("unknown_topic")
            logger.warning("dataplane consumer got unknown topic=%r", topic)
            return False

        run_id = self._decode_key(msg.key())
        if not run_id:
            self._bump("missing_key")
            return False

        raw = msg.value()
        if not raw:
            self._bump("empty_value")
            return False

        try:
            payload = _loads(raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode())
        except Exception as exc:  # noqa: BLE001 - any decoder failure is a decode error
            self._bump("decode_errors")
            logger.warning("invalid JSON on %s for run_id=%s: %s", topic, run_id, exc)
            return False
        if not isinstance(payload, dict):
            self._bump("decode_errors")
            return False

        handler = self.handlers.get(logical)
        if handler is None:
            self._bump("no_handler")
            return False

        with self._lock:
            self._counts["messages"] += 1
            self._by_topic[logical] = self._by_topic.get(logical, 0) + 1
            self._last_message_at = time.monotonic()

        # Deliberately unguarded: a handler that raises must reach ``handle_message``, which
        # is the only place that knows not to advance this record's offset.
        handler(logical, run_id, payload)
        return True

    # ------------------------------------------------------------------ offsets

    def safe_offsets(self) -> list:
        """``[(topic, partition, next_offset_to_consume)]`` not yet accepted by the broker."""
        with self._lock:
            out = [
                (key[0], key[1], nxt)
                for key, nxt in self._commit_at.items()
                if nxt > self._committed.get(key, -1)
            ]
        out.sort()
        return out

    def flush_writes(self) -> bool:
        """Make everything the handlers buffered durable. False means: commit NOTHING.

        Handlers batch their store writes over a poll batch, and this is the single gate that
        keeps that safe: it is called from ``commit_safe``, the one place in this process where
        an offset can move, so "rows unwritten" and "offsets committed" are mutually exclusive
        by construction rather than by convention.
        """
        if self._flush is None:
            return True
        try:
            self._flush()
        except Exception:  # noqa: BLE001 - counted, rolled back, replayed on the next start
            self._bump("handler_errors")
            logger.exception("pre-commit flush failed")
            rolled: Dict[Tuple[str, int], int] = {}
            with self._lock:
                for key, nxt in list(self._commit_at.items()):
                    floor = self._flush_floor.get(key, nxt)
                    if nxt > floor and (not self._flush_topics or key[0] in self._flush_topics):
                        self._commit_at[key] = floor
                        # Those rows are gone, so this partition is blocked exactly as if
                        # each handler had written its own row and raised.
                        self._blocked.setdefault(key, floor)
                        rolled[key] = floor
            self._rewind_blocked()
            return False
        with self._lock:
            self._flush_floor = dict(self._commit_at)
        return True

    def _rewind_blocked(self) -> None:
        """Rewind every blocked partition so the broker redelivers what never reached the store.

        A partition gets blocked two ways — a handler that raised, and a pre-commit flush that
        failed — and BOTH leave the same state: the commit point sits at the last durable
        offset while the consumer has already read past those records. Nothing redelivers
        them, so the partition stayed blocked for the life of the process and only a restart
        recovered it. Measured 2026-08-07 (adv9 d2): after the store healed in place, 50 of
        450 rows were durable and `blocked=['kpi','run_status']` never cleared.

        Rewinding here rather than at each block site keeps one mechanism for both causes, and
        makes the broker the retry buffer — which is the whole reason nothing is committed
        before its data is durable.
        """
        with self._lock:
            blocked = dict(self._blocked)
        self._seek_back(blocked)

    def _seek_back(self, rolled: Dict[Tuple[str, int], int]) -> None:
        """Rewind the consumer to the offsets whose rows were lost. Best effort, never raises.

        Throttled per partition: a store that is down for good would otherwise turn every poll
        into read-fail-seek at full speed. One rewind per ``SEEK_BACKOFF_S`` per partition is
        enough — the records are not going anywhere.
        """
        if not rolled:
            return
        consumer = self._consumer
        if consumer is None or not hasattr(consumer, "seek"):
            return
        from confluent_kafka import TopicPartition  # local: import time stays librdkafka-free

        now = time.monotonic()
        for (topic, partition), floor in rolled.items():
            last = self._last_seek_at.get((topic, partition), 0.0)
            if now - last < SEEK_BACKOFF_S:
                continue
            try:
                consumer.seek(TopicPartition(topic, partition, floor))
                self._last_seek_at[(topic, partition)] = now
                self._bump("seek_back")
                logger.warning(
                    "rewound %s[%d] to offset %d after a failed write; the broker will "
                    "redeliver those records", topic, partition, floor,
                )
            except Exception:  # noqa: BLE001 - an unassigned partition cannot be sought
                logger.debug("seek back failed for %s[%d]", topic, partition, exc_info=True)

    def blocked_topics(self) -> set:
        """Logical topics holding an offset known NOT to be written. See ``_note_offset``.

        A blocked partition means "rows below this point never reached the store, and only a
        replay can recover them". That is exactly the question the terminal path has to ask
        before it lets a run's durable record be written as complete: ``flush_writes``
        answers "did THIS batch land", and this answers "is anything still missing".
        """
        with self._lock:
            keys = list(self._blocked)
        return {self._topic_to_logical.get(key[0], key[0]) for key in keys}

    def rewind_if_blocked(self) -> None:
        """Public hook: retry blocked partitions. Called from the poll loop's commit tick."""
        self._rewind_blocked()

    def commit_safe(self, *, force: bool = False) -> bool:
        """Commit exactly the offsets whose data is durable. True iff a commit was issued.

        Never raises: a commit failure is counted and retried on the next tick (at worst a
        message is replayed, and every writer in this package is idempotent against that).
        """
        if not self.flush_writes():
            return False
        with self._lock:
            consumer = self._consumer
            if consumer is None:
                return False
            if not force and (time.monotonic() - self._last_commit_at) < self.commit_interval_s:
                return False
            untracked = self._untracked
        commit = getattr(consumer, "commit", None)
        if commit is None:
            with self._lock:
                self._last_commit_at = time.monotonic()
            return False

        offsets = self.safe_offsets()
        if not offsets:
            # Nothing trackable to commit. Fall back to a blind commit only when this
            # consumer never managed to track anything at all.
            if untracked <= 0:
                return False
            return self._commit_blind(commit)

        from confluent_kafka import TopicPartition  # import here: import time stays librdkafka-free

        try:
            commit(offsets=[TopicPartition(t, p, o) for t, p, o in offsets], asynchronous=False)
        except Exception:  # noqa: BLE001 - includes "no offsets to commit" on an idle group
            self._bump("commit_errors")
            logger.debug("offset commit failed", exc_info=True)
            with self._lock:
                self._last_commit_at = time.monotonic()
            return False
        with self._lock:
            for topic, partition, offset in offsets:
                self._committed[(topic, partition)] = int(offset)
            self._last_commit_at = time.monotonic()
            self._counts["commits"] += 1
            self._untracked = 0
        return True

    def _commit_blind(self, commit) -> bool:
        try:
            commit(asynchronous=False)
        except Exception:  # noqa: BLE001
            self._bump("commit_errors")
            logger.debug("offset commit failed", exc_info=True)
            with self._lock:
                self._last_commit_at = time.monotonic()
            return False
        with self._lock:
            self._last_commit_at = time.monotonic()
            self._counts["commits"] += 1
            self._untracked = 0
        return True

    # ------------------------------------------------------------------ loop

    @staticmethod
    def _partition_keys(assignment) -> set:
        """``{(topic, partition)}`` out of whatever ``assignment()`` returned."""
        keys = set()
        for tp in assignment:
            topic = getattr(tp, "topic", None)
            partition = getattr(tp, "partition", 0)
            if topic is None and isinstance(tp, (tuple, list)) and len(tp) == 2:
                topic, partition = tp
            if topic is None:
                continue
            try:
                keys.add((str(topic), int(partition or 0)))
            except (TypeError, ValueError):  # a client that cannot name its own partitions
                continue
        return keys

    def refresh_broker_lag(self) -> None:
        """Refresh the broker-lag cache. Called from the reconcile task, never the poll loop."""
        consumer = self._consumer
        if consumer is None:
            return
        self._refresh_broker_lag(consumer, getattr(self, "_last_assigned", None))

    def _refresh_broker_lag(self, consumer, assigned=None) -> None:
        """How many records the BROKER still holds past our commit point. Never raises.

        This is the one fact the process was missing, and it separates two states that are
        byte-identical in memory — silence, an open run in the store, zero commit lag:

        * ``lag > 0`` while nothing is being consumed: the records exist and are not being
          fetched. Ingest is dead, and no in-process counter can say so — a cleanly dead
          fetcher produces no handler error, no discard and no commit lag at all;
        * ``lag == 0``: there is nothing to consume. Idle, however long it lasts, which is
          what OpenRide is between runs and after a sim is killed mid-flight.

        It is also the only measurement of consumer lag against the broker in the package: a
        process that is merely *slow* has every in-process counter at zero while the backlog
        climbs, so ``broker_lag`` is the only number in the health body that can show it.

        One ``get_watermark_offsets`` per ASSIGNED partition, at most once every
        ``BROKER_LAG_REFRESH_S`` and bounded by ``WATERMARK_TIMEOUT_S`` each, cached for
        ``stats()``. Three properties are load-bearing and each was wrong once:

        * the set is the **assignment**, not the partitions already consumed from. Derived
          from the latter, a process that ingested nothing measured nothing;
        * a lookup that fails is **UNKNOWN**, never absent: a dropped key summed to zero and
          read as "nothing waiting", and the failure is correlated — the broker that stopped
          answering fetches stopped answering watermarks too;
        * it is **cached and bounded**, because it runs on the poll thread.

        Known limit: a partition never consumed from is measured against the head as it stood
        the first time we looked, which is where ``auto.offset.reset=latest`` put us on
        joining. Records the broker retained from before this process started are therefore
        not counted as lag — they are not ours to consume.
        """
        now = time.monotonic()
        if (now - self._broker_lag_at) < BROKER_LAG_REFRESH_S:
            return  # the cached value is younger than the window it feeds
        watermarks = getattr(consumer, "get_watermark_offsets", None)
        if watermarks is None:
            return
        try:
            from confluent_kafka import TopicPartition
        except Exception:  # noqa: BLE001 - import time must stay librdkafka-free
            return
        self._broker_lag_at = now
        with self._lock:
            # The ASSIGNMENT is the set that matters — a partition this process has never
            # received a record on is exactly the one that can be silently starved — plus
            # anything tracked, which covers a client that cannot report its assignment.
            keys = set(self._commit_at)
            if assigned:
                keys |= assigned
            # Where the broker thinks we are: the last offset it accepted, or the point we
            # are about to commit for a partition that has not committed yet.
            positions = {key: self._committed.get(key, self._commit_at.get(key)) for key in keys}
            previous = dict(self._broker_lag)
            baseline = dict(self._broker_lag_base)
        lag: Dict[Tuple[str, int], int] = {}
        bases: Dict[Tuple[str, int], int] = {}
        unknown = 0
        for key, position in sorted(positions.items()):
            try:
                _low, high = watermarks(
                    TopicPartition(key[0], key[1]), timeout=WATERMARK_TIMEOUT_S
                )
                high = int(high)
            except Exception:  # noqa: BLE001 - counted as UNKNOWN, never as zero
                unknown += 1
                if key in previous:
                    lag[key] = previous[key]  # the last number anyone actually measured
                logger.debug("watermark lookup failed for %s", key, exc_info=True)
                continue
            if position is None:
                # Assigned and never consumed from. ``auto.offset.reset`` put us at the head
                # as it stood when we joined, so that head is the position, and everything
                # published since is lag we are not fetching.
                position = baseline.get(key)
                if position is None:
                    position = bases[key] = high
            lag[key] = max(0, high - int(position))
        total = sum(lag.values())
        with self._lock:
            self._broker_lag = lag
            self._broker_lag_unknown = unknown
            self._broker_lag_base.update(bases)
            # THE TREND. A process that is merely slow has every in-process counter at zero
            # and a last_message_age of ~0.01 s, so no silence rule can ever reach it: 52 591
            # records (72 s) behind measured ok=True degraded=[]. What says "losing" rather
            # than "busy" is not the size of the backlog but its refusal to come down.
            consuming = self._counts["messages"] > self._broker_lag_msgs
            self._broker_lag_msgs = self._counts["messages"]
            floor = self._broker_lag_floor
            if total <= 0 or not consuming:
                self._broker_lag_floor = None
                self._broker_lag_rising = 0
            elif floor is None or total < floor:
                self._broker_lag_floor = total     # it fell: the streak starts again here
                self._broker_lag_rising = 0
            elif total - floor >= BROKER_LAG_RISE_RECORDS:
                self._broker_lag_rising += 1
            else:
                self._broker_lag_rising = 0

    def _poll_bookkeeping(self, consumer) -> None:
        """Refresh the cached partition assignment and broker lag. Never raises."""
        now = time.monotonic()
        if (now - self._assignment_at) < ASSIGNMENT_REFRESH_S:
            return
        self._assignment_at = now
        assigned = None
        assignment = getattr(consumer, "assignment", None)
        if assignment is None:
            with self._lock:
                self._assigned = len(self._max_seen)
        else:
            try:
                assigned = self._partition_keys(assignment() or [])
                self._assigned = len(assigned)
            except Exception:  # noqa: BLE001
                logger.debug("assignment() failed", exc_info=True)
        # The watermark refresh is NOT issued here. Measured against a real broker on
        # 2026-08-07: a subscribed, polling consumer calling get_watermark_offsets from its
        # own poll thread queues the ListOffsetsRequest behind its fetches, so at a 50 ms
        # timeout 55.6% of lookups failed (0.25 s: 48.3%; 1.0 s: 0%) and the signal that
        # carries the dead-ingest verdict was blind more often than not, while /health
        # reported ok. A timeout long enough to succeed is also long enough to stall ingest
        # (p95 501 ms x 6 partitions), so the refresh runs on the reconcile task instead —
        # an existing thread, no new machinery — and this tick only records the assignment.
        self._last_assigned = assigned

    def run(self, task) -> None:
        """Supervised entrypoint: poll until ``task.stopping``, heartbeat at least once a second.

        Deliberately no ``finally`` commit and no ``finally`` close: shutdown is ordered by the
        service as *stop polling → commit what became durable → close*, and closing here would
        destroy the consumer object before step two could use it.
        """
        consumer = self._ensure_consumer()
        while not task.stopping:
            task.heartbeat()
            # Drain what the broker already has (bounded), then write and commit it ONCE.
            msg = consumer.poll(0.5)
            handled = 0
            while msg is not None:
                task.heartbeat()  # a full batch must not look like a hung thread
                self.handle_message(msg)
                handled += 1
                msg = consumer.poll(0.0) if handled < POLL_BATCH else None
            self._poll_bookkeeping(consumer)
            self.commit_safe()
            # A partition blocked by a raising handler has no flush failure to trigger its
            # rewind, so the retry lives on the commit tick: one mechanism, both causes.
            self.rewind_if_blocked()

    def stats(self) -> dict:
        with self._lock:
            age = None
            if self._last_message_at is not None:
                age = round(max(0.0, time.monotonic() - self._last_message_at), 3)
            out = dict(self._counts)
            out["topics"] = self.broker_topics()
            out["by_topic"] = dict(self._by_topic)
            out["group_id"] = self.group_id
            out["last_message_age_s"] = age
            out["assigned_partitions"] = self._assigned
            out["discards"] = self._discards
            out["last_discard_age_s"] = (
                None if self._last_discard_at is None
                else round(max(0.0, time.monotonic() - self._last_discard_at), 3)
            )
            # Records consumed whose offset has not been committed: with synchronous writes
            # this is zero in a healthy steady state and non-zero exactly when a write failed.
            lag: Dict[str, int] = {}
            uncommitted = 0
            stuck: Optional[float] = None
            now = time.monotonic()
            for key, seen in self._max_seen.items():
                behind = int(seen + 1 - self._commit_at.get(key, seen))
                lag[f"{key[0]}:{key[1]}"] = behind
                uncommitted += behind
                if behind > 0:
                    # How long this partition's commit point has been shut. Zero-lag
                    # partitions are excluded: an idle topic's point is old but not blocked.
                    age = now - self._commit_moved_at.get(key, now)
                    stuck = age if stuck is None else max(stuck, age)
            out["commit_lag"] = lag
            # Records waiting AT THE BROKER past our commit point. Empty means "not
            # measured" (a client that cannot report watermarks), never "nothing waiting".
            out["broker_lag"] = {f"{key[0]}:{key[1]}": n for key, n in self._broker_lag.items()}
            out["broker_lag_total"] = sum(self._broker_lag.values())
            # Partitions the last refresh could NOT measure. Non-zero means the totals above
            # are incomplete, so they may not be read as "nothing waiting".
            out["broker_lag_unknown"] = self._broker_lag_unknown
            # Consecutive refreshes the backlog has failed to fall back to its floor while
            # records were still being consumed. The derivative, not the level.
            out["broker_lag_rising"] = self._broker_lag_rising
            out["uncommitted"] = uncommitted + self._untracked
            out["commit_stuck_s"] = None if stuck is None else round(max(0.0, stuck), 3)
        return out

    def close(self) -> None:
        with self._lock:
            consumer = self._consumer
            self._consumer = None
        if consumer is not None:
            try:
                consumer.close()
            except Exception:  # noqa: BLE001
                logger.debug("consumer close failed", exc_info=True)
