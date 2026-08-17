"""DataplaneConsumer routing + total-failure-handling tests. No Kafka is involved."""

from __future__ import annotations

import json
import threading
import time

import pytest

from apps.config import kafka_config
from apps.dataplane.health import BROKER_LAG_RISING_REFRESHES
from apps.dataplane.ingest.consumer import (
    BROKER_LAG_REFRESH_S,
    WATERMARK_TIMEOUT_S,
    DataplaneConsumer,
    _PARTITION_EOF,
)

BROKER = kafka_config["topics"]


class FakeError:
    def __init__(self, code=-1, msg="fake error"):
        self._code = code
        self._msg = msg

    def code(self):
        return self._code

    def __str__(self):
        return self._msg


class FakeMessage:
    """Duck-types confluent_kafka.Message: .topic() .key() .value() .error()."""

    def __init__(self, topic, key=b"run_1", value=None, error=None, raw=None):
        self._topic = topic
        self._key = key
        if raw is not None:
            self._value = raw
        else:
            self._value = None if value is None else json.dumps(value).encode("utf-8")
        self._error = error

    def topic(self):
        return self._topic

    def key(self):
        return self._key

    def value(self):
        return self._value

    def error(self):
        return self._error


class OffsetMessage(FakeMessage):
    """A FakeMessage that CAN report its coordinates, like a real librdkafka message."""

    def __init__(self, topic, key=b"run_1", value=None, error=None, raw=None,
                 partition=0, offset=0):
        super().__init__(topic, key=key, value=value, error=error, raw=raw)
        self._partition = partition
        self._offset = offset

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset


class FakeTP:
    def __init__(self, topic, partition=0):
        self.topic, self.partition = topic, partition


class FakeConsumer:
    def __init__(self, conf):
        self.conf = conf
        self.subscribed = None
        self.closed = False
        self.queue = []
        self.polls = 0
        self.commits = []
        self.committed_offsets = []
        # The watermark surface the lag signal is built on. A double that cannot answer it
        # cannot express the cost of answering it, which is the defect this pins.
        self.heads = {}
        self.watermark_calls = []      # (monotonic_at, topic, timeout)
        self.watermark_delay = 0.0

    def subscribe(self, topics):
        self.subscribed = list(topics)

    def assignment(self):
        return [FakeTP(t, 0) for t in (self.subscribed or [])]

    def get_watermark_offsets(self, tp, timeout=None, cached=False):
        self.watermark_calls.append((time.monotonic(), tp.topic, timeout))
        if self.watermark_delay:
            time.sleep(min(self.watermark_delay, float(timeout if timeout is not None else 0.5)))
        return (0, int(self.heads.get(tp.topic, 0)))

    def poll(self, timeout):
        self.polls += 1
        if self.queue:
            return self.queue.pop(0)
        time.sleep(0.005)
        return None

    def commit(self, offsets=None, asynchronous=False):
        self.commits.append(asynchronous)
        if offsets is not None:
            self.committed_offsets.append(
                sorted((tp.topic, tp.partition, tp.offset) for tp in offsets)
            )

    def close(self):
        self.closed = True


def recording_handlers():
    seen = {}

    def make(name):
        def handler(topic, run_id, payload):
            seen.setdefault(name, []).append((topic, run_id, payload))

        return handler

    return seen, {k: make(k) for k in DataplaneConsumer.LOGICAL_TOPICS}


def make_consumer(handlers=None, factory=None):
    if handlers is None:
        _, handlers = recording_handlers()
    return DataplaneConsumer(
        handlers=handlers,
        consumer_factory=factory or (lambda conf: FakeConsumer(conf)),
    )


class TestWiring:
    def test_topic_map_covers_exactly_the_six_logical_topics(self):
        c = make_consumer()
        assert set(c.topic_map().values()) == set(DataplaneConsumer.LOGICAL_TOPICS)
        assert c.topic_map() == {
            BROKER["run_status"]: "run_status",
            BROKER["kpi"]: "kpi",
            BROKER["kpi_breakdown"]: "kpi_breakdown",
            BROKER["trip_geo"]: "trip_geo",
            BROKER["facility_stream"]: "facility_stream",
            BROKER["perf"]: "perf",
        }
        assert c.broker_topics() == [
            "run_status",
            "kpi_stream",
            "kpi_breakdown_stream",
            "trip_geo_stream",
            "facility_stream",
            "perf_stream",
        ]

    def test_one_group_id_and_exactly_six_subscribed_topics(self):
        made = []
        c = make_consumer(factory=lambda conf: made.append(FakeConsumer(conf)) or made[-1])

        class Task:
            stopping = True

            def heartbeat(self):
                pass

        c.run(Task())
        assert len(made) == 1
        assert made[0].conf["group.id"] == "dataplane"
        assert len(made[0].subscribed) == 6
        assert len(set(made[0].subscribed)) == 6
        # run() deliberately does NOT close: shutdown is drain -> commit -> close, and the
        # commit step needs this object alive. Closing is the caller's, explicitly.
        assert made[0].closed is False
        c.close()
        assert made[0].closed is True


