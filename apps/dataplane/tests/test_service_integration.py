"""End-to-end wiring tests over the REAL object graph.

Every test here builds real collaborators: a real :class:`HotStore`, a real
:class:`DuckStore` on a ``tmp_path`` database file, and (where the durable record is
part of the claim) a real :class:`MongoArchive` against a throwaway
``dataplane_test_<uuid4hex>`` database that is dropped in teardown. Mongo-backed tests
skip cleanly when mongod is unreachable.

Why this file exists at all: the previous round shipped a completely dead restart path
with 244 green tests, because the fix lived in one module, the caller never called it,
and a *fake* in the wiring tests implemented a method the real class did not have. A
unit test against a double cannot see a missing seam. These tests can — the only doubles
used are SUBCLASSES of the real stores that make one method fail.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Dict, List

import pytest

from apps.config import kafka_config
from apps.dataplane.service import DataplaneService
from apps.dataplane.store.duck import DuckStore, DuckStoreError

pymongo = pytest.importorskip("pymongo")

CLOCK = "Mon, 01 Jun 2026 08:00:00 GMT"
CLOCK2 = "Mon, 01 Jun 2026 09:00:00 GMT"


# ── real mongo, throwaway database ──────────────────────────────────────────


def _mongo_client():
    from pymongo import MongoClient

    from apps.config import kpi_sink_settings

    uri = kpi_sink_settings.get("mongo_uri")
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=1000)
    return MongoClient(
        kpi_sink_settings["mongo_host"],
        int(kpi_sink_settings["mongo_port"]),
        serverSelectionTimeoutMS=1000,
    )


@pytest.fixture(scope="module")
def mongo_client():
    try:
        client = _mongo_client()
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Mongo unreachable ({type(exc).__name__}: {exc})")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def archive(mongo_client):
    from apps.dataplane.archive.mongo import MongoArchive

    db_name = f"dataplane_test_{uuid.uuid4().hex}"
    arc = MongoArchive(client=mongo_client, db_name=db_name)
    arc.ensure_indexes()
    try:
        yield arc
    finally:
        mongo_client.drop_database(db_name)


# ── helpers ─────────────────────────────────────────────────────────────────


def truck_loc(agent: str, lon: float, lat: float, haulier: str, clock: str = CLOCK) -> dict:
    return {
        "type": "truck_loc",
        "sim_clock": clock,
        "truck_agent_id": agent,
        "lon": lon,
        "lat": lat,
        "haul_state": "loaded_in_transit",
        "haulier_id": haulier,
    }


def feed(service: DataplaneService, logical: str, run_id: str, payload: dict) -> None:
    """Dispatch exactly the way DataplaneConsumer does — through ``service.handlers``."""
    service.handlers[logical](logical, run_id, payload)


def frame_rows(duck: DuckStore, run_id: str) -> List[Dict[str, Any]]:
    return duck.query(
        "SELECT frame_idx, slot, lng, haulier FROM frames WHERE run_id = ? "
        "ORDER BY frame_idx, slot",
        [run_id],
    )


def service_over(db_path, **kwargs) -> DataplaneService:
    return DataplaneService(db_path=str(db_path), enable_http=False, **kwargs)


# ── (a) a process restart over the same database ────────────────────────────


def test_a_restart_reuses_no_frame_idx_and_keeps_the_haulier_code_book(tmp_path, archive):
    """Two real services over ONE db file: no fused frames, no relabelled hauliers.

    Before the fix: the second process built ``HotStore()`` unseeded, restarted frame_idx
    at 0, and its first capture merged with the first process's frame 0 (one truck read
    back as two, at two positions, at one instant). Mongo's deterministic ``_id`` then
    dropped the second frame silently, so the post-restart positions were archived
    nowhere. Meanwhile ``restore_run_codes`` was dead three ways, so a haulier that
    reported in a different order after the restart got a different uint8 — with every
    pre-restart frame still encoded the old way.
    """
    db = tmp_path / "dp.duckdb"
    run_id = "RUN1"

    # ── process 1: ACME reports first, BOLT second ──────────────────────────
    s1 = service_over(db, archive=archive)
    try:
        feed(s1, "trip_geo", run_id, truck_loc("t1", 4.0, 51.0, "ACME"))
        feed(s1, "trip_geo", run_id, truck_loc("t2", 9.0, 52.0, "BOLT"))
        assert s1.capture_frames() == 1
        feed(s1, "trip_geo", run_id, truck_loc("t1", 4.5, 51.0, "ACME"))
        assert s1.capture_frames() == 1
        for i in range(3):
            feed(s1, "kpi", run_id, {"metric": f"m{i}", "value": i, "sim_clock": CLOCK})
        feed(
            s1, "kpi_breakdown", run_id,
            {"scope": "haulier", "sim_clock": CLOCK, "final": False,
             "breakdown": {"entities": [{"id": "ACME", "empty_km": 1.0}]}},
        )
        pre_codes = json.loads(s1.duck.get_run_meta(run_id)["haulier_codes"])
        pre_frames = frame_rows(s1.duck, run_id)
        assert pre_codes == {"ACME": 1, "BOLT": 2}
        assert sorted({r["frame_idx"] for r in pre_frames}) == [0, 1]
    finally:
        s1.stop(timeout=2.0)

    # ── process 2: SAME db file, BOLT now reports first ─────────────────────
    s2 = service_over(db, archive=archive)
    try:
        feed(s2, "trip_geo", run_id, truck_loc("t2", 9.5, 52.0, "BOLT", CLOCK2))
        feed(s2, "trip_geo", run_id, truck_loc("t1", 5.0, 51.0, "ACME", CLOCK2))
        feed(s2, "trip_geo", run_id, truck_loc("t3", 6.0, 51.5, "CEDA", CLOCK2))
        assert s2.capture_frames() == 1
        feed(s2, "trip_geo", run_id, truck_loc("t3", 6.5, 51.5, "CEDA", CLOCK2))
        assert s2.capture_frames() == 1
        feed(s2, "kpi", run_id, {"metric": "m9", "value": 9, "sim_clock": CLOCK2})
        feed(
            s2, "run_status", run_id,
            {"status": "COMPLETED", "run_id": run_id, "simulation_active": False},
        )
        assert s2.drain_dumps() == 1

        duck = s2.duck
        rows = frame_rows(duck, run_id)
        meta = duck.get_run_meta(run_id)
        post_codes = json.loads(meta["haulier_codes"])
        post_slots = json.loads(meta["slot_map"])

        # 1. No frame_idx is ever reused: each index holds exactly one snapshot, i.e.
        #    no (frame_idx, slot) pair occurs twice, and the indices are dense from 0.
        by_idx: Dict[int, List[int]] = {}
        for row in rows:
            by_idx.setdefault(int(row["frame_idx"]), []).append(int(row["slot"]))
        for idx, slots in by_idx.items():
            assert len(slots) == len(set(slots)), f"frame {idx} fused two snapshots: {slots}"
        assert sorted(by_idx) == list(range(len(by_idx)))
        # Two captures on each side of the restart. The terminal capture adds nothing here
        # because no truck moved after the last sweep: an unchanged run is never snapshotted
        # twice (it used to be, once per terminal replay).
        assert len(by_idx) == 4
        # the pre-restart frames are untouched by the second process
        assert frame_rows(duck, run_id)[: len(pre_frames)] == pre_frames

        # 2. A haulier reporting in a different order keeps its pre-restart code…
        assert post_codes["ACME"] == pre_codes["ACME"]
        assert post_codes["BOLT"] == pre_codes["BOLT"]
        assert post_codes["CEDA"] not in (pre_codes["ACME"], pre_codes["BOLT"])
        # …and the frames written AFTER the restart encode it that way.
        acme_slot = post_slots["t1"]
        post_acme = [
            r for r in rows if int(r["frame_idx"]) >= 2 and int(r["slot"]) == acme_slot
        ]
        assert post_acme and {int(r["haulier"]) for r in post_acme} == {pre_codes["ACME"]}
        # slots are stable across the restart too
        assert post_slots["t1"] == 0 and post_slots["t2"] == 1

        # 3. Every frame written on BOTH sides of the restart is in Mongo.
        archived = {int(d["frame_idx"]) for d in archive.frames.find({"run_id": run_id})}
        assert archived == set(by_idx)
        summary = archive.run_summary(run_id)
        assert summary["complete"] is True
        # 4. The dump outcome is recorded (set_export_state used to be dead code).
        assert (duck.get_export_state(run_id) or {}).get("status") == "exported"
    finally:
        s2.stop(timeout=2.0)


# ── (b) the archive probe must not stall ingest or /health ──────────────────


class _SlowPingArchive:
    """Stands in for a mongod that accepts the connection and answers slowly."""

    def __init__(self, delay: float = 3.0) -> None:
        self.delay = delay
        self.runs = None

    def ping(self) -> bool:
        time.sleep(self.delay)
        return True

    def ensure_indexes(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_a_slow_mongo_probe_blocks_neither_ingest_nor_health(tmp_path):
    """Regression: the probe held _init_lock across ping(), and every truck_loc took it."""
    service = service_over(
        tmp_path / "dp.duckdb", archive=None, archive_factory=lambda: _SlowPingArchive(3.0)
    )
    try:
        service.hot  # warm the stores so the measurement is of the lock, not of setup
        probe = threading.Thread(target=lambda: service.archive, daemon=True)
        probe.start()
        time.sleep(0.3)  # the probe is now inside ping()

        t0 = time.monotonic()
        feed(service, "trip_geo", "R", truck_loc("t1", 4.0, 51.0, "ACME"))
        ingest_s = time.monotonic() - t0

        t1 = time.monotonic()
        payload = service.health_payload()
        health_s = time.monotonic() - t1

        assert ingest_s < 0.5, f"on_trip_geo waited {ingest_s:.2f}s on the mongo probe"
        assert health_s < 0.5, f"health_payload waited {health_s:.2f}s on the mongo probe"
        assert payload["service"] == "dataplane"
        probe.join(timeout=10)
        assert service._archive is not None  # …and the probe still published its result
    finally:
        service.stop(timeout=2.0)


# ── (c) a failing frame writer ──────────────────────────────────────────────


class BrokenFrameDuck(DuckStore):
    """A REAL DuckStore whose frame writes fail (subclass, never a duck-typed double)."""

    def write_frame(self, run_id, frame):
        raise DuckStoreError("disk full")


def test_frame_write_failures_are_loud_and_do_not_burn_frame_indices(tmp_path):
    db = tmp_path / "dp.duckdb"
    duck = BrokenFrameDuck(str(db))
    service = service_over(db, duck=duck, archive=None, archive_factory=lambda: None)
    try:
        feed(service, "trip_geo", "R", truck_loc("t1", 4.0, 51.0, "ACME"))
        for i in range(40):
            feed(service, "trip_geo", "R", truck_loc("t1", 4.0 + i * 0.1, 51.0, "ACME"))
            assert service.capture_frames() == 0  # capture_frames logs and continues

        assert service.counters["frames_written"] == 0
        assert service.counters["frame_write_failures"] == 40
        # No index was burned: the first frame that lands is still 0, so the archive's
        # dense-prefix check is not defeated by a 0..39 hole.
        assert service.hot.next_frame_idx("R") == 0
        assert duck.run_frame_count("R") == 0
    finally:
        service.stop(timeout=2.0)


def test_the_frame_path_recovers_and_reuses_the_rolled_back_index(tmp_path):
    """After the failure clears, capture continues at the index that was handed back."""
    db = tmp_path / "dp.duckdb"

    class FlakyFrameDuck(DuckStore):
        fail = True

        def write_frame(self, run_id, frame):
            if self.fail:
                raise DuckStoreError("disk full")
            return super().write_frame(run_id, frame)

    duck = FlakyFrameDuck(str(db))
    service = service_over(db, duck=duck, archive=None, archive_factory=lambda: None)
    try:
        feed(service, "trip_geo", "R", truck_loc("t1", 4.0, 51.0, "ACME"))
        assert service.capture_frames() == 0
        duck.fail = False
        feed(service, "trip_geo", "R", truck_loc("t1", 4.2, 51.0, "ACME"))
        assert service.capture_frames() == 1
        assert duck.frame_range("R") == (0, 0)
    finally:
        service.stop(timeout=2.0)


# ── (d) the write path and the offset gate, over the REAL object graph ──────
#
# Everything below drives a real DataplaneService through a real DataplaneConsumer against a
# real DuckStore (subclassed only to make ONE method fail). No handler is called directly and
# no store is a double, because the two bugs these tests pin — an inline write on the poll
# thread, and a commit gate made of failure counters — both lived in the seam between those
# three objects, which is exactly where a unit test with doubles cannot look.


class _KafkaSpy:
    """A Kafka client that records exactly which offsets were committed."""

    def __init__(self, messages=()):
        self.queue: List[Any] = list(messages)
        self.committed: List[Any] = []
        self.subscribed: List[Any] = []
        self.paused: List[Any] = []
        self.closed = False
        self._served: dict = {}

    def get_watermark_offsets(self, tp, timeout=None, cached=False):
        """The broker's head for this topic: one past the highest offset it holds.

        Served and still-queued records both count — a record this client has not delivered
        yet is exactly the backlog ``broker_lag`` exists to see.
        """
        highest = self._served.get(tp.topic, -1)
        for msg in list(self.queue):
            try:
                if msg.topic() == tp.topic:
                    highest = max(highest, int(msg.offset()))
            except Exception:  # noqa: BLE001 - a message that cannot report is not countable
                continue
        return (0, highest + 1)

    def subscribe(self, topics):
        self.subscribed.append(list(topics))

    def assignment(self):
        return []

    def pause(self, parts):
        self.paused.append(list(parts))

    def resume(self, parts):
        pass

    def poll(self, timeout=0.0):
        if not self.queue:
            return None
        msg = self.queue.pop(0)
        try:
            self._served[msg.topic()] = max(self._served.get(msg.topic(), -1), int(msg.offset()))
        except Exception:  # noqa: BLE001
            pass
        return msg

    def commit(self, *args, **kwargs):
        offsets = kwargs.get("offsets")
        if offsets is None:
            self.committed.append(("*", "*", "blind"))
            return
        for tp in offsets:
            self.committed.append((tp.topic, tp.partition, tp.offset))

    def close(self):
        self.closed = True

    # -- assertions ---------------------------------------------------------

    def commit_point(self, topic: str) -> int:
        """The highest offset committed for ``topic``, i.e. the next one Kafka would serve."""
        points = [o for t, _p, o in self.committed if t == topic and isinstance(o, int)]
        return max(points) if points else -1


class _Msg:
    """A message that can report its partition and offset, the way librdkafka does."""

    def __init__(self, topic, value, key=b"R", partition=0, offset=0):
        self._topic, self._key, self._partition, self._offset = topic, key, partition, offset
        self._value = json.dumps(value).encode()

    def topic(self):
        return self._topic

    def key(self):
        return self._key

    def value(self):
        return self._value

    def error(self):
        return None

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset


BROKER = kafka_config["topics"]


def _entities(n, prefix="t"):
    return [
        {"id": f"{prefix}{i}", "haulier_id": f"h{i % 5}", "haulier_name": f"H{i % 5}",
         "num_orders_completed": i, "empty_km": i * 1.5, "loaded_km": i * 2.5,
         "total_km": i * 4.0, "empty_ratio": 0.375, "active_hours": 5.5,
         "orders_per_day": 3.2, "dual_cycle_count": i % 3, "chain_opportunities": i % 7,
         "dual_cycle_rate": 0.1, "num_trucks": 1}
        for i in range(n)
    ]


def _bd(offset, hour=8, n=500):
    return _Msg(
        BROKER["kpi_breakdown"],
        {"scope": "truck", "sim_clock": f"Mon, 01 Jun 2026 {hour:02d}:00:00 GMT",
         "final": False, "breakdown": {"entities": _entities(n)}},
        offset=offset,
    )


def _loc(offset, agent="t1", lon=4.0):
    return _Msg(BROKER["trip_geo"], truck_loc(agent, lon, 51.0, "ACME"), offset=offset)


def _kpi(offset, metric="m"):
    return _Msg(BROKER["kpi"], {"metric": metric, "value": 1.0, "sim_clock": CLOCK}, offset=offset)


def _terminal(offset):
    return _Msg(
        BROKER["run_status"],
        {"status": "COMPLETED", "run_id": "R", "simulation_active": False},
        offset=offset,
    )


def _wire(tmp_path, duck=None, messages=(), **kwargs) -> tuple:
    kafka = _KafkaSpy(messages)
    service = DataplaneService(
        db_path=str(tmp_path / "dp.duckdb"),
        duck=duck,
        archive=None,
        archive_factory=lambda: None,
        enable_http=False,
        consumer_factory=lambda conf: kafka,
        **kwargs,
    )
    service.consumer._ensure_consumer()
    return service, kafka


def _pump(service, kafka):
    """Poll every queued message the way ``DataplaneConsumer.run`` does. Returns the gaps."""
    consumer = service.consumer
    gaps: List[float] = []
    last = time.monotonic()
    while True:
        msg = kafka.poll(0.0)
        if msg is None:
            break
        consumer.handle_message(msg)
        now = time.monotonic()
        gaps.append(now - last)
        last = now
        consumer.commit_safe(force=True)
    return gaps


def test_a_breakdown_storm_is_affordable_on_the_poll_thread(tmp_path):
    """The measurement the whole asynchronous write path was built to avoid — re-taken.

    One 500-entity truck-scope snapshot written inline used to cost the poll thread
    **3.0–3.9 s** (p50 3.12 s) and a 7-day run publishes ~196 of them, so the consumer could
    never catch up. That was never the synchronous write; it was ``executemany`` issuing one
    upserting INSERT per entity. The columnar path in ``store/duck.py`` took 35 000 breakdown
    rows from 166 s to 0.255 s, and one snapshot now measures ~25 ms against a live arrival
    rate of 132 msg/s. This test pins that: the same stream that took 12.62 s with a 3.166 s
    worst-case gap, written synchronously with nothing between the handler and the store.
    """
    service, kafka = _wire(tmp_path)
    try:
        # Per-topic offsets, the way single-partition Kafka actually numbers them.
        loc_offset = bd_offset = 0
        for snap in range(4):
            for i in range(500):
                kafka.queue.append(_loc(loc_offset, agent=f"t{i}", lon=4.0 + snap * 0.01))
                loc_offset += 1
            kafka.queue.append(_bd(bd_offset, hour=8 + snap))
            bd_offset += 1

        service.hot  # build the lazy stores before the measurement starts
        started = time.monotonic()
        gaps = _pump(service, kafka)
        wall = time.monotonic() - started

        assert len(gaps) == 2004
        assert max(gaps) < 0.5, f"a 500-entity snapshot cost the poll thread {max(gaps):.3f}s"
        assert wall < 10.0, f"draining 2004 messages took {wall:.2f}s"
        # …and every row is durable the moment its handler returned.
        assert service.duck.breakdown_row_count("R") == 2000
        assert service.consumer.stats()["uncommitted"] == 0
    finally:
        service.stop(timeout=2.0)


def test_a_transient_breakdown_failure_holds_the_offset_and_the_rows_land_on_replay(
    tmp_path,
):
    """ROOT 2: the rows must be in DuckDB afterwards — asserted on contents, not counters.

    Before: the entities were never buffered anywhere, so a failed snapshot existed only in
    Kafka; the next successful snapshot cleared the writer's counter and the offsets committed
    straight past the shredded one (measured: DuckDB held the 10:00 and 12:00 snapshots and
    nothing at 11:00, with ``/health ok=True degraded=[]``). Kafka is the buffer now: the
    write raised, so the offset never moved, so the broker still has all three.
    """

    class FlakyBreakdownDuck(DuckStore):
        broken = True

        def write_breakdown_batch(self, run_id, snapshots):
            if self.broken:
                raise DuckStoreError("TransactionException: write-write conflict")
            return super().write_breakdown_batch(run_id, snapshots)

    duck = FlakyBreakdownDuck(str(tmp_path / "dp.duckdb"))
    messages = [_bd(i, hour=10 + i, n=5) for i in range(3)]
    service, kafka = _wire(tmp_path, duck=duck, messages=list(messages))
    try:
        _pump(service, kafka)

        # 1. The rows are not in DuckDB — and not lost either: they are still in Kafka.
        assert duck.breakdown_row_count("R") == 0

        # 2. The offset did NOT advance past them. The commit point is the lowest unwritten
        #    offset, so a restart replays exactly these three records.
        assert kafka.commit_point(BROKER["kpi_breakdown"]) == 0
        assert service.consumer.stats()["commit_lag"][f"{BROKER['kpi_breakdown']}:0"] == 3

        # 3. /health is not green while the store is refusing rows.
        payload = service.health_payload()
        assert payload["ok"] is False
        assert "ingest_discarding" in payload["degraded"]

        # 4. The store recovers and the broker replays: the rows land and the offsets resume
        #    on their own — no counter had to be cleared by a matching success.
        duck.broken = False
        kafka.queue.extend(messages)
        _pump(service, kafka)
        clocks = sorted(
            r["sim_clock"].isoformat()
            for r in duck.query("SELECT DISTINCT sim_clock FROM kpi_breakdown_rows WHERE run_id='R'")
        )
        assert clocks == [
            "2026-06-01T10:00:00", "2026-06-01T11:00:00", "2026-06-01T12:00:00",
        ], "a snapshot was shredded"
        assert kafka.commit_point(BROKER["kpi_breakdown"]) == 3
        assert service.consumer.stats()["uncommitted"] == 0
    finally:
        service.stop(timeout=2.0)


def test_a_failed_terminal_capture_does_not_evict_and_the_offsets_resume(tmp_path):
    """ROOT 4: eviction happens only after the capture AND the meta write have succeeded.

    Before: ``on_run_status`` captured, persisted and evicted unconditionally, so a failed
    final ``write_frame`` left ``_write_failures['frames'] = 1`` with no resident run — and
    nothing could ever clear it. Measured over 500 subsequent healthy kpi messages:
    ``commits=0, commits_blocked=500, commit_ready()=False``, permanently, with
    ``/health ok=True degraded=[]``.
    """

    class FlakyFrameDuck(DuckStore):
        broken = True

        def write_frame(self, run_id, frame):
            if self.broken:
                raise DuckStoreError("IO Error: disk full")
            return super().write_frame(run_id, frame)

    duck = FlakyFrameDuck(str(tmp_path / "dp.duckdb"))
    service, kafka = _wire(tmp_path, duck=duck, messages=[_loc(0), _terminal(1)])
    try:
        _pump(service, kafka)

        # 1. The run is STILL RESIDENT: without the slab the failure could never be retried.
        assert service.hot.resident_runs() == ["R"]
        assert duck.run_frame_count("R") == 0
        assert (duck.get_run_meta("R") or {}).get("status") is None

        # 2. The terminal record's offset is held, so Kafka will replay it.
        assert kafka.commit_point(BROKER["run_status"]) == 1
        assert service.consumer.stats()["commit_lag"][f"{BROKER['run_status']}:0"] == 1

        # 3. /health is not green while the run cannot be closed.
        stuck = service.health_payload()
        assert stuck["ok"] is False
        assert "ingest_discarding" in stuck["degraded"]

        # 4. The store recovers. The broker replays the terminal, the run closes in order,
        #    and 500 later healthy messages commit — the gate was never latched.
        duck.broken = False
        kafka.queue.append(_terminal(1))
        kafka.queue.extend(_kpi(2 + i, metric=f"m{i}") for i in range(500))
        _pump(service, kafka)

        assert duck.run_frame_count("R") == 1, "the final positions never landed"
        assert duck.get_run_meta("R")["status"] == "completed"
        assert service.hot.resident_runs() == []       # evicted, but only now
        assert service.pending_dumps() == ["R"]
        assert duck.kpi_row_count("R") == 500
        assert service.consumer.stats()["commits"] > 0
        assert kafka.commit_point(BROKER["kpi"]) == 502
        assert kafka.commit_point(BROKER["run_status"]) == 2
        assert service.consumer.stats()["uncommitted"] == 0
    finally:
        service.stop(timeout=2.0)


def test_offsets_keep_advancing_after_a_writer_stops_producing(tmp_path):
    """ROOT 2 regression: a writer whose traffic ended used to latch the gate forever.

    ``frames`` only writes while a run is resident, so once the run was evicted no frame
    success could ever clear its streak. Measured before: 500 perfectly healthy kpi messages
    produced ``commits=0, commits_blocked=500, uncommitted=500`` and ``/health ok=True``.
    There is no per-writer state left to be stuck in.
    """

    class FlakyFrameDuck(DuckStore):
        broken = True

        def write_frame(self, run_id, frame):
            if self.broken:
                raise DuckStoreError("IO Error")
            return super().write_frame(run_id, frame)

    duck = FlakyFrameDuck(str(tmp_path / "dp.duckdb"))
    service, kafka = _wire(tmp_path, duck=duck, messages=[_loc(0)])
    try:
        _pump(service, kafka)
        service.capture_frames()          # the frame write fails
        duck.broken = False
        service.evict_run("R")            # …and then that writer stops producing entirely
        assert service.hot.resident_runs() == []

        kafka.queue.extend(_kpi(1 + i, metric=f"k{i}") for i in range(500))
        _pump(service, kafka)

        assert duck.kpi_row_count("R") == 500
        assert service.consumer.stats()["commits"] > 0
        assert kafka.commit_point(BROKER["kpi"]) == 501
        assert service.consumer.stats()["uncommitted"] == 0
        assert service.health_payload()["ok"] is True   # nothing is owed: green is the truth

        # The other half of the same rule: when data really IS un-durable the offsets stop at
        # it and the verdict goes red, so "green" above is a measurement and not a blind spot.
        service._duck.write_kpi_events = _explode
        kafka.queue.extend(_kpi(501 + i, metric=f"z{i}") for i in range(3))
        _pump(service, kafka)
        assert kafka.commit_point(BROKER["kpi"]) == 501, "committed past rows nothing wrote"
        payload = service.health_payload()
        assert payload["ok"] is False
        assert "ingest_discarding" in payload["degraded"]
    finally:
        service.stop(timeout=2.0)


def _explode(*args, **kwargs):
    raise DuckStoreError("kpi table wedged")


class _BlockingKafkaSpy(_KafkaSpy):
    """Like ``_KafkaSpy`` but ``poll`` waits instead of spinning, for the threaded test."""

    def poll(self, timeout=0.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self.queue:
                return self.queue.pop(0)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)


def test_a_store_outage_loses_nothing_however_long_it_lasts(tmp_path):
    """The stated cost of never dropping: a store that is down stops its own partition.

    What this replaces: a writer that classified a non-``DuckStoreError`` as "this payload can
    never be written" and PARKED the whole unit after three attempts, releasing its offsets —
    so a store malfunction that did not present as ``DuckStoreError`` (duckdb 1.5.3's pandas
    analyzer raising ``AttributeError`` from inside ``register()`` is a measured one) silently
    destroyed every record in the unit. There is no quarantine and no attempt counter: the
    write either returned or it did not.
    """
    duck = DuckStore(str(tmp_path / "dp.duckdb"))
    service, kafka = _wire(tmp_path, duck=duck)
    try:
        service._duck.write_breakdown_batch = _explode
        for attempt in range(12):                 # the broker redelivers, forever
            kafka.queue.append(_bd(0, hour=9, n=3))
            _pump(service, kafka)
        assert duck.breakdown_row_count("R") == 0
        assert kafka.commit_point(BROKER["kpi_breakdown"]) == 0
        assert service.health_payload()["store"]["rows_dropped"] == 0   # nothing was lost

        # It was only stuck, never lost: when the store recovers the replay writes.
        del service._duck.write_breakdown_batch
        kafka.queue.append(_bd(0, hour=9, n=3))
        _pump(service, kafka)
        assert duck.breakdown_row_count("R") == 3
        assert kafka.commit_point(BROKER["kpi_breakdown"]) == 1
    finally:
        service.stop(timeout=2.0)


def test_the_started_process_writes_everything_it_consumes_and_commits(tmp_path):
    """The whole graph with real threads: supervisor -> consumer task -> stores -> frames task.

    Nothing here calls a handler or a store directly. It is the only test that can prove the
    consumer task is actually wired to the handlers that write — the sort of seam that shipped
    dead last round under 244 green tests.
    """
    duck = DuckStore(str(tmp_path / "dp.duckdb"))
    kafka = _BlockingKafkaSpy()
    service = DataplaneService(
        db_path=str(tmp_path / "dp.duckdb"),
        duck=duck,
        archive=None,
        archive_factory=lambda: None,
        enable_http=False,
        consumer_factory=lambda conf: kafka,
        frame_interval_s=0.05,
        reconcile_interval_s=0.05,
    )
    service.start()
    try:
        offset = 0
        for i in range(50):
            kafka.queue.append(_kpi(offset, metric=f"m{i}"))
            offset += 1
        kafka.queue.append(_bd(offset, hour=9, n=200))
        offset += 1
        for i in range(20):
            kafka.queue.append(_loc(offset, agent=f"t{i}", lon=4.0 + i * 0.01))
            offset += 1
        terminal_offset = offset
        kafka.queue.append(_terminal(offset))

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if (duck.kpi_row_count("R") == 50 and duck.breakdown_row_count("R") == 200
                    and service.pending_dumps() == ["R"]):
                break
            time.sleep(0.05)

        assert duck.kpi_row_count("R") == 50
        assert duck.breakdown_row_count("R") == 200
        assert duck.get_run_meta("R")["status"] == "completed"
        # The final capture ran, and it ran BEFORE the eviction: the last frame holds all
        # twenty trucks' final positions.
        assert duck.run_frame_count("R") >= 1
        last = duck.frame_range("R")[1]
        assert len(frame_rows(duck, "R")) >= 20
        assert len([r for r in frame_rows(duck, "R") if int(r["frame_idx"]) == last]) == 20
        assert service.hot.resident_runs() == []
        assert service.counters["runs_finalized"] == 1

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and service.consumer.stats()["uncommitted"]:
            time.sleep(0.05)
        assert service.consumer.stats()["uncommitted"] == 0
    finally:
        service.stop(timeout=3.0)

    # Every offset the process consumed is durable, so every one of them is committed.
    assert kafka.commit_point(BROKER["kpi"]) == 50
    assert kafka.commit_point(BROKER["run_status"]) == terminal_offset + 1
    assert kafka.closed is True


def test_a_hard_stop_commits_nothing_it_did_not_write(tmp_path):
    """A hard kill costs time, never data.

    The store is broken and stays broken, so the records after the outage begins are never
    written. Their offsets must not appear in what was committed, and the broker must still
    have them when the process comes back.
    """
    duck = DuckStore(str(tmp_path / "dp.duckdb"))
    messages = [_kpi(i, metric=f"m{i}") for i in range(5)]
    service, kafka = _wire(tmp_path, duck=duck, messages=messages)
    try:
        # offsets 0 and 1 are written and committed; 2, 3 and 4 never are.
        service.consumer.handle_message(kafka.poll(0.0))
        service.consumer.handle_message(kafka.poll(0.0))
        service.consumer.commit_safe(force=True)
        assert kafka.commit_point(BROKER["kpi"]) == 2

        service._duck.write_kpi_events = _explode
        _pump(service, kafka)
        assert duck.kpi_row_count("R") == 2
    finally:
        service.stop(timeout=1.0)

    assert kafka.commit_point(BROKER["kpi"]) == 2, "committed past rows that were never written"
    assert kafka.closed is True


# ── (e) a headless run (the OpenRide default) ───────────────────────────────


def test_a_headless_run_reaches_a_terminal_state_and_is_archived(tmp_path, archive):
    """No truck_loc at all: the run must still close, or archive lag grows forever."""
    from apps.dataplane.archive.mongo import MongoArchive

    service = service_over(tmp_path / "dp.duckdb", archive=archive)
    try:
        for i in range(20):
            feed(service, "kpi", "HL", {"metric": f"m{i}", "value": i, "sim_clock": CLOCK})
        feed(
            service, "kpi_breakdown", "HL",
            {"scope": "haulier", "sim_clock": CLOCK, "final": True,
             "breakdown": {"entities": [{"id": "ACME", "empty_km": 873.4, "orders": 412}]}},
        )
        feed(
            service, "run_status", "HL",
            {"status": "COMPLETED", "run_id": "HL", "simulation_active": False},
        )
        assert service._hot is None, "a headless run has no hot tier — that is the point"

        duck = service.duck
        meta = duck.get_run_meta("HL")
        assert meta is not None and meta["status"] == "completed"
        assert MongoArchive.run_is_terminal(duck, "HL") is True

        # A real reconcile sweep must now see it as archivable, and CLOSE it. (Four
        # consecutive sweeps used to leave complete=False and pending_run_ids=['HL'].)
        result = service.reconcile_once()
        assert "HL" in result["run_ids"]
        assert "HL" in archive.archived_run_ids()
        assert archive.run_summary("HL")["complete"] is True
        assert archive.kpi.count_documents({"run_id": "HL"}) == 20

        # The queued terminal dump drains too (the archive task does this first).
        assert service.drain_dumps() == 1
        payload = service.health_payload()
        assert payload["archive"]["lag_runs"] == 0
        assert payload["archive"]["pending_run_ids"] == []
        assert payload["ok"] is True

        # A second sweep is a no-op: the lag signal stays clean instead of growing.
        assert service.reconcile_once()["dumped"] == 0
        assert service.health_payload()["archive"]["lag_runs"] == 0
    finally:
        service.stop(timeout=2.0)


def test_a_terminal_status_never_blanks_the_code_book(tmp_path):
    """A status-only write used to replace the maps with {} — the frames' only dictionary."""
    db = tmp_path / "dp.duckdb"
    s1 = service_over(db, archive=None, archive_factory=lambda: None)
    try:
        feed(s1, "trip_geo", "R", truck_loc("t1", 4.0, 51.0, "ACME"))
        s1.capture_frames()
        assert json.loads(s1.duck.get_run_meta("R")["haulier_codes"]) == {"ACME": 1}
    finally:
        s1.stop(timeout=2.0)

    # A fresh process that sees ONLY the terminal status for that run.
    s2 = service_over(db, archive=None, archive_factory=lambda: None)
    try:
        feed(
            s2, "run_status", "R",
            {"status": "COMPLETED", "run_id": "R", "simulation_active": False},
        )
        meta = s2.duck.get_run_meta("R")
        assert meta["status"] == "completed"
        assert json.loads(meta["haulier_codes"]) == {"ACME": 1}
        assert json.loads(meta["slot_map"]) == {"t1": 0}
    finally:
        s2.stop(timeout=2.0)


