"""A partition blocked by a failed write must recover WITHOUT a process restart.

The bug this pins: a partition gets blocked two ways — a handler that raised, and a
pre-commit flush that failed — and both leave the same state. The commit point sits at the
last durable offset while the consumer has already read past those records, so nothing
redelivers them. The rows were not buffered anywhere (deliberately: the broker is the
buffer), and the read position never went back, so the partition stayed blocked for the life
of the process. Measured 2026-08-07: after the store healed in place, 50 of 450 rows were
durable and ``blocked=['kpi','run_status']`` never cleared. Only a restart recovered it.

These tests use the REAL ``DataplaneConsumer`` and its REAL poll loop against a fake broker
that can seek — the earlier doubles could not, which is why a fix depending on redelivery
looked broken and went unwritten for two rounds.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from apps.dataplane.ingest.consumer import DataplaneConsumer


class _Msg:
    def __init__(self, topic: str, key: bytes, value: bytes, offset: int) -> None:
        self._t, self._k, self._v, self._o = topic, key, value, offset

    def topic(self):
        return self._t

    def key(self):
        return self._k

    def value(self):
        return self._v

    def offset(self):
        return self._o

    def partition(self):
        return 0

    def error(self):
        return None


class SeekableBroker:
    """One partition per topic, delivering from a movable read position."""

    def __init__(self) -> None:
        self.log: Dict[str, List[_Msg]] = {}
        self.read_pos: Dict[str, int] = {}
        self.subscribed: List[str] = []
        self.seeks: List[tuple] = []
        self._lock = threading.Lock()

    def produce(self, topic: str, key: str, value: bytes) -> None:
        with self._lock:
            lst = self.log.setdefault(topic, [])
            lst.append(_Msg(topic, key.encode(), value, len(lst)))
            self.read_pos.setdefault(topic, 0)

    def subscribe(self, topics):
        self.subscribed = list(topics)

    def assignment(self):
        class _TP:
            def __init__(self, t):
                self.topic, self.partition = t, 0

        return [_TP(t) for t in self.subscribed]

    def get_watermark_offsets(self, tp, timeout=None, cached=False):
        with self._lock:
            return (0, len(self.log.get(tp.topic, [])))

    def seek(self, tp):
        with self._lock:
            if tp.topic not in self.log:
                raise KeyError(tp.topic)
            self.seeks.append((tp.topic, tp.offset))
            self.read_pos[tp.topic] = max(0, min(int(tp.offset), len(self.log[tp.topic])))

    def poll(self, timeout=0.0):
        with self._lock:
            for t in self.subscribed:
                if self.read_pos.get(t, 0) < len(self.log.get(t, [])):
                    m = self.log[t][self.read_pos[t]]
                    self.read_pos[t] += 1
                    return m
        if timeout:
            time.sleep(min(timeout, 0.01))
        return None

    def commit(self, *a, **k):
        return None

    def pause(self, parts):
        return None

    def resume(self, parts):
        return None

    def close(self):
        return None


class _Task:
    """The supervisor contract the run loop needs, and nothing else."""

    def __init__(self) -> None:
        self.stopping = False

    def heartbeat(self) -> None:
        pass

    def wait(self, timeout: float) -> bool:
        time.sleep(min(timeout, 0.02))
        return self.stopping


def _kpi(metric: str) -> bytes:
    import json

    return json.dumps(
        {"metric": metric, "value": 1.0, "sim_clock": "Mon, 01 Jun 2026 08:00:00 GMT"}
    ).encode()


def _drive(consumer: DataplaneConsumer, seconds: float) -> _Task:
    task = _Task()
    th = threading.Thread(target=consumer.run, args=(task,), daemon=True)
    th.start()
    time.sleep(seconds)
    return task


def test_a_failed_flush_recovers_without_a_restart():
    broker = SeekableBroker()
    state = {"fail": True}
    written: List[str] = []
    buffered: List[str] = []

    def flush():
        if state["fail"]:
            raise RuntimeError("store down")
        written.extend(buffered)
        buffered.clear()

    def on_kpi(topic, run_id, payload):
        buffered.append(str(payload.get("metric")))

    c = DataplaneConsumer(
        handlers={"kpi": on_kpi},
        consumer_factory=lambda *a, **k: broker,
        flush=flush,
        flush_topics=("kpi",),
    )
    # topic_map is physical -> logical; we need the physical name for the broker.
    topic = next(k for k, v in c.topic_map().items() if v == "kpi")
    for i in range(20):
        broker.produce(topic, "R1", _kpi(f"m{i}"))

    task = _drive(c, 2.5)
    try:
        assert "kpi" in c.blocked_topics(), "a failed flush must block its partition"
        assert not written

        state["fail"] = False  # heal in place — no restart
        deadline = time.time() + 12
        while time.time() < deadline and c.blocked_topics():
            time.sleep(0.2)

        assert not c.blocked_topics(), "the partition never unblocked — restart-only recovery"
        assert written, "no rows became durable after the store healed"
        assert broker.seeks, "the consumer never rewound, so nothing could be redelivered"
        assert c.stats()["commit_lag"].get(f"{topic}:0", 0) == 0
    finally:
        task.stopping = True
        time.sleep(0.3)


def test_a_healthy_run_never_rewinds():
    """The retry must be inert when nothing fails — a rewind is data being re-read."""
    broker = SeekableBroker()
    seen: List[str] = []

    c = DataplaneConsumer(
        handlers={"kpi": lambda t, r, p: seen.append(str(p.get("metric")))},
        consumer_factory=lambda *a, **k: broker,
        flush=lambda: None,
        flush_topics=("kpi",),
    )
    topic = "kpi_stream"
    for i in range(30):
        broker.produce(topic, "R1", _kpi(f"m{i}"))

    task = _drive(c, 3.0)
    try:
        assert len(seen) == 30, f"expected each record once, got {len(seen)}"
        assert not c.blocked_topics()
        assert broker.seeks == [], f"rewound with nothing failing: {broker.seeks}"
    finally:
        task.stopping = True
        time.sleep(0.3)