class TestRouting:
    @pytest.mark.parametrize(
        "logical,broker",
        [(k, BROKER[k]) for k in DataplaneConsumer.LOGICAL_TOPICS],
    )
    def test_each_broker_topic_routes_to_its_logical_handler(self, logical, broker):
        seen, handlers = recording_handlers()
        c = make_consumer(handlers)
        assert c.handle_message(FakeMessage(broker, b"run_42", {"hello": logical})) is True
        assert list(seen) == [logical]
        got_topic, run_id, payload = seen[logical][0]
        assert got_topic == logical
        assert run_id == "run_42"
        assert payload == {"hello": logical}
        assert c.stats()["messages"] == 1
        assert c.stats()["by_topic"][logical] == 1

    def test_string_key_is_accepted(self):
        seen, handlers = recording_handlers()
        c = make_consumer(handlers)
        assert c.handle_message(FakeMessage(BROKER["kpi"], "run_str", {"a": 1})) is True
        assert seen["kpi"][0][1] == "run_str"


class TestSurvivesEverything:
    def test_malformed_json(self):
        seen, handlers = recording_handlers()
        c = make_consumer(handlers)
        assert c.handle_message(FakeMessage(BROKER["kpi"], raw=b"{not json")) is False
        assert c.stats()["decode_errors"] == 1
        assert seen == {}

    def test_non_dict_payload(self):
        c = make_consumer()
        assert c.handle_message(FakeMessage(BROKER["kpi"], value=[1, 2, 3])) is False
        assert c.stats()["decode_errors"] == 1

    def test_none_key(self):
        c = make_consumer()
        assert c.handle_message(FakeMessage(BROKER["kpi"], key=None, value={"a": 1})) is False
        assert c.stats()["missing_key"] == 1

    def test_none_value(self):
        c = make_consumer()
        assert c.handle_message(FakeMessage(BROKER["kpi"], value=None)) is False
        assert c.stats()["empty_value"] == 1

    def test_partition_eof_is_not_an_error(self):
        c = make_consumer()
        msg = FakeMessage(BROKER["kpi"], error=FakeError(_PARTITION_EOF, "eof"))
        assert c.handle_message(msg) is False
        assert c.stats()["eof"] == 1
        assert c.stats()["kafka_errors"] == 0

    def test_real_kafka_error_is_counted(self):
        c = make_consumer()
        assert c.handle_message(FakeMessage(BROKER["kpi"], error=FakeError(1, "broken"))) is False
        assert c.stats()["kafka_errors"] == 1

    def test_unknown_topic(self):
        c = make_consumer()
        assert c.handle_message(FakeMessage("some_other_topic", value={"a": 1})) is False
        assert c.stats()["unknown_topic"] == 1

    def test_handler_exception_is_swallowed_and_counted(self):
        def boom(topic, run_id, payload):
            raise ValueError("poison payload")

        c = make_consumer({"kpi": boom})
        assert c.handle_message(FakeMessage(BROKER["kpi"], value={"a": 1})) is False
        assert c.stats()["handler_errors"] == 1
        assert c.stats()["messages"] == 1  # it arrived; it just failed

    def test_a_message_object_that_explodes_does_not_raise(self):
        class Exploding:
            def error(self):
                raise RuntimeError("librdkafka went sideways")

        c = make_consumer()
        assert c.handle_message(Exploding()) is False
        assert c.stats()["handler_errors"] == 1

    def test_missing_handler_for_a_known_topic(self):
        c = make_consumer({"kpi": lambda *a: None})
        assert c.handle_message(FakeMessage(BROKER["perf"], value={"a": 1})) is False
        assert c.stats()["no_handler"] == 1


