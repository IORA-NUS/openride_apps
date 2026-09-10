"""The dataplane process: one consumer, one hot tier, one DuckDB, one archive, one /health.

Everything that loops is a :class:`~apps.dataplane.supervisor.SupervisedTask`, so a task that
dies, hangs or crash-loops is restarted *and* makes ``/health`` return 503. That pair is the
whole point of this module — the sink it replaces had neither.

Collaborators (HotStore, DuckStore, MongoArchive) are constructed **lazily** and are
injectable via constructor kwargs, so ``import apps.dataplane.service`` connects to nothing
and tests can pass fakes.

**THE WRITE PATH, IN FULL.**

    drain a poll batch -> the handlers write it to DuckDB -> commit that batch's offsets
    anything raises    -> those offsets are NOT committed -> Kafka redelivers the records

That is the entire design. Durability is guaranteed by the commit happening after the write
returns, not by a ledger tracking which offsets are safe; at-least-once redelivery costs
nothing because every store write here is an idempotent upsert on its primary key. The kpi
handler is the one that defers its write to the end of the batch (:meth:`flush_writes`) —
a DuckDB transaction costs ~6.5 ms whatever it carries, and one per record capped ingest at
~141 msg/s against a live rate of 132. It is not a queue: ``DataplaneConsumer.commit_safe``,
the only place an offset can move, flushes first, and a batch that will not write is dropped
and replayed rather than held.

This replaced an asynchronous write path — a bounded ``WriteQueue``, a ``DuckWriter`` thread,
an ``OffsetLedger`` of deferred marks, a retry backoff, a quarantine, high/low-water
partition pausing and an abandoned-run reaper. That machinery existed for one reason: a
breakdown snapshot written inline once blocked the poll thread for 3.0–3.9 s. It was never
the synchronous write that cost that; it was ``executemany`` issuing one upserting INSERT per
entity. The columnar rewrite in ``store/duck.py`` fixed it at the source — 35 000 breakdown
rows went from 166 s to **0.255 s**, and one 500-entity snapshot written synchronously
through the real ``DuckStore`` measures **25.8 ms** against a measured live arrival rate of
**132 msg/s**. Occasional 25 ms on the poll thread is affordable; the subsystem that avoided
it was the single largest source of defects in this package, three of them data-loss bugs
worse than the stall they replaced (a reaper that finalized and evicted a *live* run; a
quarantine that destroyed every record in a unit when the store malfunctioned; an ``_offer``
that discarded rows *and* committed their offsets). It is gone.

Rules this module still enforces, each of which is a bug that was found in an earlier draft:

1. **No lock an ingest path needs is ever held across network I/O.** The Mongo probe builds,
   pings and indexes with *no* lock held and takes ``_archive_lock`` only to publish the
   outcome — holding ``_init_lock`` across a 3 s ``ping()`` stalled every ``truck_loc`` and
   every ``/health`` behind an unreachable mongod.
2. **The Mongo probe is retried on a backoff.** A single failed probe used to disable the
   archive for the whole life of the process, silently, while ``/health`` still said 200.
   Unreachable-with-pending-runs now fails the health verdict.
3. **A finished run is evicted from the hot tier and never re-admitted**, and an unchanged run
   is not snapshotted. Without the first, the capture sweep appended a full frame of frozen
   positions every second for every completed run — 500 rows/s of garbage that also never
   reached Mongo, so DuckDB and the archive diverged permanently. Eviction alone was not
   enough: ``trip_geo`` and ``run_status`` are unordered topics, so one late position
   re-created the slab and the sweep wrote frames *after* the archive dump had been taken
   (measured: 14 frames in DuckDB, 4 in Mongo, summary already ``complete``).
4. **Eviction happens only after the final capture AND the meta write have both succeeded**
   (:meth:`finalize_run`). Evicting first is what made a single failed final frame write
   unrecoverable: there was no resident run left, so no later frame write could ever happen.
   Both steps propagate their failure out of the handler, so the terminal ``run_status``
   offset is not committed and the broker replays it.
5. **A run's identity survives a process restart.** The hot tier is built with a
   ``frame_idx_seed`` reading ``DuckStore.frame_range``; ``restore_run_codes`` puts the
   persisted haulier code book, slot map and next frame index back before the first position
   is recorded; and ``persist_run_meta`` *merges* rather than replaces those maps. Without
   this, one uint8 means two different hauliers in one run's frames.
6. **A run becomes terminal even with no hot tier.** Headless is the OpenRide default and
   publishes no ``truck_loc``, so a status write conditional on a resident slab left every
   headless run open forever and the archive lag climbing.
7. **There is exactly one frame producer per run at a time.** :meth:`capture_run` holds
   ``_run_lock`` across the snapshot *and* the frame write, so the periodic sweep and a
   terminal arriving on the poll thread cannot both mint a frame for the same instant.

Lock order is strictly: construction (``_init_lock``) -> ``_run_lock`` -> ``HotStore._lock``
-> ``DuckStore``. Nothing acquires ``_run_lock`` while holding the hot tier's lock.
``_archive_lock`` and ``_dump_lock`` are disjoint leaves.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections import deque
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from apps.config import kpi_sink_settings
from apps.dataplane.health import (
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    build_health_payload,
    make_health_server,
)
from apps.dataplane.ingest.consumer import DataplaneConsumer
from apps.dataplane.ingest.parse import (
    coerce_metric_value,
    parse_sim_clock,
    should_export_run_status,
    sim_time_ms,
)
from apps.dataplane.store.hot import RunNotResident
from apps.dataplane.supervisor import Supervisor

logger = logging.getLogger(__name__)

# Facility snapshots are published from several call sites per facility tick
# (`facility/app.py` forces one on enqueue, on gate-service completion and on close), so the
# same facility emits ~2.7 snapshots for the SAME `sim_clock`. The dashboard's
# `facilityStreamSlice` is a last-writer-wins upsert keyed on `facility_id`, so every one but
# the last is overwritten on arrival — measured 7,327 events for 60 facilities across 45
# distinct ticks in a 32 s capture, of which 68% were same-facility-same-tick repeats, and the
# `queue` array they each carry is 82% of facility bytes / ~58% of ALL live SSE traffic.
#
# Coalescing is keyed on `sim_clock`, NOT on a wall-clock window: a tick spans ~700 ms of wall
# time, so a short periodic flush would coalesce nothing, and a flush long enough to span a
# tick would add that latency to every facility. Holding the latest snapshot per facility and
# releasing it when that facility's `sim_clock` advances emits exactly the snapshot the
# reducer settles on, one tick later.
DEFAULT_FACILITY_COALESCE_MAX_HOLD_S = 2.0

DEFAULT_RECONCILE_INTERVAL_S = 300.0
DEFAULT_FRAME_INTERVAL_S = 1.0
# Breakdown scopes the store holds. ``planner`` — one row per cooperation component, the
# project's primary research lens (CLAUDE.md §1) — used to fall through the same silent hole
# as ``lane``; ``scope`` is already in the primary key, so holding it costs nothing.
BREAKDOWN_SCOPES = ("truck", "haulier", "planner", "lane")
# Deliberately NOT stored: lane rows stay on Mongo behind the separate /api/lanes feature.
# Naming them is what makes the skip auditable — anything outside BOTH tuples is a scope this
# store has never heard of, and is counted as a discard rather than dropped in silence.
BREAKDOWN_SCOPES_IGNORED = ()  # lane is stored now: /lanes reads it

# How many runs keep a packed slab in memory. The default of 4 silently evicted a *live*
# run's uncaptured positions as soon as a fifth ``run_id`` appeared on ``trip_geo`` — a
# restart replaying a backlog, or two sims overlapping, is enough. A slab is one small
# numpy block per truck, so a generous bound costs memory only for runs that actually exist.
HOT_MAX_RUNS = 64

# Archive probe backoff: retry a failed Mongo probe rather than latching it off forever.
ARCHIVE_PROBE_BACKOFF_INITIAL_S = 5.0
ARCHIVE_PROBE_BACKOFF_MAX_S = 300.0

# MongoClient timeouts. A mongod that accepts the connection and then stops answering must
# raise, not block the caller forever (the archive task would otherwise stall indefinitely).
MONGO_CLIENT_TIMEOUTS: Dict[str, int] = {
    "serverSelectionTimeoutMS": 3000,
    "connectTimeoutMS": 3000,
    "socketTimeoutMS": 30000,
}


def _utcnow_naive() -> datetime:
    """UTC now as a naive datetime — DuckDB TIMESTAMP columns are timezone-free."""
    return datetime.now(timezone.utc).replace(tzinfo=None)



class _LiveBus:
    """Recent live events per run, for SSE subscribers. Memory only, bounded, lossy by design.

    The dashboard's SSE handlers parse the RAW Kafka payloads (``parseLiveTruckLocMessage`` /
    ``parseLiveTripMessage`` / ``parseLiveFacilityMessage``), so this forwards payloads
    verbatim rather than reshaping them — the frontend behaves identically whether Next.js
    tails Kafka itself or reads this.

    Bounded and drop-oldest on purpose: a viewer who walks away must never be able to grow
    this or back-pressure ingest. A subscriber that falls behind the window misses events,
    which for a live map means it redraws from the next frame — the same thing a reconnect
    already does.
    """

    __slots__ = ("_runs", "_lock", "_max", "_seq")

    def __init__(self, max_per_run: int = 4000) -> None:
        self._runs: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._max = max_per_run
        self._seq = 0

    def publish(self, run_id: str, event: str, payload: Any) -> None:
        if not run_id:
            return
        with self._lock:
            self._seq += 1
            buf = self._runs.get(run_id)
            if buf is None:
                buf = self._runs[run_id] = deque(maxlen=self._max)
            buf.append((self._seq, event, payload))

    def since(self, run_id: str, cursor: int, limit: int = 500):
        """Events after ``cursor`` for one run, plus the new cursor."""
        with self._lock:
            buf = self._runs.get(run_id)
            if not buf:
                return [], cursor
            out = [e for e in buf if e[0] > cursor][:limit]
        return out, (out[-1][0] if out else cursor)

    def head(self, run_id: str) -> int:
        with self._lock:
            buf = self._runs.get(run_id)
            return buf[-1][0] if buf else 0

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"runs": len(self._runs), "events": sum(len(b) for b in self._runs.values())}


class _FacilitySnapshotCoalescer:
    """Collapse the repeat facility snapshots emitted within one simulation tick.

    Holds the most recent snapshot per ``(run_id, facility_id)`` and releases it when that
    facility's ``sim_clock`` advances — so exactly one snapshot per facility per tick reaches
    the live bus, and it is the LAST one, which is the state the dashboard's last-writer-wins
    reducer already settles on today.

    Fails OPEN: a payload with no ``facility_id`` or no ``sim_clock`` is passed straight
    through uncoalesced, as is every payload when ``max_hold_s`` is 0 (the kill switch).

    ``max_hold_s`` bounds how long a held snapshot can wait, so a facility that goes quiet
    mid-run (or a run whose last tick never advances) still delivers its final state. The
    sweep runs inline on each ``offer``; with ~60 facilities per run that is cheaper than
    owning a thread, and facility traffic is dense enough (~220/s) to drive it.
    """

    __slots__ = ("_pending", "_lock", "_max_hold_s", "_coalesced")

    def __init__(self, max_hold_s: float = DEFAULT_FACILITY_COALESCE_MAX_HOLD_S) -> None:
        self._pending: Dict[tuple, tuple] = {}
        self._lock = threading.Lock()
        self._max_hold_s = float(max_hold_s)
        self._coalesced = 0

    @property
    def enabled(self) -> bool:
        return self._max_hold_s > 0

    def offer(self, run_id: str, payload: dict, now: Optional[float] = None) -> list:
        """Return the (run_id, payload) pairs to publish for this arrival."""
        if not self.enabled:
            return [(run_id, payload)]
        facility_id = payload.get("facility_id")
        sim_clock = payload.get("sim_clock")
        if not facility_id or not sim_clock:
            return [(run_id, payload)]

        now = time.monotonic() if now is None else now
        key = (run_id, facility_id)
        out = []
        with self._lock:
            held = self._pending.get(key)
            if held is not None:
                held_payload, _held_at, held_clock = held
                if held_clock == sim_clock:
                    # Same tick: this arrival supersedes the held one, which is dropped.
                    self._coalesced += 1
                else:
                    out.append((run_id, held_payload))
            self._pending[key] = (payload, now, sim_clock)
            out.extend(self._sweep_locked(now))
        return out

    def _sweep_locked(self, now: float) -> list:
        """Release snapshots held longer than ``max_hold_s``. Caller holds the lock."""
        deadline = now - self._max_hold_s
        stale = [k for k, (_p, at, _c) in self._pending.items() if at <= deadline]
        return [(k[0], self._pending.pop(k)[0]) for k in stale]

    def flush_run(self, run_id: str) -> list:
        """Release everything held for one run — used when the run goes terminal."""
        with self._lock:
            keys = [k for k in self._pending if k[0] == run_id]
            return [(run_id, self._pending.pop(k)[0]) for k in keys]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"pending": len(self._pending), "coalesced": self._coalesced}


class DataplaneService:
    def __init__(
        self,
        *,
        hot: Any = None,
        duck: Any = None,
        archive: Any = None,
        consumer: Any = None,
        consumer_factory: Optional[Callable[[dict], Any]] = None,
        group_id: str = "dataplane",
        auto_offset_reset: str = "latest",
        db_path: Optional[str] = None,
        http_host: Optional[str] = None,
        http_port: Optional[int] = None,
        enable_http: bool = True,
        reconcile_interval_s: float = DEFAULT_RECONCILE_INTERVAL_S,
        frame_interval_s: float = DEFAULT_FRAME_INTERVAL_S,
        facility_coalesce_max_hold_s: Optional[float] = None,
        archive_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        # Guards ONLY the construction/publication of _hot, _duck, _consumer, _http_server.
        # Never held across Mongo, Kafka or a long DuckDB call.
        self._init_lock = threading.RLock()
        self._hot = hot
        self._duck = duck
        # Disjoint from _init_lock: the archive probe does real network I/O, and the ingest
        # path must never queue behind it.
        self._archive_lock = threading.RLock()
        self._archive = archive
        self._archive_factory = archive_factory
        self._mongo_client: Any = None
        # Probe scheduling (never a one-shot latch): monotonic deadline + growing backoff.
        self._archive_probe_at = 0.0 if archive is None else float("inf")
        self._archive_probe_backoff = ARCHIVE_PROBE_BACKOFF_INITIAL_S
        self._archive_probes = 0
        self._archive_probing = False
        self._db_path = db_path

        self.group_id = group_id
        self.auto_offset_reset = auto_offset_reset
        self._consumer_factory = consumer_factory
        self._consumer = consumer

        self.http_host = http_host or os.environ.get("DATAPLANE_HTTP_HOST", DEFAULT_HTTP_HOST)
        self.http_port = int(
            http_port
            if http_port is not None
            else os.environ.get("DATAPLANE_HTTP_PORT", DEFAULT_HTTP_PORT)
        )
        self.enable_http = enable_http

        self.reconcile_interval_s = float(reconcile_interval_s)
        self.frame_interval_s = float(frame_interval_s)

        # Kill switch: DATAPLANE_FACILITY_COALESCE_MAX_HOLD_S=0 restores verbatim forwarding.
        self._facility_coalescer = _FacilitySnapshotCoalescer(
            float(
                facility_coalesce_max_hold_s
                if facility_coalesce_max_hold_s is not None
                else os.environ.get(
                    "DATAPLANE_FACILITY_COALESCE_MAX_HOLD_S",
                    DEFAULT_FACILITY_COALESCE_MAX_HOLD_S,
                )
            )
        )

        self._stop = threading.Event()
        self.supervisor = Supervisor()
        self._started_at = time.monotonic()
        self._http_server = None

        # Terminal runs waiting to be archived. Drained by the archive task, never inline.
        self._dump_lock = threading.Lock()
        self._dump_queue: List[Tuple[str, str]] = []
        self._dump_queued: Dict[str, str] = {}

        # Frame-capture bookkeeping: only snapshot a run that actually moved. Held across the
        # snapshot and the frame write, so there is one frame producer per run (rule 7).
        self._run_lock = threading.RLock()
        self._run_updates: Dict[str, int] = {}
        self._run_captured: Dict[str, int] = {}
        self._run_first_seen: Dict[str, datetime] = {}
        self._run_max_frame_idx: Dict[str, int] = {}
        self._run_meta_sig: Dict[str, Tuple[int, int]] = {}
        self._run_identity: Dict[str, Dict[str, Any]] = {}
        self.live_bus = _LiveBus()
        self._run_restored: set = set()
        # Rule 3. Outlives ``_dump_queued``, which is cleared by a successful dump and so
        # cannot answer "is this run over?" once the archive has caught up.
        self._run_finalized: set = set()

        # kpi rows accepted by ``on_kpi`` and not yet written. Emptied by ``flush_writes``,
        # which ``DataplaneConsumer.commit_safe`` calls before any offset can move.
        self._kpi_lock = threading.Lock()
        self._kpi_buffer: List[Tuple[Any, Any, Any, Any]] = []

        self._stats_lock = threading.Lock()
        self.counters: Dict[str, int] = {
            "kpi": 0,
            "kpi_breakdown": 0,
            "kpi_breakdown_skipped": 0,
            "truck_loc": 0,
            "trip_route": 0,
            "trip_end": 0,
            "trip_geo_other": 0,
            "facility_stream": 0,
            "facility_published": 0,
            "perf": 0,
            "run_status": 0,
            "run_terminal": 0,
            "frames_written": 0,
            "frames_skipped_idle": 0,
            "frames_not_resident": 0,
            "positions_dropped": 0,
            "runs_evicted": 0,
            # Records this process threw away. Every one of them has had its offset committed,
            # so it is gone from Kafka too — see ``_discard``.
            "kpi_malformed": 0,
            "kpi_breakdown_malformed": 0,
            "kpi_breakdown_entities_dropped": 0,
            "truck_loc_malformed": 0,
            "dumps_queued": 0,
            "dumps_ok": 0,
            "dumps_failed": 0,
            "frame_write_failures": 0,
            "runs_finalized": 0,
            "meta_writes": 0,
            "haulier_code_reseed_unavailable": 0,
            "code_book_conflicts": 0,
            "runs_restored": 0,
            "export_state_writes": 0,
        }
        self._archive_state: Dict[str, Any] = {
            "available": False,
            "lag_runs": 0,
            "last_dump_at": None,
            "pending_run_ids": [],
        }

        self.handlers: Dict[str, Callable[[str, str, dict], None]] = {
            "run_status": self.on_run_status,
            "kpi": self.on_kpi,
            "kpi_breakdown": self.on_kpi_breakdown,
            "trip_geo": self.on_trip_geo,
            "facility_stream": self.on_facility,
            "perf": self.on_perf,
        }

    # ------------------------------------------------------------------ lazy collaborators

    def _count(self, key: str, delta: int = 1) -> None:
        with self._stats_lock:
            self.counters[key] = self.counters.get(key, 0) + delta

    def _discard(self, reason: str) -> None:
        """A consumed record this process cannot use. Counted here, **timed at the boundary**.

        Four handler paths used to return without counting or logging anything at any level.
        A handler that returns without writing has its offset committed, so the record is gone
        from Kafka as well. Measured: 100% of a topic discarded with every service counter at
        zero and ``/health`` answering 200, which is the exact 2026-07-01 shape this package
        exists to remove. The consumer owns the discard *clock* (it already counts decode/key/
        value drops), so a single verdict rule judges every way a record can be thrown away.
        """
        self._count(reason)
        consumer = self._consumer
        note = getattr(consumer, "note_discard", None) if consumer is not None else None
        if note is not None:
            note(reason)

    @property
    def hot(self) -> Any:
        """The hot tier, built **seeded** so a restart cannot reuse a frame_idx.

        ``self.duck`` is materialised first, outside ``_init_lock``, so that
        ``_seed_frame_idx`` (invoked later under the hot tier's own lock) can read
        ``self._duck`` as a plain attribute without re-entering this property — taking
        ``_init_lock`` from inside the hot lock is the wrong lock order and deadlocks.
        """
        hot = self._hot
        if hot is not None:
            return hot
        try:
            self.duck
        except Exception:  # noqa: BLE001 - a broken store must not leave us with no hot tier
            logger.exception("duck store unavailable while building the hot tier")
        with self._init_lock:
            if self._hot is None:
                from apps.dataplane.store.hot import HotStore

                self._hot = HotStore(max_runs=HOT_MAX_RUNS, frame_idx_seed=self._seed_frame_idx)
            return self._hot

    def _seed_frame_idx(self, run_id: str) -> Optional[int]:
        """Next safe ``frame_idx`` for ``run_id`` from the durable working set, or None.

        Called by ``HotStore`` **with the hot lock held**, so it must never raise, never call
        back into the hot tier, and never take ``_init_lock`` (hence ``self._duck``, the plain
        attribute, not the ``duck`` property).
        """
        duck = self._duck
        if duck is None:
            return None
        frame_range = getattr(duck, "frame_range", None)
        if frame_range is None:
            return None
        try:
            rng = frame_range(run_id)
        except Exception:  # noqa: BLE001 - a failed seed must not stop ingest
            logger.debug("frame_idx seed lookup failed run_id=%s", run_id, exc_info=True)
            return None
        if not rng:
            return None
        try:
            return int(rng[1]) + 1
        except (TypeError, ValueError, IndexError):
            return None

    @property
    def duck(self) -> Any:
        duck = self._duck
        if duck is not None:
            return duck
        with self._init_lock:
            if self._duck is None:
                from apps.dataplane.store.duck import DuckStore

                self._duck = DuckStore(self._db_path)
            return self._duck

    def mongo_client_kwargs(self) -> Dict[str, Any]:
        """Connection kwargs for the archive's MongoClient, timeouts included.

        Pure, so the timeouts are testable without a mongod. ``MongoArchive`` builds its own
        client without timeouts; we hand it one that cannot block forever instead.
        """
        kwargs: Dict[str, Any] = dict(MONGO_CLIENT_TIMEOUTS)
        uri = kpi_sink_settings.get("mongo_uri")
        if uri:
            kwargs["host"] = uri
        else:
            kwargs["host"] = kpi_sink_settings["mongo_host"]
            kwargs["port"] = int(kpi_sink_settings["mongo_port"])
        return kwargs

    def _build_archive(self) -> Tuple[Any, Any]:
        """Build ``(archive, owned_client)``. Pure construction — publishes nothing.

        The client is returned rather than stashed on ``self``: assigning it here as a side
        effect meant a failed probe could close the client belonging to a *concurrent*
        successful probe.
        """
        if self._archive_factory is not None:
            return self._archive_factory(), None
        from pymongo import MongoClient

        from apps.dataplane.archive.mongo import MongoArchive

        client = MongoClient(**self.mongo_client_kwargs())
        return MongoArchive(client=client), client

    @property
    def archive(self) -> Any:
        """The Mongo archive, or None when it cannot be reached right now. Never raises.

        Two properties matter here and both were once violated:

        * the probe is **retried on a backoff** — an earlier version latched before probing
          and never reset, disabling archiving for the life of the process while ``/health``
          stayed green;
        * the probe does its network I/O with **no lock held**. ``_archive_lock`` is taken
          only to decide whether to probe and to publish the outcome, and it is a lock no
          ingest path ever touches. Holding ``_init_lock`` across ``ping()`` made every
          ``truck_loc`` and every ``/health`` wait out ``serverSelectionTimeoutMS``.
        """
        with self._archive_lock:
            if self._archive is not None:
                return self._archive
            if self._archive_probing:
                return None  # another thread is probing; never queue behind it
            if time.monotonic() < self._archive_probe_at:
                return None
            self._archive_probes += 1
            probe_no = self._archive_probes
            self._archive_probing = True

        candidate: Any = None
        client: Any = None
        ok = False
        try:  # ── unlocked: build + ping + ensure_indexes all talk to mongod ──
            candidate, client = self._build_archive()
            if candidate is not None and candidate.ping():
                candidate.ensure_indexes()
                ok = True
            else:
                logger.warning(
                    "mongo archive unreachable (probe #%d) — retrying in %.0fs",
                    probe_no, self._archive_probe_backoff,
                )
        except Exception:  # noqa: BLE001 - degrade, never fail startup
            logger.exception(
                "mongo archive unavailable (probe #%d) — retrying in %.0fs",
                probe_no, self._archive_probe_backoff,
            )

        if ok:
            with self._archive_lock:
                self._archive = candidate
                self._mongo_client = client
                self._archive_state["available"] = True
                self._archive_probe_backoff = ARCHIVE_PROBE_BACKOFF_INITIAL_S
                self._archive_probing = False
            logger.info("mongo archive connected (probe #%d)", probe_no)
            return candidate

        self._close_candidate(candidate, client)
        with self._archive_lock:
            self._archive_state["available"] = False
            self._archive_probe_at = time.monotonic() + self._archive_probe_backoff
            self._archive_probe_backoff = min(
                self._archive_probe_backoff * 2.0, ARCHIVE_PROBE_BACKOFF_MAX_S
            )
            self._archive_probing = False
        return None

    @staticmethod
    def _close_candidate(candidate: Any, client: Any) -> None:
        for obj in (candidate, client):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def consumer(self) -> DataplaneConsumer:
        with self._init_lock:
            if self._consumer is None:
                self._consumer = DataplaneConsumer(
                    handlers=self.handlers,
                    group_id=self.group_id,
                    auto_offset_reset=self.auto_offset_reset,
                    consumer_factory=self._consumer_factory,
                    flush=self.flush_writes,
                    flush_topics=("kpi",),  # the only handler that defers its write
                )
            return self._consumer

    # ------------------------------------------------------------------ handlers
    # They run on the consumer thread and are wrapped by DataplaneConsumer.handle_message,
    # which counts (never propagates) exceptions. A handler that RAISES is the durability
    # contract: its record's offset is not committed, so the broker replays it.

    def on_kpi(self, topic: str, run_id: str, payload: dict) -> None:
        metric = payload.get("metric")
        if not metric:
            self._discard("kpi_malformed")
            return
        # Forward verbatim to the live bus BEFORE storing, exactly as `on_trip_geo` does.
        #
        # This is the dashboard clock's ONLY live time source. `metricsSlice` derives
        # `simBaseTimeMs`/`latestSimTimeMs` from KPI `sim_clock`, and `Dashboard` derives the
        # live duration from those two. When Next.js tailed Kafka itself it forwarded these
        # payloads as NAMELESS SSE data lines (`es.onmessage`); the dataplane took over the
        # live stream but never republished kpi, so the clock sat at its
        # `2020-01-01T00:00:00Z` default for the whole run while trucks moved on the map.
        # Raw payload, not the parsed row: `sim_clock` stays the RFC-1123 string the browser's
        # `new Date(...)` expects, and the store's epoch-ms form would parse to NaN there.
        self.live_bus.publish(run_id, "kpi", payload)
        # A payload the store can never represent is a DISCARD, not an exception. Both parses
        # were unguarded, so a missing / unparseable / numeric ``sim_clock`` or a non-numeric
        # ``value`` raised out of the handler and pinned the partition at that record for the
        # life of the process — and the byte-identical replay re-wedged it after every
        # restart. Raising is reserved for "the store is down", which a redelivery can fix.
        try:
            row = (
                run_id,
                str(metric),
                coerce_metric_value(payload.get("value")),
                parse_sim_clock(payload.get("sim_clock")),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            self._discard("kpi_malformed")
            logger.warning("undecodable kpi payload run_id=%s metric=%s: %s", run_id, metric, exc)
            return
        with self._kpi_lock:
            self._kpi_buffer.append(row)
        self._count("kpi")

    def flush_writes(self) -> None:
        """Write this poll batch's kpi rows in ONE statement. Raising means: commit nothing.

        A DuckDB transaction costs ~6.5 ms whatever it carries, so one statement per *record*
        capped ingest at ~141 msg/s against a measured live rate of 132; one per poll batch
        carries 200 rows at 0.05 ms each.

        Not a write queue: it is emptied by ``DataplaneConsumer.commit_safe``, the only place
        an offset can move, so a row is never buffered across a commit — and a failed write
        DROPS its rows while the consumer rolls the commit points back, so the broker replays
        them exactly as it did when each handler wrote its own row and raised. Retaining them
        would be a retry queue that grows for as long as the store is down.
        """
        with self._kpi_lock:
            rows, self._kpi_buffer = self._kpi_buffer, []
        if rows:
            self.duck.write_kpi_events(rows)

    def on_kpi_breakdown(self, topic: str, run_id: str, payload: dict) -> None:
        scope = payload.get("scope")
        if scope not in BREAKDOWN_SCOPES:
            if scope in BREAKDOWN_SCOPES_IGNORED:
                self._count("kpi_breakdown_skipped")
            else:
                self._discard("kpi_breakdown_unknown_scope")
                logger.warning("kpi_breakdown scope=%r is stored nowhere run_id=%s", scope, run_id)
            return
        raw_clock = payload.get("sim_clock")
        if not raw_clock:
            self._discard("kpi_breakdown_malformed")
            return
        try:  # same rule as on_kpi: an unparseable clock is a discard, never a wedged partition
            clock = parse_sim_clock(raw_clock)
        except (TypeError, ValueError, OverflowError) as exc:
            self._discard("kpi_breakdown_malformed")
            logger.warning("unparseable breakdown sim_clock run_id=%s: %s", run_id, exc)
            return
        # The ENVELOPE is guarded for the same reason its entities are: a ``breakdown`` that
        # is a list or a string raised AttributeError out of this handler, identically on
        # every redelivery, pinning kpi_breakdown_stream for the life of the deployment.
        breakdown = payload.get("breakdown")
        if breakdown is not None and not isinstance(breakdown, dict):
            self._discard("kpi_breakdown_malformed")
            return
        entities = ((breakdown or {}).get("entities")) or []
        if not isinstance(entities, list):
            self._discard("kpi_breakdown_malformed")
            return
        # THE poison gate. ``write_breakdown_batch`` calls ``entity.get("id")``, so a JSON
        # null or a bare string inside ``entities`` raises AttributeError, identically on
        # every redelivery, forever — one malformed producer message was enough to pin
        # ``kpi_breakdown_stream`` at offset 0 for the life of the deployment. Rejecting it
        # here is a deletion of risk: the store never sees a payload it cannot represent.
        clean = [e for e in entities if isinstance(e, dict)]
        if len(clean) != len(entities):
            self._discard("kpi_breakdown_entities_dropped")
        # An EMPTY entities list is legitimate, not malformed: ``save_breakdowns()`` in
        # apps/container_logistics/analytics/manager.py emits exactly that at the start of
        # every run. Counting it as a discard 503'd /health for the first five minutes of
        # every simulation. ``write_breakdown_batch`` writes nothing for it, which is right.
        self.duck.write_breakdown_batch(
            run_id, [(str(scope), clock, bool(payload.get("final")), clean)]
        )
        self._count("kpi_breakdown")

    def on_trip_geo(self, topic: str, run_id: str, payload: dict) -> None:
        # Forward verbatim before interpreting: the dashboard's `trip` handler parses raw
        # truck_loc AND trip payloads off this one event name, so a faithful forward is what
        # lets Next.js stop tailing Kafka itself.
        self.live_bus.publish(run_id, "trip", payload)
        kind = payload.get("type")
        if kind == "trip_route":
            self._count("trip_route")
            return
        if kind == "trip_end":
            self._count("trip_end")
            return
        if kind != "truck_loc":
            self._count("trip_geo_other")
            return
        agent_id = payload.get("truck_agent_id")
        lon = payload.get("lon")
        lat = payload.get("lat")
        haulier_id = payload.get("haulier_id")
        # The hot tier's haulier code book is a dict KEYED by this value, so an object here
        # raised ``TypeError: unhashable type`` out of the handler and pinned trip_geo_stream
        # — whose offsets commit on receipt, so a frozen partition does not even save the
        # record. ``haul_state`` needs no guard: ``state_code`` is already total.
        if not agent_id or lon is None or lat is None or isinstance(haulier_id, (dict, list, set)):
            self._discard("truck_loc_malformed")
            return
        # …and REJECTING the unhashable types is not enough, because the poison is not
        # unhashability, it is a MIXED book. A JSON number or boolean is hashable, so
        # ``{"H1": 1, 1.5: 2}`` was admitted, and the terminal then reached
        # ``json.dumps(codes, sort_keys=True)`` in persist_run_meta and raised
        # ``TypeError: '<' not supported between instances of 'str' and 'float'`` out of
        # finalize_run -> on_run_status. That does not advance run_status, so the
        # byte-identical terminal replays on every restart and the partition whose lost
        # consumer WAS the 2026-07-01 outage is blocked for the life of every process.
        # Coerced at the boundary, exactly as ``agent_id`` already is, the book can only
        # ever have ``str`` keys and the poison class is unrepresentable rather than merely
        # rejected. ``None`` stays ``None``: the hot tier reads it as "no haulier", code 0.
        if haulier_id is not None:
            haulier_id = str(haulier_id)
        try:  # same rule as on_kpi: junk coordinates or an unparseable clock are a discard
            lon, lat = float(lon), float(lat)
            sim_ms = sim_time_ms(payload.get("sim_clock"))
        except (TypeError, ValueError, OverflowError) as exc:
            self._discard("truck_loc_malformed")
            logger.warning("undecodable truck_loc run_id=%s agent=%s: %s", run_id, agent_id, exc)
            return
        with self._run_lock:
            finalized = run_id in self._run_finalized
        if finalized:
            # Rule 3: admitting this re-created the evicted slab, and the sweep then wrote
            # frames into DuckDB after the archive dump had already been taken.
            self._count("trip_geo_other")
            return
        self.restore_run_codes(run_id)
        self.hot.update_position(
            run_id,
            str(agent_id),
            lon,  # the wire field is `lon`; the frame column is `lng`
            lat,
            payload.get("haul_state"),
            haulier_id,
            sim_ms,
        )
        with self._run_lock:
            self._run_updates[run_id] = self._run_updates.get(run_id, 0) + 1
            self._run_first_seen.setdefault(run_id, _utcnow_naive())
        self._count("truck_loc")

    def on_facility(self, topic: str, run_id: str, payload: dict) -> None:
        self._count("facility_stream")  # stored nowhere; live-only, by design
        # One snapshot per facility per tick reaches the bus; see _FacilitySnapshotCoalescer.
        for rid, out in self._facility_coalescer.offer(run_id, payload):
            self.live_bus.publish(rid, "facility", out)
            self._count("facility_published")

    def on_perf(self, topic: str, run_id: str, payload: dict) -> None:
        self._count("perf")  # round 1: counted, not stored

    def on_run_status(self, topic: str, run_id: str, payload: dict) -> None:
        self._count("run_status")
        # Capture the run's identity BEFORE the terminal check. run_name / scenario_slug /
        # scenario_display_name ride on the RUNNING messages, which is exactly the case the
        # dashboard's live-run list needs, and the early return below would skip them.
        # This replaces `peekLatestRunStatusPayload` in the frontend, which spawned a NEW
        # Kafka consumer group per run per page load and tore it down 1.2 s later — far less
        # than a join+sync takes, so the teardown raced the startup and produced
        # `KafkaJSConnectionError: write after end`, plus 18 leaked `peek-rs` groups.
        self._note_run_identity(run_id, payload)
        self.live_bus.publish(run_id, "status", payload)
        # run_meta is the dashboard's RunStatusMeta, emitted once per run when run_status
        # first carries the identity fields — the same message _note_run_identity persists.
        if any(payload.get(k) for k in ("run_name", "scenario_slug", "scenario_display_name")):
            self.live_bus.publish(
                run_id,
                "run_meta",
                {
                    "status": payload.get("status"),
                    "scenarioSlug": payload.get("scenario_slug"),
                    "scenarioName": payload.get("scenario_display_name"),
                    "numTrucks": payload.get("num_trucks"),
                    "numOrders": payload.get("num_orders"),
                    "runName": payload.get("run_name"),
                },
            )
        reason = should_export_run_status(payload)
        if reason is None:
            return
        self._count("run_terminal")
        # A held snapshot must not die with the run: release the last tick before the
        # terminal event, so the final facility state still reaches an attached viewer.
        for rid, out in self._facility_coalescer.flush_run(run_id):
            self.live_bus.publish(rid, "facility", out)
            self._count("facility_published")
        self.live_bus.publish(run_id, "simulation_terminal", {"outcome": reason, "run_id": run_id})
        if reason == "completed":
            self.live_bus.publish(run_id, "simulation_complete", {"run_id": run_id})
        logger.info("terminal run_status run_id=%s reason=%s", run_id, reason)
        self.finalize_run(run_id, reason)

    # ------------------------------------------------------------------ work

    def _note_run_identity(self, run_id: str, payload: dict) -> None:
        """Persist a run's name/scenario once, the first time run_status carries them.

        Written only when the value actually changes, so a run publishing a status every
        step costs one dict comparison per message rather than a DuckDB write.
        """
        fields = {
            "run_name": payload.get("run_name"),
            "scenario_slug": payload.get("scenario_slug"),
            "scenario_name": payload.get("scenario_display_name"),
        }
        known = {k: v for k, v in fields.items() if isinstance(v, str) and v}
        if not known:
            return
        with self._run_lock:
            if self._run_identity.get(run_id) == known:
                return
            self._run_identity[run_id] = known
        n_trucks = payload.get("num_trucks")
        if isinstance(n_trucks, int):
            known["n_trucks"] = n_trucks
        try:
            duck = self.duck
            if duck is not None:
                duck.upsert_run_meta(run_id, **known)
        except Exception:  # noqa: BLE001 - identity must not stop ingest, but must be visible
            # Not debug: a swallowed write here is invisible and the row simply stays empty,
            # which is how the schema/whitelist mismatch above survived its first live run.
            logger.warning("run identity write failed for %s", run_id, exc_info=True)
            with self._run_lock:
                self._run_identity.pop(run_id, None)

    def finalize_run(self, run_id: str, reason: str) -> None:
        """Close one run out. Raising means "this record was not handled" — replay it.

        The order is the fix, not a convention:

        1. ``capture_run`` — the run's final positions, while the slab still exists;
        2. ``persist_run_meta(status=reason, force=True)`` — the haulier code book and slot map,
           without which every archived frame is integers with no dictionary anywhere;
        3. ``evict_run`` — **only now**;
        4. ``queue_dump`` — the archive task picks it up.

        Steps 1 and 2 propagate their failures out of ``on_run_status``, so the terminal
        record's offset is not committed and the broker replays it after a restart. Evicting
        *before* those two succeeded is what made a single failed final frame write
        unrecoverable: with the slab gone there was no resident run left, so the failure could
        never be retried.

        A terminal is replayable, so every step must be idempotent: ``capture_run`` skips a
        run that has not moved, because ``frames`` has no primary key and is the one write
        here a replay cannot deduplicate.

        Step 0 is the kpi rows this poll batch buffered: a terminal can arrive in the same
        batch as its run's last kpi records, and the archive must not read a working set one
        flush behind. It goes through the *consumer's* wrapper, which is what rolls the kpi
        commit points back if that write fails.
        """
        consumer = self._consumer
        if consumer is None:
            self.flush_writes()
        elif not consumer.flush_writes():
            raise RuntimeError(f"kpi flush failed before finalizing run_id={run_id}")
        # Step 0b: is this run's ingest WHOLE? ``flush_writes`` answers only for the batch in
        # hand. A partition blocked by an earlier failure is holding rows that never reached
        # the store and that only a replay can recover, and archiving over that hole is worse
        # than not archiving at all: measured, one transient kpi write failure left 100 of
        # 200 rows on disk and the terminal then captured, persisted, evicted and dumped the
        # run anyway — Mongo held ``kpi_count=100`` with ``complete=True``, and the
        # reconciler could never repair it because ``run_is_pending`` compares DuckDB's 100
        # against the archive's 100 and finds them equal. Refusing does not advance the
        # terminal's offset, so the run is finalized correctly on the restart that replays
        # the blocked partition (measured after restart: 200/200 rows, 0 duplicates).
        #
        # ``run_status`` is excluded from the question: it carries no rows into the archive,
        # and refusing here blocks it, so counting it would make this gate self-perpetuating.
        blocked = set()
        if consumer is not None:
            blocked = {t for t in consumer.blocked_topics() if t != "run_status"}
        if blocked:
            raise RuntimeError(
                f"ingest is blocked on {sorted(blocked)}; refusing to finalize run_id="
                f"{run_id} — its archive would be written complete with a hole"
            )
        self.capture_run(run_id)
        with self._run_lock:
            # A run the LRU pushed out before its terminal arrived is closed here with NO
            # position series: ``capture_run`` catches ``RunNotResident``, and the run is then
            # persisted ``completed``, evicted and archived with ``frame_count=0``. A real,
            # unrecoverable loss that went into no counter any endpoint reads.
            lost = self._positions_uncaptured(run_id)
        if lost:
            self._count("positions_dropped")
            logger.error(
                "run_id=%s finalized with positions that were never captured — those positions "
                "are unrecoverable (trip_geo offsets are committed on receipt)", run_id,
            )
        self.persist_run_meta(run_id, status=reason, force=True)
        with self._run_lock:
            self._run_finalized.add(run_id)  # rule 3: never re-admit it to the hot tier
        self.evict_run(run_id)
        self.queue_dump(run_id, reason)
        self._count("runs_finalized")

    def capture_run(self, run_id: str) -> int:
        """Snapshot one run into DuckDB. Returns the number of frames written (0 or 1).

        ``_run_lock`` is held for the whole body — the snapshot AND the write — so this run
        has exactly one frame producer even though the sweep and a terminal arriving on the
        poll thread can both call it. Two producers minted two ``frame_idx`` values for one
        instant. The lock is a leaf: no other thread holds it across anything slow, and the
        frame write it covers measures well under a millisecond.

        A run whose positions have not changed since the last capture is skipped: otherwise a
        finished (or merely idle) run mints an identical frame every ``frame_interval_s``
        forever, and those frames land after the archive dump so DuckDB and Mongo diverge.
        The skip is **unconditional**, which is what makes the terminal path and its replays
        idempotent: a ``force`` flag that overrode it appended a byte-identical copy of the
        last sweep's frame at the normal end of every run.

        Two failures are deliberately *not* the same thing:

        * **no positions to capture** — the run is not resident (headless, already evicted, or
          never seen). ``snapshot`` raises :class:`RunNotResident`, which is caught *by that
          type* and counted, and this returns 0. Nothing is wrong: there is no frame to write,
          and blocking the run's terminal sequence on the absence of positions would leave the
          run open forever, which is the headless case (rule 6). A bare ``except Exception``
          here made this case indistinguishable from a hot tier that was malfunctioning, and
          the terminal path then archived and evicted the run anyway;
        * **the store would not take the frame** — ``write_frame`` raises. That *is* a failure:
          it is counted, it hands the frame index back to the hot tier, it leaves
          ``_run_captured`` unadvanced so the next sweep retries the same positions, and it
          re-raises, which on the terminal path is what stops :meth:`finalize_run` **before**
          the eviction and leaves the terminal record uncommitted.
        """
        hot = self._hot
        if hot is None:
            return 0
        with self._run_lock:
            seq = self._run_updates.get(run_id, 0)
            seen = self._run_captured.get(run_id)
            if seen is not None and seen == seq:
                self._count("frames_skipped_idle")
                return 0
            try:
                frame = hot.snapshot(run_id)
            except RunNotResident:  # nothing to capture — anything else propagates
                self._count("frames_not_resident")
                logger.debug("no hot slab to capture run_id=%s", run_id)
                return 0
            if frame.n == 0:
                self._run_captured[run_id] = seq
                return 0
            try:
                self.duck.write_frame(run_id, frame)
            except Exception:  # noqa: BLE001 - counted, rolled back, replayed; never swallowed
                self._count("frame_write_failures")
                self._rollback_frame_idx(hot, run_id, frame)
                raise
            self._run_captured[run_id] = seq
            idx = int(getattr(frame, "frame_idx", 0) or 0)
            if idx >= self._run_max_frame_idx.get(run_id, -1):
                self._run_max_frame_idx[run_id] = idx
        self._count("frames_written")
        return 1

    def _positions_uncaptured(self, run_id: str) -> bool:
        """Has this run reported positions that no frame on disk holds? Call under ``_run_lock``.

        The ONE definition of "trip_geo data was lost", shared by the LRU audit in
        :meth:`capture_frames` and the terminal path in :meth:`finalize_run`, which used to
        get it wrong in opposite directions: the sweep counted every eviction even when every
        position was already on disk, and the terminal path counted none even when the run had
        left by the LRU door. ``trip_geo`` offsets commit on receipt: nothing can replay these.
        """
        return self._run_updates.get(run_id) != self._run_captured.get(run_id)

    @staticmethod
    def _rollback_frame_idx(hot: Any, run_id: str, frame: Any) -> None:
        """Give a failed frame's index back, so the next attempt reuses it (no 0..39 hole)."""
        rollback = getattr(hot, "rollback_frame_idx", None)
        if rollback is None:
            return
        try:
            rollback(run_id, int(getattr(frame, "frame_idx", 0) or 0))
        except Exception:  # noqa: BLE001 - best effort
            logger.debug("frame_idx rollback failed run_id=%s", run_id, exc_info=True)

    def capture_frames(self) -> int:
        """Snapshot every resident run that moved since the last sweep.

        The sweep is also the only place that can notice a run leaving the hot tier by the
        *LRU* door rather than through :meth:`evict_run`. That loss is real and unrecoverable
        — ``trip_geo`` offsets are committed on receipt, so nothing can replay those positions
        — so it is counted as ``positions_dropped``, which ``/health`` reports as
        ``store.rows_dropped`` and fails the verdict on. ``HOT_MAX_RUNS`` is what makes it
        essentially unreachable; this is the audit that proves it.

        The set of runs to check is read **before** ``resident_runs()``: read after, a run
        whose very first position landed in between was absent from the (older) residency
        list and was reported as a loss that never happened.
        """
        hot = self._hot
        if hot is None:
            return 0
        with self._run_lock:
            known = set(self._run_updates)
        try:
            runs = list(hot.resident_runs())
        except Exception:  # noqa: BLE001
            logger.exception("resident_runs failed")
            return 0
        with self._run_lock:
            gone = [rid for rid in known if rid not in runs and rid not in self._run_finalized]
            # Leaving the hot tier is not a loss; leaving it with work the sweep never wrote is.
            # Counting every LRU eviction latched /health at 503 forever over frames that were
            # already on disk (``rows_dropped`` is monotonic, so no success can clear it) —
            # the very "counter only a later success can clear" shape this package deleted.
            dropped = [rid for rid in gone if self._positions_uncaptured(rid)]
            for rid in gone:
                self._run_updates.pop(rid, None)
                self._run_captured.pop(rid, None)
        if dropped:
            self._count("positions_dropped", len(dropped))
            logger.error(
                "hot tier evicted run(s) %s with positions that were never captured — those "
                "positions are unrecoverable (trip_geo offsets are committed on receipt)",
                dropped,
            )
        written = 0
        for run_id in runs:
            try:
                written += self.capture_run(run_id)
                self.persist_run_meta(run_id)
            except Exception:  # noqa: BLE001 - one bad run must not stop the sweep
                logger.exception("frame capture failed run_id=%s", run_id)
        return written

    @staticmethod
    def _load_code_map(raw: Any) -> Dict[str, int]:
        """A JSON ``{name: int}`` column as a dict. Junk reads as empty, never raises."""
        if isinstance(raw, dict):
            source: Any = raw
        elif raw:
            try:
                source = json.loads(raw)
            except (TypeError, ValueError):
                return {}
        else:
            return {}
        if not isinstance(source, dict):
            return {}
        out: Dict[str, int] = {}
        for key, value in source.items():
            try:
                out[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return out

    def _merge_code_map(
        self, run_id: str, what: str, persisted: Dict[str, int], live: Dict[str, int]
    ) -> Dict[str, int]:
        """Union of the two books; on a disagreement the PERSISTED value wins.

        The persisted value is the one the already-archived frames were encoded with, so it
        is the only value that keeps those frames decodable. A disagreement means this
        process minted a different code before the restore landed — it is counted and warned
        about, never resolved silently in favour of the newcomer.
        """
        merged = dict(live)
        conflicts: List[str] = []
        for key, value in persisted.items():
            if key in merged and merged[key] != value:
                conflicts.append(key)
            merged[key] = value
        if conflicts:
            self._count("code_book_conflicts", len(conflicts))
            logger.warning(
                "run_id=%s: %d %s entr(y/ies) disagree with the persisted book (%s) — "
                "keeping the persisted values, which is what the archived frames encode",
                run_id, len(conflicts), what, sorted(conflicts),
            )
        return merged

    def persist_run_meta(self, run_id: str, *, status: Optional[str] = None, force: bool = False) -> bool:
        """Write the run's code books and status into ``run_meta``. True if a row was written.

        ``haulier`` is a uint8 and ``slot`` a uint32 whose meaning lives only in the hot tier's
        first-sighting order. Without these two JSON maps on disk, every archived frame is a
        pile of integers that cannot be coloured by haulier or joined to a truck — which would
        make the project's primary lens (empty distance per haulier) unrecoverable from the
        durable record. Written whenever the maps grow, and always at terminal status.

        Two rules earn their keep here:

        * the write **merges** with what is already on disk and never writes an empty map, so
          a status-only write (or a process that saw the run in a different order) cannot
          stamp ``{}`` over the only surviving dictionary;
        * the "nothing new to write" signature is recorded **after** the upsert returns.
          Recorded before, one failed write was never retried — the book stops growing once
          every truck has been seen, so the sweep's only trigger never fired again and the
          books never reached disk at all (1 attempt across 21 sweeps, ``run_meta`` ``None``);
        * a forced/terminal write happens **even with no hot tier**. Headless runs publish no
          ``truck_loc``, so ``_hot`` is never built for them; making the status conditional on
          a resident slab left every headless run non-terminal, its archive summary open, and
          ``archive.lag_runs`` climbing forever.
        """
        hot = self._hot
        if hot is None and status is None and not force:
            return False
        duck = self.duck
        codes: Dict[str, int] = {}
        slots: Dict[str, int] = {}
        if hot is not None:
            try:
                codes = dict(hot.haulier_codes(run_id))
                slots = dict(hot.slot_map(run_id))
            except Exception:  # noqa: BLE001 - run evicted between the sweep and here
                if status is None and not force:
                    return False
                codes, slots = {}, {}

        existing: Dict[str, Any] = {}
        getter = getattr(duck, "get_run_meta", None)
        if getter is not None:
            try:
                existing = getter(run_id) or {}
            except Exception:  # noqa: BLE001 - a read failure must not lose the write
                logger.debug("run_meta read failed run_id=%s", run_id, exc_info=True)
                existing = {}
        codes = self._merge_code_map(
            run_id, "haulier code", self._load_code_map(existing.get("haulier_codes")), codes
        )
        slots = self._merge_code_map(
            run_id, "slot", self._load_code_map(existing.get("slot_map")), slots
        )

        sig = (len(codes), len(slots))
        with self._run_lock:
            if not force and status is None and self._run_meta_sig.get(run_id) == sig:
                return False
            first_seen = self._run_first_seen.setdefault(run_id, _utcnow_naive())
            max_idx = self._run_max_frame_idx.get(run_id)
        fields: Dict[str, Any] = {
            "first_seen": first_seen,
            "last_seen": _utcnow_naive(),
            "source": "live",
        }
        # Never write an empty map: it would erase the book rather than describe it.
        # ``sort_keys`` compares the keys against each other, so a map whose keys are not all
        # the same type raises here — belt and braces beside the boundary coercion in
        # ``on_trip_geo``, because this call is on the terminal path and anything that raises
        # here wedges run_status *and* leaves the archived frames as integers with no
        # dictionary anywhere, which is the very failure this function exists to prevent.
        if codes:
            fields["haulier_codes"] = json.dumps(
                {str(k): v for k, v in codes.items()}, sort_keys=True
            )
        if slots:
            fields["slot_map"] = json.dumps(
                {str(k): v for k, v in slots.items()}, sort_keys=True
            )
            fields["n_trucks"] = len(slots)
        if max_idx is not None:
            fields["max_frame_idx"] = int(max_idx)
        if status is not None:
            fields["status"] = status
        duck.upsert_run_meta(run_id, **fields)
        with self._run_lock:
            self._run_meta_sig[run_id] = sig  # only a DURABLE write commits the signature
        self._count("meta_writes")
        return True

    def restore_run_codes(self, run_id: str) -> bool:
        """Once per run: put the persisted run identity back into the hot tier.

        Codes are assigned in first-sighting order, so a process restart mid-run would
        otherwise make the same uint8 mean a different haulier — and the same uint32 a
        different truck — in different segments of one run's frames. All three parts of the
        identity are restored: the haulier code book, the slot map, and the next frame index
        (``max_frame_idx + 1``, agreeing with the ``frame_idx_seed`` the hot tier already
        holds).

        Restoring slots is safe: ``_RunSlab.written`` excludes never-written slots from
        ``snapshot()``, so a re-seeded truck that has not reported yet is simply absent from
        the frame rather than sitting at (0, 0) in the sea.

        The run is latched (never looked up twice) only once a lookup has actually completed —
        latching before the store existed is what made this dead code.
        """
        with self._run_lock:
            if run_id in self._run_meta_sig or run_id in self._run_restored:
                return False
        duck = self.duck  # the PROPERTY: on the first message of a fresh process _duck is None
        getter = getattr(duck, "get_run_meta", None) if duck is not None else None
        if getter is None:
            return False  # deliberately not latched: nothing was looked up
        try:
            meta = getter(run_id)
        except Exception:  # noqa: BLE001
            logger.debug("run_meta lookup failed run_id=%s", run_id, exc_info=True)
            return False  # deliberately not latched: retry on the next message
        with self._run_lock:
            self._run_restored.add(run_id)  # a completed lookup, including a None result
        if not meta:
            return False
        start_frame_idx: Optional[int] = None
        with self._run_lock:
            if meta.get("first_seen") is not None:
                self._run_first_seen[run_id] = meta["first_seen"]
            if meta.get("max_frame_idx") is not None:
                try:
                    max_idx = int(meta["max_frame_idx"])
                except (TypeError, ValueError):
                    max_idx = None
                if max_idx is not None:
                    self._run_max_frame_idx[run_id] = max_idx
                    start_frame_idx = max_idx + 1
        codes = self._load_code_map(meta.get("haulier_codes"))
        slots = self._load_code_map(meta.get("slot_map"))
        if not codes and not slots and start_frame_idx is None:
            return False
        seeder = getattr(self.hot, "seed_run_identity", None)
        if seeder is None:
            self._count("haulier_code_reseed_unavailable")
            logger.warning(
                "run_id=%s has %d persisted haulier codes but the hot tier cannot be seeded — "
                "codes assigned after this restart may not match the archived frames",
                run_id,
                len(codes),
            )
            return False
        try:
            seeder(
                run_id,
                haulier_codes=codes or None,
                slot_map=slots or None,
                start_frame_idx=start_frame_idx,
            )
        except Exception:  # noqa: BLE001
            logger.exception("run identity seeding failed run_id=%s", run_id)
            return False
        self._count("runs_restored")
        logger.info(
            "restored run identity run_id=%s (%d haulier code(s), %d slot(s), next frame_idx %s)",
            run_id, len(codes), len(slots), start_frame_idx,
        )
        return True

    def evict_run(self, run_id: str) -> bool:
        hot = self._hot
        if hot is None:
            return False
        try:
            evicted = bool(hot.evict(run_id))
        except Exception:  # noqa: BLE001
            logger.exception("hot eviction failed run_id=%s", run_id)
            return False
        if evicted:
            self._count("runs_evicted")
            with self._run_lock:
                self._run_updates.pop(run_id, None)
                self._run_captured.pop(run_id, None)
        return evicted

    # ------------------------------------------------------------------ archive

    def queue_dump(self, run_id: str, reason: str = "") -> None:
        with self._dump_lock:
            if run_id in self._dump_queued:
                self._dump_queued[run_id] = reason or self._dump_queued[run_id]
                return
            self._dump_queued[run_id] = reason
            self._dump_queue.append((run_id, reason))
        self._count("dumps_queued")
        self._refresh_pending()

    def pending_dumps(self) -> List[str]:
        with self._dump_lock:
            return [run_id for run_id, _ in self._dump_queue]

    def _refresh_pending(self, extra: Optional[List[str]] = None) -> None:
        pending = self.pending_dumps()
        for run_id in extra or ():
            if run_id not in pending:
                pending.append(run_id)
        with self._archive_lock:
            self._archive_state["pending_run_ids"] = pending
            self._archive_state["lag_runs"] = len(pending)

    def drain_dumps(self, limit: Optional[int] = None) -> int:
        """Archive the queued terminal runs. Runs on the archive task, never on ingest."""
        done = 0
        while True:
            if limit is not None and done >= limit:
                break
            with self._dump_lock:
                if not self._dump_queue:
                    break
                run_id, reason = self._dump_queue[0]
            if not self.dump_run(run_id, reason=reason):
                break  # archive down: leave it queued, a miss costs delay, never data
            with self._dump_lock:
                if self._dump_queue and self._dump_queue[0][0] == run_id:
                    self._dump_queue.pop(0)
                self._dump_queued.pop(run_id, None)
            done += 1
        self._refresh_pending()
        return done

    def dump_run(self, run_id: str, *, reason: str = "") -> bool:
        """Archive one run to Mongo. A failure is logged and reported, never raised.

        The run summary (including the code books) is written by ``MongoArchive.dump_run``
        itself, so the reconcile sweep and this path produce identical summaries. The service
        deliberately does **not** patch the summary afterwards: it only ever did so from one
        of the two call sites, so the next sweep wiped what it had added.
        """
        archive = self.archive
        if archive is None:
            with self._archive_lock:
                self._archive_state["available"] = False
            logger.warning("archive unavailable — run_id=%s not dumped (reason=%s)", run_id, reason)
            return False
        # A run whose ingest is BLOCKED cannot be certified complete, whatever the store
        # currently holds. The archive's watermark check compares DuckDB against the summary,
        # and rows stranded in a blocked partition are in neither — so a run archived in that
        # window was written `complete=True` while missing them, `run_is_pending` answered
        # False and /health showed `lag_runs: 0`. Measured 2026-08-07 (adv9 d3): 100 of 200
        # kpi rows archived as a closed run. An OPEN summary keeps the run pending, so the
        # sweep re-dumps it once the rewind has replayed the missing records.
        consumer = self._consumer
        blocked = consumer.blocked_topics() if consumer is not None else set()
        blocked.discard("run_status")  # carries no rows of its own
        try:
            result = archive.dump_run(
                self.duck, run_id, reason=reason, complete=False if blocked else None
            )
            if blocked:
                self._count("dumps_left_open_blocked_ingest")
                logger.warning(
                    "archived run_id=%s with an OPEN summary: ingest blocked on %s, so the "
                    "run cannot be certified complete yet",
                    run_id, sorted(blocked),
                )
            with self._archive_lock:
                self._archive_state["available"] = True
                self._archive_state["last_dump_at"] = time.time()
            self._count("dumps_ok")
            self._record_export_state(run_id, result)
            logger.info("archived run_id=%s reason=%s result=%s", run_id, reason, result)
            return True
        except Exception as exc:  # noqa: BLE001
            with self._archive_lock:
                self._archive_state["available"] = False
            self._count("dumps_failed")
            logger.exception("archive dump failed run_id=%s", run_id)
            self._record_export_state(run_id, None, error=f"{type(exc).__name__}: {exc}")
            return False

    def _record_export_state(
        self, run_id: str, result: Any, *, error: Optional[str] = None
    ) -> None:
        """Record the dump outcome in ``run_export_state``.

        ``run_export_state`` is what ``MongoArchive.run_is_terminal`` reads when ``run_meta``
        cannot answer; leaving it unwritten (it was dead code) meant an archived run could
        still look unfinished to the reconciler.
        """
        setter = getattr(self._duck, "set_export_state", None) if self._duck is not None else None
        if setter is None:
            return
        stats = result if isinstance(result, dict) else {}

        def _int_or_none(*keys: str) -> Optional[int]:
            for key in keys:
                value = stats.get(key)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return None
            return None

        try:
            if error is None:
                setter(
                    run_id,
                    status="exported",
                    row_count=_int_or_none("kpi_rows", "kpi_count"),
                    frame_count=_int_or_none("frames", "frame_count"),
                )
            else:
                setter(run_id, status="error", error=error[:500])
            self._count("export_state_writes")
        except Exception:  # noqa: BLE001 - reporting must not break the dump path
            logger.debug("export state write failed run_id=%s", run_id, exc_info=True)

    def reconcile_once(self) -> Dict[str, Any]:
        """Sweep: any run in DuckDB but not in Mongo gets dumped. A miss costs delay, not data."""
        archive = self.archive
        if archive is None:
            with self._archive_lock:
                self._archive_state["available"] = False
            self._refresh_pending()
            return {"checked": 0, "dumped": 0, "run_ids": [], "errors": {}}
        try:
            result = archive.reconcile(self.duck, reason="sweep")
            pending: List[str] = []
            try:
                # ``run_is_pending``, not ``archived_run_ids``: "does this run still need a
                # dump?", not "is its summary closed?". A sim killed mid-flight (CLAUDE.md §8,
                # the most common abnormal state) never sends a terminal, so its summary stays
                # OPEN forever and that run sat in ``pending`` for the life of the process
                # while ``archive_lagging`` 503'd an idle box. An open summary whose working
                # set has not moved is an unfinished run, not lag — and this method knows it.
                pending = [r for r in self.duck.list_run_ids() if archive.run_is_pending(self.duck, r)]
            except Exception:  # noqa: BLE001
                logger.debug("archive lag probe failed", exc_info=True)
            with self._archive_lock:
                self._archive_state["available"] = True
                if result.get("dumped"):
                    self._archive_state["last_dump_at"] = time.time()
            self._refresh_pending(pending)
            return result
        except Exception:  # noqa: BLE001
            with self._archive_lock:
                self._archive_state["available"] = False
            logger.exception("archive reconcile failed")
            self._refresh_pending()
            return {"checked": 0, "dumped": 0, "run_ids": [], "errors": {"*": "reconcile failed"}}

    # ------------------------------------------------------------------ supervised loops

    def _task_consumer(self, task) -> None:
        self.consumer.run(task)

    def _task_frames(self, task) -> None:
        """The frame-capture sweep: snapshot every resident run that moved.

        This is the only loop left that writes to DuckDB off the poll thread, and it exists
        because the hot tier is *sampled*, not because writing is slow: nobody publishes a
        frame, so somebody has to mint one every ``frame_interval_s``.
        """
        while not task.stopping:
            task.heartbeat()
            try:
                self.capture_frames()
            except Exception:  # noqa: BLE001 - a failed sweep must not end the loop
                logger.exception("frame capture sweep failed")
            if task.wait(max(self.frame_interval_s, 0.01)):
                break

    def _read_stream(self, path, params):
        """SSE dispatch for /live. Returns a generator or None."""
        from apps.dataplane import read_api

        return read_api.stream(self, path, params)

    def _read_api(self, path, params):
        """Dispatch a frontend read. Bound to the LAZY store so the server can start first."""
        from apps.dataplane import read_api

        return read_api.handle(self, path, params)

    def _task_reconcile(self, task) -> None:
        """Archive task: drain queued terminal runs often, sweep for missed ones rarely."""
        next_sweep = time.monotonic()
        while not task.stopping:
            task.heartbeat()
            self.drain_dumps()
            consumer = self._consumer
            if consumer is not None:
                try:
                    consumer.refresh_broker_lag()
                except Exception:  # noqa: BLE001 - lag is a signal, never a reason to stop archiving
                    logger.debug("broker lag refresh failed", exc_info=True)
            if time.monotonic() >= next_sweep:
                next_sweep = time.monotonic() + self.reconcile_interval_s
                self.reconcile_once()
            if task.wait(min(1.0, max(self.reconcile_interval_s, 0.01))):
                break

    def _task_watchdog(self, task) -> None:
        """Restart tasks that hang — a blocked thread is invisible to death-only restarts."""
        while not task.stopping:
            task.heartbeat()
            self.supervisor.watchdog_tick()
            if task.wait(1.0):
                break

    def _task_http(self, task) -> None:
        server = self.http_server()
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.5},
            name="dp-health-serve", daemon=True,
        )
        thread.start()
        try:
            while not task.stopping:
                task.heartbeat()
                if not thread.is_alive():
                    raise RuntimeError("health http server thread died")
                if task.wait(0.5):
                    break
        finally:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------ health

    def http_server(self):
        with self._init_lock:
            if self._http_server is None:
                self._http_server = make_health_server(
                    self.health_payload,
                    self.http_host,
                    self.http_port,
                    reader=self._read_api,
                    streamer=self._read_stream,
                )
            return self._http_server

    def _hot_stats(self) -> Dict[str, Any]:
        hot = self._hot
        if hot is None:
            return {"resident_runs": [], "trucks": 0, "frames": 0}
        try:
            stats = hot.stats()
            return {
                "resident_runs": list(hot.resident_runs()),
                "trucks": int(stats.get("trucks", 0)),
                "frames": int(stats.get("frames", 0)),
            }
        except Exception:  # noqa: BLE001 - reported, never swallowed into a plausible zero
            logger.exception("hot tier stats failed")
            return {"resident_runs": [], "trucks": 0, "frames": 0, "stats_unavailable": True}

    def _store_stats(self) -> Dict[str, Any]:
        """``rows_dropped`` is the one number here that is a loss.

        The synchronous write path has no drop path of its own — a write that fails raises out
        of the handler and its offset is not committed — so the only contributor left is a hot
        slab evicted by the LRU with positions the sweep never captured. Those positions are
        genuinely gone (``trip_geo`` offsets commit on receipt), so they are reported as
        dropped rows and ``/health`` fails on them.
        """
        duck = self._duck
        with self._stats_lock:
            base = {
                "db_path": None,
                "run_ids": 0,
                # Runs with rows but no terminal status: THE liveness signal. A headless run
                # (ORSIM_HEADLESS, the OpenRide default) publishes no truck_loc and so has no
                # hot slab, which is why residency can never answer "is ingest expected".
                "open_runs": 0,
                "rows_dropped": self.counters.get("positions_dropped", 0),
                # Projected so an operator can tell a store that is writing every frame from
                # one that is refusing all of them.
                "frame_write_failures": self.counters.get("frame_write_failures", 0),
            }
        if duck is None:
            return base
        base["db_path"] = str(getattr(duck, "db_path", None))
        try:
            base["run_ids"] = len(duck.list_run_ids())
        except Exception:  # noqa: BLE001
            pass
        try:
            base["open_runs"] = len(duck.open_run_ids())
        except Exception:  # noqa: BLE001
            pass
        return base

    def _consumer_stats(self) -> Dict[str, Any]:
        """Consumer stats, or a sentinel the verdict can see.

        Both ingest rules are guarded on fields the defaults leave empty, so returning ``{}``
        from a raising ``stats()`` silently DELETED every ingest verdict while ``/health`` kept
        answering 200 with a plausible all-zero consumer block.
        """
        consumer = self._consumer
        if consumer is None:
            return {}
        try:
            return dict(consumer.stats())
        except Exception:  # noqa: BLE001
            logger.exception("consumer stats failed")
            return {"stats_unavailable": True}

    def health_payload(self) -> Dict[str, Any]:
        with self._archive_lock:
            archive_state = dict(self._archive_state)
        archive_state["pending_run_ids"] = list(archive_state.get("pending_run_ids") or [])
        consumer_stats = self._consumer_stats()
        hot_stats = self._hot_stats()
        degraded = [f"task_stalled:{name}" for name in self.supervisor.stalled_tasks()]
        # A collaborator that cannot report is a fault in its own right: judged, not defaulted.
        if consumer_stats.pop("stats_unavailable", False):
            degraded.append("consumer_stats_unavailable")
        if hot_stats.pop("stats_unavailable", False):
            degraded.append("hot_stats_unavailable")
        return build_health_payload(
            self.supervisor.statuses(),
            uptime_s=time.monotonic() - self._started_at,
            consumer=consumer_stats,
            archive=archive_state,
            hot=hot_stats,
            store=self._store_stats(),
            degraded=degraded,
        )

    # ------------------------------------------------------------------ lifecycle

    def build_tasks(self) -> None:
        if self.supervisor.tasks():
            return
        # The consumer is deliberately NOT restart_on_stall: two threads polling the same
        # confluent Consumer object is worse than one stuck thread. It is reported instead.
        self.supervisor.add("consumer", self._task_consumer, stop_event=self._stop)
        self.supervisor.add("frames", self._task_frames, stop_event=self._stop, restart_on_stall=True)
        self.supervisor.add(
            "reconcile", self._task_reconcile, stop_event=self._stop, restart_on_stall=True
        )
        self.supervisor.add("watchdog", self._task_watchdog, stop_event=self._stop)
        if self.enable_http:
            self.supervisor.add("health-http", self._task_http, stop_event=self._stop)

    def start(self) -> None:
        self._started_at = time.monotonic()
        self.build_tasks()
        self.supervisor.start_all()
        logger.info(
            "dataplane started group=%s http=http://%s:%s",
            self.group_id, self.http_host, self.http_port,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Ordered shutdown. **A hard kill costs time, never data.**

        1. signal every task and join them;
        2. commit exactly the offsets whose writes returned, then close the consumer;
        3. checkpoint and close the store, then the archive.

        There is nothing to drain: a record is either written (and its offset committable) or
        it raised (and its offset was never advanced past), so anything unwritten at any
        moment is replayed by the broker on the next start. The consumer's own ``run()``
        deliberately neither commits nor closes in a ``finally``: it would destroy the object
        step 2 needs.
        """
        self._stop.set()
        self.supervisor.stop_all(timeout=float(timeout))
        consumer = self._consumer
        if consumer is not None:
            try:
                consumer.commit_safe(force=True)
            except Exception:  # noqa: BLE001
                logger.exception("final offset commit failed")
            try:
                consumer.close()
            except Exception:  # noqa: BLE001
                logger.debug("consumer close failed", exc_info=True)
        with self._init_lock:
            server, self._http_server = self._http_server, None
        if server is not None:
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        duck = self._duck
        if duck is not None:
            try:
                duck.checkpoint()
            except Exception:  # noqa: BLE001
                logger.exception("duckdb checkpoint failed")
            try:
                duck.close()
            except Exception:  # noqa: BLE001
                logger.exception("duck close failed")
        with self._archive_lock:
            archive = self._archive
            client, self._mongo_client = self._mongo_client, None
        if archive is not None:
            try:
                archive.close()
            except Exception:  # noqa: BLE001
                pass
        if client is not None:
            try:
                client.close()  # MongoArchive does not own the client we handed it
            except Exception:  # noqa: BLE001
                pass
        logger.info("dataplane stopped")

    def wait(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(0.5)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("DATAPLANE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    service = DataplaneService(db_path=os.environ.get("DATAPLANE_DUCKDB_PATH"))

    def _shutdown(signum, _frame):
        logger.info("received signal %s — shutting down", signum)
        service._stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    service.start()
    try:
        service.wait()
    finally:
        service.stop()
    return 0


# Default DuckDB location, for operators reading --help-less code.
DEFAULT_DB_PATH = os.environ.get(
    "DATAPLANE_DUCKDB_PATH", os.path.join(kpi_sink_settings["data_dir"], "dataplane.duckdb")
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