def test_the_hot_tier_is_always_built_with_a_working_frame_idx_seed(tmp_path):
    """The exact wiring that was missing: HotStore() with no seed at service.py:215."""
    db = tmp_path / "dp.duckdb"
    s1 = service_over(db, archive=None, archive_factory=lambda: None)
    try:
        feed(s1, "trip_geo", "R", truck_loc("t1", 4.0, 51.0, "ACME"))
        for _ in range(5):
            feed(s1, "trip_geo", "R", truck_loc("t1", 4.1, 51.0, "ACME"))
            s1.capture_frames()
        assert s1.duck.frame_range("R") == (0, 4)
    finally:
        s1.stop(timeout=2.0)

    s2 = service_over(db, archive=None, archive_factory=lambda: None)
    try:
        # No run_meta lookup involved: the seed callable alone must carry this.
        assert s2._hot is None
        assert s2.hot._frame_idx_seed is not None
        assert s2._seed_frame_idx("R") == 5
        assert s2._seed_frame_idx("NEVER_SEEN") is None
        s2.hot.update_position("R", "t1", 4.2, 51.0, "idle", "ACME", 0.0)
        assert s2.hot.next_frame_idx("R") == 5
    finally:
        s2.stop(timeout=2.0)


def test_the_seed_callable_cannot_deadlock_against_the_hot_lock(tmp_path):
    """It runs under the hot lock, so it must read _duck directly, never the property.

    Threads race first-touch of both lazy properties while positions stream in; taking
    _init_lock from inside the hot lock would wedge this test rather than fail it, so it
    is run with a hard timeout.
    """
    service = service_over(tmp_path / "dp.duckdb", archive=None, archive_factory=lambda: None)
    errors: List[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(40):
                feed(service, "trip_geo", f"R{n % 3}", truck_loc(f"t{i}", 4.0, 51.0, "ACME"))
                service.capture_frames()
                service.health_payload()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(6)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "deadlock: seed callable took _init_lock under the hot lock"
        assert not errors, f"threads raised: {errors}"
        assert service.counters["frames_written"] > 0
    finally:
        service.stop(timeout=2.0)


def test_a_run_nobody_closed_is_not_counted_as_archive_lag(tmp_path, archive):
    """An OPEN archive summary for a run that is not producing is not lag — it is unfinished.

    CLAUDE.md §8 names this OpenRide's most common abnormal state: a run started as a plain
    session child dies mid-sim, so no terminal ``run_status`` is ever published. The reconcile
    sweep dumps what DuckDB holds and writes ``complete=False``, and ``archived_run_ids()``
    deliberately excludes such a summary. Keyed on that set, ``pending_run_ids`` held the run
    for the life of the process, ``run_is_pending`` then returned False every cycle so
    ``last_dump_at`` was never refreshed again, and ``archive_lagging`` 503'd an idle box
    permanently — measured at 301 s, at 1 h and at 7 days with every loop live and nothing on
    the box able to clear it.
    """
    service = service_over(
        tmp_path / "dp.duckdb", archive=archive, archive_factory=lambda: archive
    )
    try:
        for i in range(10):
            feed(service, "kpi", "RB", {"metric": f"m{i}", "value": i, "sim_clock": CLOCK})
        service.flush_writes()

        assert archive.run_is_pending(service.duck, "RB") is True     # nothing archived yet
        service.reconcile_once()
        assert archive.run_summary("RB")["complete"] is False         # …swept, still open
        assert "RB" not in archive.archived_run_ids()
        assert archive.run_is_pending(service.duck, "RB") is False    # and owed nothing
        assert service.health_payload()["archive"]["pending_run_ids"] == []

        with service._archive_lock:                                   # seven days pass, idle
            service._archive_state["last_dump_at"] = time.time() - 7 * 24 * 3600
        payload = service.health_payload()
        assert "archive_lagging" not in payload["degraded"]

        # The other half of the rule: rows the archive does not hold ARE lag again.
        feed(service, "kpi", "RB", {"metric": "late", "value": 1, "sim_clock": CLOCK2})
        service.flush_writes()
        assert archive.run_is_pending(service.duck, "RB") is True
    finally:
        service.stop(timeout=2.0)