class TestLoop:
    def test_run_polls_until_stopping_and_heartbeats(self):
        seen, handlers = recording_handlers()
        fake = {}

        def factory(conf):
            fake["c"] = FakeConsumer(conf)
            fake["c"].queue = [
                FakeMessage(BROKER["kpi"], value={"metric": "m", "value": 1}),
                FakeMessage(BROKER["perf"], value={"x": 1}),
            ]
            return fake["c"]

        c = make_consumer(handlers, factory)

        class Task:
            def __init__(self):
                self.beats = 0
                self.stop = threading.Event()

            @property
            def stopping(self):
                return self.stop.is_set()

            def heartbeat(self):
                self.beats += 1

        task = Task()
        t = threading.Thread(target=c.run, args=(task,), daemon=True)
        t.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen) < 2:
            time.sleep(0.01)
        task.stop.set()
        t.join(timeout=3)
        assert not t.is_alive()
        assert set(seen) == {"kpi", "perf"}
        assert task.beats >= 2
        assert fake["c"].closed is False   # see test_one_group_id_...: run() never closes
        c.close()
        assert fake["c"].closed is True

    def test_stats_last_message_age(self):
        c = make_consumer()
        assert c.stats()["last_message_age_s"] is None
        c.handle_message(FakeMessage(BROKER["kpi"], value={"a": 1}))
        assert c.stats()["last_message_age_s"] is not None
        assert c.stats()["last_message_age_s"] < 1.0

    def test_counters_are_correct_under_concurrent_dispatch(self):
        """Race real threads through handle_message; every message must be accounted for."""
        received = []
        lock = threading.Lock()

        def handler(topic, run_id, payload):
            with lock:
                received.append(payload["i"])

        c = make_consumer({k: handler for k in DataplaneConsumer.LOGICAL_TOPICS})
        n_threads, per_thread = 8, 100

        def push(offset):
            for i in range(per_thread):
                c.handle_message(FakeMessage(BROKER["kpi"], value={"i": offset + i}))

        threads = [threading.Thread(target=push, args=(t * per_thread,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(received) == n_threads * per_thread
        assert c.stats()["messages"] == n_threads * per_thread
        assert c.stats()["by_topic"]["kpi"] == n_threads * per_thread
        assert c.stats()["handler_errors"] == 0


class TestOffsetCommits:
    """The whole offset rule: advance over a record whose handler wrote it, and nothing else.

    Two gates failed here before. Auto-commit advanced offsets whether or not the row ever
    reached DuckDB. Its replacement, a ``commit_ready`` callback over per-writer failure
    counters, latched shut forever the moment a writer's traffic stopped (500 healthy
    messages, ``commits=0``, /health green) and re-opened over rows that had been shredded
    (offsets 2, 3, 4 committed over two lost snapshots) because the next success from that
    writer cleared the counter. Its replacement in turn — an offset ledger of deferred marks
    released by a background writer — leaked marks on every rejected offer and pinned a
    healthy partition at offset 4 of 40 for the life of the process.

    What is left is three lines in ``_note_offset`` and no state a writer has to clear.
    """

    def test_auto_commit_is_disabled(self):
        conf = make_consumer().consumer_config()
        assert conf["enable.auto.commit"] is False
        assert conf["group.id"] == "dataplane"

    def _drive(self, consumer, fake, *, messages):
        """Run the poll loop until the queue drains, then stop."""

        class Task:
            def __init__(self):
                self.stop = threading.Event()

            @property
            def stopping(self):
                return self.stop.is_set()

            def heartbeat(self):
                pass

        fake.queue = list(messages)
        task = Task()
        thread = threading.Thread(target=consumer.run, args=(task,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and fake.queue:
            time.sleep(0.01)
        time.sleep(0.15)  # let at least one commit tick elapse
        task.stop.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        return task

    def _consumer_with(self, handlers=None, fake=None):
        fake = fake if fake is not None else {}
        if handlers is None:
            _, handlers = recording_handlers()
        c = DataplaneConsumer(
            handlers=handlers,
            consumer_factory=lambda conf: fake.setdefault("c", FakeConsumer(conf)),
            commit_interval_s=0.01,
        )
        c._ensure_consumer()
        return c, fake["c"]

    def test_a_clean_handler_commits_the_offset_it_just_wrote(self):
        c, fake = self._consumer_with()
        self._drive(c, fake, messages=[
            OffsetMessage(BROKER["kpi"], value={"metric": "m", "value": i}, offset=i)
            for i in range(3)
        ])
        assert fake.committed_offsets[-1] == [("kpi_stream", 0, 3)]
        assert c.stats()["uncommitted"] == 0

    def test_offsets_do_not_advance_past_a_record_whose_write_raised(self):
        """The durability contract: the write raised, so the offset is not committed."""
        boom = {"on": True}

        def writing(topic, run_id, payload):
            if boom["on"]:
                raise RuntimeError("DuckStoreError: store is down")

        c, fake = self._consumer_with(handlers={"kpi": writing})
        for i in range(3):
            c.handle_message(OffsetMessage(BROKER["kpi"], value={"metric": "m"}, offset=i))
        assert c.commit_safe(force=True) is True
        # Committed AT the oldest unwritten record — a restart replays exactly those three.
        assert fake.committed_offsets[-1] == [("kpi_stream", 0, 0)]
        assert c.stats()["uncommitted"] == 3
        assert c.stats()["handler_errors"] == 3

    def test_one_failure_freezes_the_partition_even_if_later_records_succeed(self):
        """Nothing may be committed OVER unwritten data — the shredded-snapshot regression."""
        fail_at = {2}

        def writing(topic, run_id, payload):
            if payload["i"] in fail_at:
                raise RuntimeError("store said no")

        c, fake = self._consumer_with(handlers={"kpi": writing})
        for i in range(5):
            c.handle_message(OffsetMessage(BROKER["kpi"], value={"i": i}, offset=i))
        assert c.commit_safe(force=True) is True
        assert fake.committed_offsets[-1] == [("kpi_stream", 0, 2)]
        assert c.stats()["commit_lag"] == {"kpi_stream:0": 3}

    def test_compaction_gaps_on_run_status_do_not_freeze_its_commit_point(self):
        """``run_status`` is a COMPACTED topic, so its offsets are non-contiguous by design.

        apps/config.py declares ``cleanup.policy=compact`` keyed by ``run_id``, and every run
        publishes many RUNNING messages under one key — so once a segment is cleaned the
        surviving offsets have holes. Requiring the commit point to sit exactly on the
        delivered offset jammed the topic on the first hole with NOTHING having failed:
        measured, six byte-identical clean terminals at [100, 107, 131, 168, 202, 259] handled
        with 0 handler errors committed 101 against a head of 260, ``commit_lag`` 159 and
        /health 503 ``ingest_wedged`` — and every restart replayed the whole retained region,
        re-finalizing and re-archiving every run in it. A gap the broker never delivers is not
        an unwritten record.
        """
        c, fake = self._consumer_with(handlers={"run_status": lambda *a: None})
        for offset in (100, 107, 131, 168, 202, 259):
            c.handle_message(
                OffsetMessage(BROKER["run_status"], value={"status": "COMPLETED"}, offset=offset)
            )
        assert c.commit_safe(force=True) is True
        assert fake.committed_offsets[-1] == [("run_status", 0, 260)]
        assert c.stats()["commit_lag"] == {"run_status:0": 0}
        assert c.stats()["commit_stuck_s"] is None

    def test_a_gap_after_a_failure_is_still_held_by_the_failure(self):
        """Stepping over gaps must not step over unwritten data — that would be silent loss.

        The partition is held by the *failure*, not by contiguity, so a compacted gap above
        an unwritten record buys nothing: the commit point stays at the record whose handler
        raised, exactly where a replay has to resume.
        """
        def writing(topic, run_id, payload):
            if payload["i"] == 1:
                raise RuntimeError("store said no")

        c, fake = self._consumer_with(handlers={"run_status": writing})
        for i, offset in enumerate((10, 11, 40, 75)):
            c.handle_message(OffsetMessage(BROKER["run_status"], value={"i": i}, offset=offset))
        assert c.commit_safe(force=True) is True
        assert fake.committed_offsets[-1] == [("run_status", 0, 11)]
        assert c.stats()["commit_lag"] == {"run_status:0": 65}

    def test_a_silent_topic_cannot_hold_another_topics_offsets(self):
        """The r8 latch, at the level that used to own it.

        `frames` writes stop when a run is evicted, so the old counter could never be
        cleared and EVERY partition stayed blocked for the life of the process. Partitions
        are independent here and a topic whose writes succeed advances on its own.
        """
        def geo_handler(topic, run_id, payload):
            raise RuntimeError("this run's write never completed")

        _, handlers = recording_handlers()
        handlers["trip_geo"] = geo_handler
        c, fake = self._consumer_with(handlers=handlers)

        c.handle_message(OffsetMessage(BROKER["trip_geo"], value={"type": "truck_loc"}, offset=0))
        for i in range(500):
            c.handle_message(OffsetMessage(BROKER["kpi"], value={"metric": "m"}, offset=i))

        assert c.commit_safe(force=True) is True
        committed = dict(((t, p), o) for t, p, o in fake.committed_offsets[-1])
        assert committed[("kpi_stream", 0)] == 500      # 500 healthy messages: committed
        assert committed[("trip_geo_stream", 0)] == 0   # the unwritten one: pinned, not blocking
        assert c.stats()["commits"] >= 1

    def test_a_message_that_cannot_report_an_offset_is_untracked_and_commits_blind(self):
        """The documented fallback: fakes and embeddings without offsets still commit."""
        c, fake = self._consumer_with()
        c.handle_message(FakeMessage(BROKER["kpi"], value={"metric": "m"}))
        assert c.stats()["uncommitted"] == 1
        assert c.commit_safe(force=True) is True
        assert fake.commits == [False]
        assert fake.committed_offsets == []             # blind: no explicit offsets
        assert c.stats()["uncommitted"] == 0

    def test_a_commit_error_is_counted_and_retried(self):
        fake = {}
        _, handlers = recording_handlers()

        class ExplodingConsumer(FakeConsumer):
            def commit(self, offsets=None, asynchronous=False):
                raise RuntimeError("no offsets to commit")

        c = DataplaneConsumer(
            handlers=handlers,
            consumer_factory=lambda conf: fake.setdefault("c", ExplodingConsumer(conf)),
            commit_interval_s=0.0,
        )
        c._ensure_consumer()
        c.handle_message(FakeMessage(BROKER["kpi"], value={"metric": "m"}))
        assert c.commit_safe(force=True) is False
        assert c.stats()["commit_errors"] == 1
        assert c.stats()["uncommitted"] == 1  # still owed, so it is retried


class TestDiscardsAreFinal:
    """A record the process can never use commits and moves on. Holding it wedges a partition.

    Every one of these fails identically on every redelivery, so freezing the partition on
    them buys nothing and costs everything: one malformed producer message used to pin
    ``kpi_breakdown_stream`` at offset 0 for the life of the deployment. They are counted as
    discards instead, and ``ingest_discarding`` judges the rate.
    """

    @pytest.mark.parametrize("message,counter", [
        (OffsetMessage("some_other_topic", value={"a": 1}), "unknown_topic"),
        (OffsetMessage(BROKER["kpi"], key=None, value={"a": 1}), "missing_key"),
        (OffsetMessage(BROKER["kpi"], value=None), "empty_value"),
        (OffsetMessage(BROKER["kpi"], raw=b"{not json"), "decode_errors"),
        (OffsetMessage(BROKER["perf"], value={"a": 1}), "no_handler"),
    ])
    def test_a_message_that_produces_no_row_commits_its_offset(self, message, counter):
        c = DataplaneConsumer(handlers={"kpi": lambda *a: None},
                              consumer_factory=lambda conf: FakeConsumer(conf))
        c.handle_message(message)
        assert c.stats()[counter] == 1
        assert c.safe_offsets() == [(message.topic(), 0, 1)]
        assert c.stats()["discards"] == 1

    def test_trip_geo_commits_on_receipt_and_the_docstring_says_why(self):
        """Positions are a SAMPLED view; the durable form is the frame series. Replaying an
        older offset would mint different frame_idx values for the same instants, so holding
        these offsets buys duplication, not durability."""
        from apps.dataplane.ingest import consumer as consumer_module

        assert "sampled" in consumer_module.__doc__
        assert "frame_idx" in consumer_module.__doc__

        seen, handlers = recording_handlers()
        c = DataplaneConsumer(handlers=handlers,
                              consumer_factory=lambda conf: FakeConsumer(conf))
        c.handle_message(OffsetMessage(BROKER["trip_geo"], value={"type": "truck_loc"}, offset=9))
        assert seen["trip_geo"]
        assert c.safe_offsets() == [("trip_geo_stream", 0, 10)]

    def test_a_handler_that_raises_does_not_commit(self):
        def boom(topic, run_id, payload):
            raise ValueError("poison payload")

        c = DataplaneConsumer(handlers={"kpi": boom},
                              consumer_factory=lambda conf: FakeConsumer(conf))
        assert c.handle_message(OffsetMessage(BROKER["kpi"], value={"a": 1}, offset=4)) is False
        assert c.stats()["handler_errors"] == 1
        assert c.safe_offsets() == [("kpi_stream", 0, 4)], "replay it, do not skip it"

    def test_offsets_are_tracked_per_partition_under_concurrent_dispatch(self):
        """handle_message is driven from one thread in production and from many in tests."""
        def handler(topic, run_id, payload):
            if payload["i"] % 2 == 1:
                raise RuntimeError("odd records never reach the store")

        consumer = DataplaneConsumer(handlers={"kpi": handler},
                                     consumer_factory=lambda conf: FakeConsumer(conf))

        def push(partition):
            for i in range(50):
                consumer.handle_message(OffsetMessage(
                    BROKER["kpi"], value={"i": i}, partition=partition, offset=i))

        threads = [threading.Thread(target=push, args=(p,)) for p in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        # Offset 0 wrote, offset 1 raised: every partition is pinned at 1 and 49 records
        # per partition are replayable.
        assert consumer.safe_offsets() == [("kpi_stream", p, 1) for p in range(4)]
        assert consumer.stats()["commit_lag"] == {f"kpi_stream:{p}": 49 for p in range(4)}


class TestConsumerConstruction:
    """Regression r7: the Consumer was cached BEFORE subscribe()."""

    def test_a_failed_subscribe_does_not_cache_a_half_built_consumer(self):
        built = []
        subscribed = []
        fail = {"n": 1}

        class Flaky(FakeConsumer):
            def subscribe(self, topics):
                if fail["n"] > 0:
                    fail["n"] -= 1
                    raise RuntimeError("transient metadata failure")
                super().subscribe(topics)
                subscribed.append(self)

        def factory(conf):
            c = Flaky(conf)
            built.append(c)
            return c

        c = make_consumer(factory=factory)
        with pytest.raises(RuntimeError):
            c._ensure_consumer()
        assert c._consumer is None, "a consumer that never subscribed must not be cached"
        assert built[0].closed is True, "the candidate must be closed, not leaked"

        again = c._ensure_consumer()
        assert len(built) == 2 and len(subscribed) == 1
        assert again.subscribed is not None

    def test_a_supervised_restart_after_a_failed_subscribe_actually_subscribes(self):
        """End to end with the REAL SupervisedTask: measured before this fix, one transient
        subscribe failure produced 1 consumer built, 0 subscribes and 700 polls against an
        unsubscribed object, with task.alive() True and /health 200."""
        from apps.dataplane.supervisor import SupervisedTask

        built = []
        subscribed = []
        fail = {"n": 1}

        class Flaky(FakeConsumer):
            def subscribe(self, topics):
                if fail["n"] > 0:
                    fail["n"] -= 1
                    raise RuntimeError("transient metadata failure")
                super().subscribe(topics)
                subscribed.append(self)

        def factory(conf):
            c = Flaky(conf)
            built.append(c)
            return c

        consumer = make_consumer(factory=factory)
        task = SupervisedTask("consumer", consumer.run, backoff_initial=0.02, backoff_max=0.02)
        task.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not subscribed:
                time.sleep(0.01)
            assert len(subscribed) == 1
            assert sum(c.polls for c in built if c.subscribed is None) == 0
            assert sum(c.polls for c in built) > 0
        finally:
            task.stop(timeout=2.0)
            consumer.close()


class TestStats:
    def test_the_health_relevant_keys_are_present(self):
        c = make_consumer()
        c.handle_message(OffsetMessage(BROKER["kpi"], value={"a": 1}, offset=3))
        stats = c.stats()
        for key in ("uncommitted", "commit_lag", "assigned_partitions", "discards"):
            assert key in stats
        # The first offset seen on a partition is where the commit point starts, so a
        # written record leaves nothing behind it.
        assert stats["commit_lag"] == {"kpi_stream:0": 0}
        assert stats["uncommitted"] == 0


class TestBrokerLagRefreshIsBoundedOnThePollThread:
    """The lag signal must never be issued from the poll loop.

    Measured against a REAL broker on 2026-08-07, which no fake had shown: a subscribed,
    polling consumer calling ``get_watermark_offsets`` from its own poll thread queues the
    ListOffsetsRequest behind its fetches. At the 50 ms timeout this package shipped,
    **55.6% of lookups failed** (0.25 s: 48.3%; 1.0 s: 0%) — so the one signal carrying the
    dead-ingest verdict was blind more often than not while /health reported ok, and the
    broker log filled with REQTMOUT. A timeout long enough to succeed is long enough to
    stall ingest (p95 501 ms x 6 partitions), so the refresh runs on the reconcile task.
    """

    def test_the_poll_tick_issues_no_watermark_lookup(self):
        c = make_consumer()
        client = c._ensure_consumer()
        for _ in range(5):
            c._assignment_at = c._broker_lag_at = 0.0
            c._poll_bookkeeping(client)
        assert client.watermark_calls == []      # ingest never pays for the signal

    def test_the_refresh_is_cached_and_each_lookup_is_bounded(self):
        c = make_consumer()
        client = c._ensure_consumer()

        c._poll_bookkeeping(client)              # records the assignment only
        c.refresh_broker_lag()
        first = len(client.watermark_calls)
        assert first == len(c.broker_topics())      # the whole assignment, exactly once
        assert {t for _at, t, _to in client.watermark_calls} == set(c.broker_topics())
        assert {to for _at, _t, to in client.watermark_calls} == {WATERMARK_TIMEOUT_S}
        # Long enough to actually answer: 0.05 s measured 55.6% unmeasurable on a real broker.
        assert WATERMARK_TIMEOUT_S >= 0.5
        assert c.stats()["broker_lag"][f"{BROKER['kpi']}:0"] == 0

        # The poll loop calls the tick every iteration. Within the interval the CACHE is what
        # it reads: the broker's head moves and not one further lookup is issued.
        client.heads[BROKER["kpi"]] = 500
        deadline = time.monotonic() + min(0.5, BROKER_LAG_REFRESH_S / 2)
        while time.monotonic() < deadline:
            c._assignment_at = 0.0    # the assignment tick is cheap; the watermark tick is not
            c._poll_bookkeeping(client)
            c.refresh_broker_lag()
        assert len(client.watermark_calls) == first
        assert c.stats()["broker_lag"][f"{BROKER['kpi']}:0"] == 0

        # …and once the interval is up it is measured again, from the head at join time.
        c._assignment_at = c._broker_lag_at = 0.0
        c._poll_bookkeeping(client)
        c.refresh_broker_lag()
        assert len(client.watermark_calls) == 2 * first
        assert c.stats()["broker_lag"][f"{BROKER['kpi']}:0"] == 500

    def test_a_slow_broker_cannot_burn_the_poll_thread(self):
        c = make_consumer()
        client = c._ensure_consumer()
        client.watermark_delay = 0.5      # every lookup burns half a second
        started = time.monotonic()
        c._poll_bookkeeping(client)
        spent = time.monotonic() - started
        # The poll tick is now free of the lookup entirely, whatever the broker is doing.
        assert spent < 0.1, spent
        # The cost lands on the reconcile task, bounded by the timeout per partition.
        started = time.monotonic()
        c.refresh_broker_lag()
        spent = time.monotonic() - started
        assert spent <= len(c.broker_topics()) * WATERMARK_TIMEOUT_S + 0.15, spent


class TestTheBacklogTrendIsMeasured:
    """A process that is merely SLOW is invisible to every in-process counter there is.

    It receives records constantly (``last_message_age_s`` ~0.01 s), writes every one of them
    (``commit_lag`` 0) and discards nothing, while the broker runs away from it: measured
    8 745 records behind at t+10 s and 52 591 — 72 s of backlog — at t+60 s, under ok=True
    degraded=[] throughout. The level cannot carry the verdict (a live run measures a lag of
    11 routinely, and one poll batch is 200 records); the derivative can.
    """

    @staticmethod
    def _refresh(consumer, client):
        # The watermark refresh moved OFF the poll thread on 2026-08-07: against a real
        # broker, a polling consumer's own ListOffsetsRequest queues behind its fetches and
        # 55.6% of lookups failed at the old 50 ms timeout. It now runs on the reconcile
        # task, so a test that wants a refresh must ask for one rather than poll.
        consumer._assignment_at = consumer._broker_lag_at = 0.0
        consumer._poll_bookkeeping(client)
        consumer._broker_lag_at = 0.0
        consumer.refresh_broker_lag()

    def test_a_backlog_that_will_not_fall_while_records_flow_is_counted(self):
        c = make_consumer()
        client = c._ensure_consumer()
        topic = BROKER["kpi"]
        offset = 0

        # Busy: the consumer takes a record per refresh and the head stays within reach.
        for head in (0, 50, 100, 150, 200, 250, 300):
            client.heads[topic] = head
            c.handle_message(OffsetMessage(topic, value={"a": 1}, offset=offset))
            offset += 1
            self._refresh(c, client)
            assert c.stats()["broker_lag_rising"] == 0, head

        # Losing: still consuming, and the backlog never comes back down.
        for i in range(BROKER_LAG_RISING_REFRESHES + 1):
            client.heads[topic] = 5000 * (i + 1)
            c.handle_message(OffsetMessage(topic, value={"a": 1}, offset=offset))
            offset += 1
            self._refresh(c, client)
        stats = c.stats()
        assert stats["broker_lag_rising"] >= BROKER_LAG_RISING_REFRESHES, stats
        assert stats["broker_lag_total"] > 0

        # …and it is a function of pending data, so draining clears it.
        client.heads[topic] = offset
        c.handle_message(OffsetMessage(topic, value={"a": 1}, offset=offset))
        offset += 1
        self._refresh(c, client)
        assert c.stats()["broker_lag_rising"] == 0

    def test_a_backlog_nobody_is_consuming_is_not_this_rule(self):
        """A runaway head with NO records coming in is the dead case ``ingest_stalled`` owns.

        Counting it here too would answer ``ingest_behind`` for an idle process whose broker
        merely has data — the one thing a rule about falling behind must not say.
        """
        c = make_consumer()
        client = c._ensure_consumer()
        topic = BROKER["kpi"]
        c.handle_message(OffsetMessage(topic, value={"a": 1}, offset=0))
        for i in range(BROKER_LAG_RISING_REFRESHES + 3):
            client.heads[topic] = 5000 * (i + 1)
            self._refresh(c, client)
        stats = c.stats()
        assert stats["broker_lag_total"] > 0     # the backlog IS measured…
        assert stats["broker_lag_rising"] == 0   # …but this is not what it is evidence of


class TestPollBatch:
    """The loop consumes a batch, writes it, commits it — no queue, no writer thread."""

    def test_the_loop_drains_a_batch_and_flushes_once_before_committing(self):
        """Round 6 wrote one DuckDB transaction per record and capped ingest at ~141 msg/s.

        The loop used to handle exactly one message per iteration, so ``commit_safe`` (and
        with it the flush) ran once per record. Draining what the broker already has and
        flushing the batch is the whole throughput fix, and the ordering is the durability
        contract: handlers, then flush, then commit — never a commit while a row a handler
        accepted is unwritten.
        """
        order = []
        seen, handlers = recording_handlers()
        fake = {}

        def factory(conf):
            fake["c"] = FakeConsumer(conf)
            fake["c"].queue = [
                FakeMessage(BROKER["kpi"], value={"metric": f"m{i}", "value": i})
                for i in range(25)
            ]
            return fake["c"]

        def flush():
            order.append(("flush", len(seen.get("kpi", []))))

        c = DataplaneConsumer(
            handlers=handlers, consumer_factory=factory, flush=flush, commit_interval_s=0.0
        )
        real_commit = FakeConsumer.commit

        class Task:
            def __init__(self):
                self.stop = threading.Event()

            @property
            def stopping(self):
                return self.stop.is_set()

            def heartbeat(self):
                pass

        task = Task()
        t = threading.Thread(target=c.run, args=(task,), daemon=True)
        t.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(seen.get("kpi", [])) < 25:
            time.sleep(0.01)
        task.stop.set()
        t.join(timeout=3)
        assert not t.is_alive()

        assert len(seen["kpi"]) == 25
        # All 25 were routed before the first flush: one statement, not twenty-five.
        assert order[0] == ("flush", 25)
        assert real_commit is FakeConsumer.commit

    def test_nothing_is_committed_when_the_flush_fails_and_the_offsets_roll_back(self):
        """A flush that raises must leave the commit points where the last one left them.

        Otherwise the batch's offsets sit past rows nothing wrote and the next successful
        flush commits over them — silent loss, which is the failure this package exists to
        remove. Retaining the rows instead would be a retry queue that grows for as long as
        the store is down; the records are replayed from the broker instead.
        """
        seen, handlers = recording_handlers()
        state = {"fail": False}

        def flush():
            if state["fail"]:
                raise RuntimeError("IO Error: disk full")

        c = DataplaneConsumer(handlers=handlers, flush=flush)
        topic = c.broker_topics()[DataplaneConsumer.LOGICAL_TOPICS.index("kpi")]
        for offset in range(3):
            c.handle_message(OffsetMessage(topic, value={"metric": "m", "value": 1}, offset=offset))
        assert c.flush_writes() is True
        assert dict(((t, p), o) for t, p, o in c.safe_offsets())[(topic, 0)] == 3

        state["fail"] = True
        for offset in range(3, 9):
            c.handle_message(OffsetMessage(topic, value={"metric": "m", "value": 1}, offset=offset))
        assert c.flush_writes() is False
        assert dict(((t, p), o) for t, p, o in c.safe_offsets())[(topic, 0)] == 3
        stats = c.stats()
        assert stats["commit_lag"][f"{topic}:0"] == 6
        assert stats["handler_errors"] == 1
        assert stats["discards"] == 1

        # …and recovery moves it again: nothing here is a latch.
        state["fail"] = False
        for offset in range(3, 9):
            c.handle_message(OffsetMessage(topic, value={"metric": "m", "value": 1}, offset=offset))
        assert c.flush_writes() is True
        assert dict(((t, p), o) for t, p, o in c.safe_offsets())[(topic, 0)] == 9
        assert c.stats()["commit_stuck_s"] is None

    def test_a_failed_flush_rolls_back_only_the_topics_whose_rows_it_dropped(self):
        """The blast radius of one transient store error is the deferring topic, and no other.

        ``flush`` buffers kpi rows only; every other handler wrote synchronously and durably
        during the same batch. Rolling the whole commit map back froze all six topics for the
        life of the process — measured: one injected ``write_kpi_events`` failure left
        ``trip_geo_stream`` and ``kpi_breakdown_stream`` pinned at 5 while 30 further clean
        records were consumed and written, ``commit_lag`` 5 -> 15 on every partition. For
        ``trip_geo`` that is worse than a stall: its offsets commit on receipt precisely
        because a replay re-drives the sampler, and ``frames`` has no primary key, so the
        restart that clears the freeze mints a second frame series for the same instants.
        """
        seen, handlers = recording_handlers()
        state = {"fail": False}

        def flush():
            if state["fail"]:
                raise RuntimeError("TransactionException: store is down")

        c = DataplaneConsumer(handlers=handlers, flush=flush, flush_topics=("kpi",))
        topics = {
            logical: c.broker_topics()[DataplaneConsumer.LOGICAL_TOPICS.index(logical)]
            for logical in ("kpi", "kpi_breakdown", "trip_geo")
        }
        payloads = {
            "kpi": {"metric": "m", "value": 1},
            "kpi_breakdown": {"scope": "truck", "breakdown": {"entities": []}},
            "trip_geo": {"type": "truck_loc", "truck_agent_id": "t1", "lon": 4.0, "lat": 51.0},
        }
        for offset in range(5):
            for logical, topic in topics.items():
                c.handle_message(OffsetMessage(topic, value=payloads[logical], offset=offset))
        assert c.flush_writes() is True
        floor = {(t, p): o for t, p, o in c.safe_offsets()}
        assert floor == {(topic, 0): 5 for topic in topics.values()}

        state["fail"] = True
        for offset in range(5, 10):
            for logical, topic in topics.items():
                c.handle_message(OffsetMessage(topic, value=payloads[logical], offset=offset))
        assert c.flush_writes() is False

        after = {(t, p): o for t, p, o in c.safe_offsets()}
        # kpi rows were dropped, so kpi alone freezes at the last flush that returned…
        assert after[(topics["kpi"], 0)] == 5
        # …and the two topics whose handlers already wrote are committable to the head.
        assert after[(topics["kpi_breakdown"], 0)] == 10
        assert after[(topics["trip_geo"], 0)] == 10
        lag = c.stats()["commit_lag"]
        assert lag[f"{topics['kpi']}:0"] == 5
        assert lag[f"{topics['kpi_breakdown']}:0"] == 0
        assert lag[f"{topics['trip_geo']}:0"] == 0
