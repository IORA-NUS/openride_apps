"""MongoDB frame archive for the dataplane.

Mongo is the durable record. DuckDB is a working set that evicts freely; a past run
is rehydrated from the documents written here, so this archive is read on every
past-run open and cannot rot unnoticed.

Document shape (decided by measurement — not up for redesign):

* ``dataplane_frames``        one document per FRAME, columns as ``bson.Binary``
* ``dataplane_kpi_events``    one document per kpi row
* ``dataplane_kpi_breakdown`` one document per ``kpi_breakdown_rows`` ROW
                              (``doc_type='row'``; snapshot-shaped docs use
                              ``doc_type='snapshot'``)
* ``dataplane_runs``          one summary document per run

Every ``_id`` is deterministic, so replaying the same run produces no duplicates:
frames use ``f"{run_id}:{frame_idx}"``, kpi rows ``f"{run_id}|{metric}|{iso}"`` and
breakdown rows ``f"{run_id}|{scope}|{iso}|{entity_id}"``.

**Revisable rows are UPSERTED; frames are insert-only.** A deterministic ``_id`` is not
the same thing as an immutable row: DuckDB's ``_UPSERT`` / ``_BREAKDOWN_UPSERT`` exist
precisely because ``AnalyticsManager.recompute_breakdowns_full`` re-persists the whole
per-haulier series at finalize and ends with the definitive ``final=True`` snapshot at
sim_clocks that were already written mid-run. An insert-only archive therefore froze the
mid-run snapshot forever (ACME empty_km 10.0 where the authoritative value was 873.4),
and since DuckDB evicts freely and Mongo is read on every past-run open, the stale value
came *back* into DuckDB on rehydrate. So kpi rows and breakdown rows go through
``bulk_write([ReplaceOne(..., upsert=True)], ordered=False)``. Frames stay
``insert_many(ordered=False)`` with duplicate keys (11000) counted and swallowed: they
are immutable, and a 1.26 M-row run must not turn into 2520 replaces.

The summary additionally carries a CONTENT watermark (``kpi_max_ingested_at`` /
``breakdown_max_ingested_at``, probed from the reader) because a revision leaves the row
COUNT unchanged — without it the count-equality short-circuit stopped the revised rows
from even being read.

**Archived is a watermark, never a boolean.** The summary document records what the
archive actually holds (``frame_count`` / ``frame_max`` / ``kpi_count`` /
``breakdown_count``) plus a ``complete`` flag that is only true once the run is known
terminal. :meth:`MongoArchive.reconcile` re-sweeps a run whenever DuckDB has moved
past that watermark, so dumping a still-running run cannot close it: an earlier
version wrote the summary unconditionally, which made the 300 s sweep archive the
live run's prefix and then skip it forever — losing everything after the first sweep
if the terminal ``run_status`` was missed. That is the exact failure this package
exists to eliminate.

This module NEVER deletes anything, from Mongo or from DuckDB.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Protocol, Sequence, Set, Tuple

import numpy as np
from bson import Binary
from pymongo import MongoClient, ReplaceOne
from pymongo.errors import BulkWriteError, DuplicateKeyError, PyMongoError

from apps.config import kpi_sink_settings
from apps.dataplane.contract.frame import KIND_KEYFRAME, Frame

logger = logging.getLogger(__name__)

FRAMES_COLLECTION = "dataplane_frames"
KPI_COLLECTION = "dataplane_kpi_events"
BREAKDOWN_COLLECTION = "dataplane_kpi_breakdown"
RUNS_COLLECTION = "dataplane_runs"

#: KPI rows are plain tuples so no module has to import another module's type.
KpiRow = Tuple[str, str, float, datetime]

#: ``run_export_state.status`` values that do NOT mean the run has finished.
_NON_TERMINAL_STATUSES = frozenset({"running", "in_progress", "inprogress", "pending", "started"})

#: Columns of ``kpi_breakdown_rows`` that identify a row.
_BREAKDOWN_KEY = ("scope", "sim_clock", "entity_id")

#: ``kpi_breakdown_rows`` columns in the order the DuckDB schema declares them.
#: Declared HERE and never imported from ``apps.dataplane.store.duck`` — the archive and
#: the store stay independent — but ``test_mongo_archive`` cross-checks the two tuples so
#: the duplication cannot drift.
_BREAKDOWN_COLUMNS = (
    "run_id",
    "scope",
    "sim_clock",
    "final",
    "entity_id",
    "haulier_id",
    "haulier_name",
    "num_orders_completed",
    "empty_km",
    "loaded_km",
    "total_km",
    "empty_ratio",
    "active_hours",
    "orders_per_day",
    "dual_cycle_count",
    "chain_opportunities",
    "dual_cycle_rate",
    "num_trucks",
    "payload",
    "ingested_at",
)

#: The same columns split by storage type, which is what the columnar readers need.
#: ``run_id`` is deliberately absent: the rehydrate binds it as a single parameter rather
#: than materialising one Python object reference per row.
_BREAKDOWN_STR_COLUMNS = ("scope", "entity_id", "haulier_id", "haulier_name", "payload")
_BREAKDOWN_TS_COLUMNS = ("sim_clock", "ingested_at")
_BREAKDOWN_BOOL_COLUMNS = ("final",)
_BREAKDOWN_NUM_COLUMNS = (
    "num_orders_completed",
    "empty_km",
    "loaded_km",
    "total_km",
    "empty_ratio",
    "active_hours",
    "orders_per_day",
    "dual_cycle_count",
    "chain_opportunities",
    "dual_cycle_rate",
    "num_trucks",
)

#: ``run_meta`` columns folded into the run summary so the archived uint8 haulier and
#: uint32 slot columns stay decodable from the durable record alone.
_SUMMARY_META_FIELDS = (
    "haulier_codes",
    "slot_map",
    "n_trucks",
    "status",
    "first_seen",
    "last_seen",
)

#: (duck table, summary key) for the content watermark that makes a REVISION visible.
_CONTENT_WATERMARKS = (
    ("kpi_events", "kpi_max_ingested_at"),
    ("kpi_breakdown_rows", "breakdown_max_ingested_at"),
)


class ArchiveError(Exception):
    """Base class for archive failures."""


class ArchiveUnavailable(ArchiveError):
    """Mongo could not be reached, or the driver raised."""


class ArchiveIncomplete(ArchiveError):
    """A dump could not archive everything the working set holds.

    The summary is still written — and written OPEN — so the next sweep retries; the
    caller must count the dump as FAILED and leave the run queued. The dump result dict
    is attached as :attr:`result` so a caller that wants the counts can still read them.
    """

    def __init__(self, message: str, result: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.result: Dict[str, Any] = result or {}


class BreakdownReadFailed(ArchiveError):
    """A reader that HAS the breakdown capability raised while supplying the rows.

    This is the third state that used to be collapsed into ``None``. The three are:
    ``None`` — the reader cannot supply breakdown rows at all; ``[]`` — the reader
    answered, and the answer is "no rows"; this exception — the answer is unknown.
    Only the first two may close a run.
    """


class DuckReader(Protocol):
    """Structural view of the DuckDB store this archive dumps from.

    Declared here (never imported from ``apps.dataplane.store.duck``) so the archive
    and the store stay independent — tests hand in a plain fake.

    Only the three methods below are required. The archive additionally *probes* for
    ``get_run_meta`` / ``get_export_state`` (is this run finished?), ``frame_range`` /
    ``kpi_row_count`` (how far has it got?) and ``read_breakdown_rows`` / ``query``
    (breakdown rows), and degrades gracefully when a reader lacks them.
    """

    def list_run_ids(self) -> List[str]:
        ...

    def iter_frames(self, run_id: str) -> Iterator[Frame]:
        ...

    def read_kpi_events(self, run_id: str) -> List[KpiRow]:
        ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _naive_utc(value: Any) -> Any:
    """Timestamps the way DuckDB stores them: naive UTC. Non-datetimes pass through.

    Mongo hands back tz-AWARE datetimes whenever the client is tz-aware. Feeding one to
    ``np.array([...], dtype="datetime64[us]")`` emits ``DeprecationWarning: parsing
    timezone aware datetimes is deprecated; this will raise an error in the future`` — and
    long before that it is a correctness bug, because an offset-carrying value lands at a
    different instant and therefore under a different primary key.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _float_or_nan(value: Any) -> float:
    """``None``/unparseable -> ``nan``, which DuckDB stores as SQL ``NULL``."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _str_column(rows: Sequence[Dict[str, Any]], key: str) -> np.ndarray:
    return np.array([row.get(key) for row in rows], dtype=object)


def _ts_column(rows: Sequence[Dict[str, Any]], key: str, fallbacks: Sequence[str] = ()) -> np.ndarray:
    values: List[Any] = []
    for row in rows:
        value = row.get(key)
        for alt in fallbacks:
            if value is not None:
                break
            value = row.get(alt)
        values.append(_naive_utc(value))
    return np.array(values, dtype="datetime64[us]")


def _bool_column(rows: Sequence[Dict[str, Any]], key: str) -> np.ndarray:
    return np.array([bool(row.get(key)) for row in rows], dtype=bool)


def _num_column(rows: Sequence[Dict[str, Any]], key: str) -> np.ndarray:
    return np.array([_float_or_nan(row.get(key)) for row in rows], dtype=np.float64)


def frame_to_doc(run_id: str, frame: Frame) -> Dict[str, Any]:
    """One document per frame, columns as little-endian ``bson.Binary`` blobs."""
    return {
        "_id": f"{run_id}:{int(frame.frame_idx)}",
        "run_id": run_id,
        "frame_idx": int(frame.frame_idx),
        "sim_time_ms": float(frame.sim_time_ms),
        "n": int(frame.n),
        "lng": Binary(np.ascontiguousarray(frame.lng, dtype="<f8").tobytes()),
        "lat": Binary(np.ascontiguousarray(frame.lat, dtype="<f8").tobytes()),
        "slot": Binary(np.ascontiguousarray(frame.slot, dtype="<u4").tobytes()),
        "state": Binary(np.ascontiguousarray(frame.state, dtype=np.uint8).tobytes()),
        "haulier": Binary(np.ascontiguousarray(frame.haulier, dtype=np.uint8).tobytes()),
        "_created": _utcnow(),
    }


def doc_to_frame(doc: Dict[str, Any]) -> Frame:
    """Inverse of :func:`frame_to_doc`. Arrays are copied so the Frame owns them."""
    return Frame(
        frame_idx=int(doc.get("frame_idx", 0)),
        sim_time_ms=float(doc.get("sim_time_ms", 0.0)),
        lng=np.frombuffer(bytes(doc["lng"]), dtype="<f8").astype(np.float64, copy=True),
        lat=np.frombuffer(bytes(doc["lat"]), dtype="<f8").astype(np.float64, copy=True),
        slot=np.frombuffer(bytes(doc["slot"]), dtype="<u4").astype(np.uint32, copy=True),
        state=np.frombuffer(bytes(doc["state"]), dtype=np.uint8).copy(),
        haulier=np.frombuffer(bytes(doc["haulier"]), dtype=np.uint8).copy(),
        kind=KIND_KEYFRAME,
    )


def kpi_row_to_doc(row: Sequence[Any], default_run_id: str = "") -> Dict[str, Any]:
    run_id = str(row[0] or default_run_id)
    metric = str(row[1])
    value = float(row[2])
    sim_clock = row[3]
    return {
        "_id": f"{run_id}|{metric}|{_iso(sim_clock)}",
        "run_id": run_id,
        "metric": metric,
        "value": value,
        "sim_clock": sim_clock,
        "_created": sim_clock,
        "_updated": sim_clock,
    }


def breakdown_row_to_doc(run_id: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One document per ``kpi_breakdown_rows`` ROW, keyed like the DuckDB primary key.

    Returns ``None`` for a row without an ``entity_id`` (nothing addressable to key on).
    """
    entity_id = row.get("entity_id")
    if entity_id is None:
        return None
    scope = str(row.get("scope") or "")
    sim_clock = row.get("sim_clock")
    doc: Dict[str, Any] = {k: v for k, v in row.items() if k != "_id"}
    doc["run_id"] = str(row.get("run_id") or run_id)
    doc["scope"] = scope
    doc["entity_id"] = str(entity_id)
    doc["doc_type"] = "row"
    doc["_id"] = f"{doc['run_id']}|{scope}|{_iso(sim_clock)}|{doc['entity_id']}"
    doc.setdefault("_created", _utcnow())
    return doc


class MongoArchive:
    """Durable frame/kpi/breakdown archive. Never deletes."""

    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        db_name: Optional[str] = None,
        client: Optional[MongoClient] = None,
    ) -> None:
        self._owns_client = client is None
        self.db_name = db_name or kpi_sink_settings["mongo_db"]
        if client is not None:
            self._client = client
        else:
            try:
                effective_uri = uri if uri is not None else kpi_sink_settings.get("mongo_uri")
                if effective_uri:
                    self._client = MongoClient(effective_uri)
                else:
                    self._client = MongoClient(
                        host or kpi_sink_settings["mongo_host"],
                        int(port or kpi_sink_settings["mongo_port"]),
                    )
            except PyMongoError as exc:  # pragma: no cover - constructor rarely raises
                raise ArchiveUnavailable(f"Could not create Mongo client: {exc}") from exc
        self._db = self._client[self.db_name]
        # Cache the collection handles: ``db[name]`` builds a fresh Collection object
        # on every access, which makes the handles impossible to wrap or patch.
        self._frames = self._db[FRAMES_COLLECTION]
        self._kpi = self._db[KPI_COLLECTION]
        self._breakdown = self._db[BREAKDOWN_COLLECTION]
        self._runs = self._db[RUNS_COLLECTION]
        self.last_dump_at: Optional[float] = None

    # ── plumbing ────────────────────────────────────────────────────────────

    @property
    def db(self):
        return self._db

    @property
    def frames(self):
        return self._frames

    @property
    def kpi(self):
        return self._kpi

    @property
    def breakdown(self):
        return self._breakdown

    @property
    def runs(self):
        return self._runs

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - close is best effort
                logger.debug("Mongo client close failed", exc_info=True)

    def ping(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except PyMongoError:
            return False
        except Exception:  # pragma: no cover - defensive: never escape into a loop
            return False

    def ensure_indexes(self) -> None:
        try:
            self.frames.create_index([("run_id", 1), ("frame_idx", 1)], name="dp_frames_run_idx")
            self.kpi.create_index([("run_id", 1), ("sim_clock", 1)], name="dp_kpi_run_clock")
            self.breakdown.create_index(
                [("run_id", 1), ("scope", 1), ("sim_clock", 1)], name="dp_bd_run_scope_clock"
            )
            self.runs.create_index([("dumped_at", -1)], name="dp_runs_dumped_at")
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"ensure_indexes failed: {exc}") from exc

    # ── writes ──────────────────────────────────────────────────────────────

    def _insert_batch(self, collection, docs: List[Dict[str, Any]]) -> int:
        """insert_many(ordered=False), swallowing duplicate keys. Returns inserted.

        Used for FRAMES and for :meth:`insert_documents` only — anything whose rows can
        be revised must go through :meth:`_replace_batch` instead.
        """
        if not docs:
            return 0
        try:
            result = collection.insert_many(docs, ordered=False)
            return len(result.inserted_ids)
        except BulkWriteError as exc:
            details = exc.details or {}
            inserted = int(details.get("nInserted", 0))
            write_errors = details.get("writeErrors") or []
            non_dup = [e for e in write_errors if e.get("code") != 11000]
            if non_dup:
                raise ArchiveUnavailable(f"bulk write failed: {non_dup[:3]}") from exc
            return inserted
        except DuplicateKeyError:
            return 0
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"insert_many failed: {exc}") from exc

    def _replace_batch(self, collection, docs: List[Dict[str, Any]]) -> int:
        """Upsert by ``_id``. Returns the number of documents that were NEW.

        A revised row (same deterministic ``_id``, different value) overwrites the
        archived document instead of being swallowed as a duplicate. The return value
        deliberately counts only upserts, so callers keep reading "how much did this
        dump add" the way they always did.

        Duplicate-key errors are still possible and still swallowed: two concurrent
        sweeps can race the same upsert, and the loser's payload is identical anyway.
        """
        if not docs:
            return 0
        ops = [ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in docs]
        try:
            result = collection.bulk_write(ops, ordered=False)
            return int(result.upserted_count or 0)
        except BulkWriteError as exc:
            details = exc.details or {}
            upserted = len(details.get("upserted") or [])
            write_errors = details.get("writeErrors") or []
            non_dup = [e for e in write_errors if e.get("code") != 11000]
            if non_dup:
                raise ArchiveUnavailable(f"bulk write failed: {non_dup[:3]}") from exc
            return upserted
        except DuplicateKeyError:
            return 0
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"bulk_write failed: {exc}") from exc

    def insert_documents(self, collection, docs: Sequence[Dict[str, Any]], *, batch_size: int = 1000) -> int:
        """Public escape hatch for tools that write their own document shape."""
        inserted = 0
        size = max(1, int(batch_size))
        docs = list(docs)
        for start in range(0, len(docs), size):
            inserted += self._insert_batch(collection, docs[start : start + size])
        return inserted

    def write_frames(self, run_id: str, frames: Iterable[Frame], *, batch_size: int = 500) -> int:
        """Write frames as one document each. Returns newly inserted document count."""
        batch: List[Dict[str, Any]] = []
        inserted = 0
        size = max(1, int(batch_size))
        for frame in frames:
            batch.append(frame_to_doc(run_id, frame))
            if len(batch) >= size:
                inserted += self._insert_batch(self.frames, batch)
                batch = []
        inserted += self._insert_batch(self.frames, batch)
        return inserted

    def write_kpi_events(self, run_id: str, rows: Iterable[Sequence[Any]], *, batch_size: int = 1000) -> int:
        """Upsert kpi rows. Returns how many were NEW (a revision returns 0, not a loss).

        A kpi value IS revised: the same ``(run_id, metric, sim_clock)`` is rewritten by
        the finalize recompute, which is why DuckDB's ``_UPSERT`` exists. Inserting and
        swallowing the duplicate froze the mid-run value in the durable record.
        """
        batch: List[Dict[str, Any]] = []
        inserted = 0
        size = max(1, int(batch_size))
        seen: Set[str] = set()
        for row in rows:
            doc = kpi_row_to_doc(row, run_id)
            # Deduplicate inside the batch: one bulk_write must not carry two ops for
            # the same _id (the second would race the first's upsert).
            if doc["_id"] in seen:
                continue
            seen.add(doc["_id"])
            batch.append(doc)
            if len(batch) >= size:
                inserted += self._replace_batch(self.kpi, batch)
                batch = []
        inserted += self._replace_batch(self.kpi, batch)
        return inserted

    def write_breakdown_rows(
        self, run_id: str, rows: Iterable[Dict[str, Any]], *, batch_size: int = 1000
    ) -> int:
        """Write ``kpi_breakdown_rows`` rows, one document each.

        These are the per-truck / per-haulier distribution rows behind the Companies
        views — ~35 k rows per real run. DuckDB evicts them freely, so if they are not
        here they are gone.

        Rows are UPSERTED: ``recompute_breakdowns_full`` rewrites the whole series at the
        same sim_clocks at finalize, so the last write for a key is the authoritative one.
        Returns how many rows were NEW.
        """
        batch: List[Dict[str, Any]] = []
        inserted = 0
        size = max(1, int(batch_size))
        seen: Set[str] = set()
        for row in rows:
            doc = breakdown_row_to_doc(run_id, row)
            if doc is None or doc["_id"] in seen:
                continue
            seen.add(doc["_id"])
            batch.append(doc)
            if len(batch) >= size:
                inserted += self._replace_batch(self.breakdown, batch)
                batch = []
        inserted += self._replace_batch(self.breakdown, batch)
        return inserted

    def write_breakdown_snapshot(
        self,
        run_id: str,
        scope: str,
        sim_clock: datetime,
        final: bool,
        entities: List[Dict[str, Any]],
    ) -> int:
        """Snapshot-shaped twin of :meth:`write_breakdown_rows` (``doc_type='snapshot'``)."""
        doc = {
            "_id": f"{run_id}|snapshot|{scope}|{_iso(sim_clock)}",
            "doc_type": "snapshot",
            "run_id": run_id,
            "scope": scope,
            "sim_clock": sim_clock,
            "final": bool(final),
            "entities": entities,
            "_created": _utcnow(),
        }
        try:
            self.breakdown.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"breakdown write failed: {exc}") from exc
        return len(entities)

    # ── reader probes (structural, never required) ──────────────────────────

    @staticmethod
    def _read_breakdown_rows(store: Any, run_id: str) -> Optional[List[Dict[str, Any]]]:
        """Breakdown rows for a run. THREE outcomes, never two:

        * ``None``  — this reader has no way to supply breakdown rows. Harmless: there is
          nothing to archive and nothing is claimed about the table.
        * ``[]``    — the reader answered, and the answer is "no rows". A real answer.
        * raises :class:`BreakdownReadFailed` — the reader HAS the capability and it blew
          up, so how many rows exist is UNKNOWN.

        Collapsing the third case into ``None`` is what let a dump that archived zero of
        34 689 rows write ``complete=True`` plus a content watermark: a swallowed failure
        was indistinguishable from an empty table, and the run was then reported CLOSED.
        """
        fn = getattr(store, "read_breakdown_rows", None)
        if callable(fn):
            try:
                return [dict(r) for r in fn(run_id)]
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                logger.warning("read_breakdown_rows failed run_id=%s", run_id, exc_info=True)
                raise BreakdownReadFailed(
                    f"read_breakdown_rows failed for run_id={run_id}: {type(exc).__name__}: {exc}"
                ) from exc
        query = getattr(store, "query", None)
        if callable(query):
            try:
                return [
                    dict(r)
                    for r in query(
                        "SELECT * FROM kpi_breakdown_rows WHERE run_id = ? "
                        "ORDER BY sim_clock, scope, entity_id",
                        [run_id],
                    )
                ]
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                logger.warning("breakdown query failed run_id=%s", run_id, exc_info=True)
                raise BreakdownReadFailed(
                    f"breakdown query failed for run_id={run_id}: {type(exc).__name__}: {exc}"
                ) from exc
        return None

    @staticmethod
    def _duck_breakdown_count(store: Any, run_id: str) -> Optional[int]:
        query = getattr(store, "query", None)
        if callable(query):
            try:
                rows = query(
                    "SELECT count(*) AS c FROM kpi_breakdown_rows WHERE run_id = ?", [run_id]
                )
                if rows:
                    return int(list(rows[0].values())[0])
                return 0
            except Exception:  # noqa: BLE001
                return None
        fn = getattr(store, "read_breakdown_rows", None)
        if callable(fn):
            try:
                return sum(1 for _ in fn(run_id))
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _probe_run_meta(store: Any, run_id: str) -> Dict[str, Any]:
        """``run_meta`` for a run, or ``{}`` when the reader cannot supply it.

        Probed, never required: ``FakeReader``-shaped readers and the preserve tool have
        no ``get_run_meta``, and a probe must never break a dump.
        """
        fn = getattr(store, "get_run_meta", None)
        if not callable(fn):
            return {}
        try:
            row = fn(run_id)
        except Exception:  # noqa: BLE001 - a probe must not kill the dump
            logger.warning("get_run_meta probe failed run_id=%s", run_id, exc_info=True)
            return {}
        return dict(row) if row else {}

    @staticmethod
    def _duck_max_ingested_at(store: Any, run_id: str, table: str) -> Optional[str]:
        """``max(ingested_at)`` for one table as an ISO string, or ``None``.

        This is the CONTENT watermark: a revised row leaves ``count(*)`` unchanged, so
        without it the count-equality short-circuit in :meth:`dump_run` stopped the
        revised rows from even being read. A reader that cannot answer (no ``query``, no
        such table, no such column) yields ``None`` and the old count-only behaviour is
        preserved for it.
        """
        query = getattr(store, "query", None)
        if not callable(query):
            return None
        try:
            rows = query(
                f"SELECT max(ingested_at) AS dp_wm FROM {table} WHERE run_id = ?", [run_id]
            )
        except Exception:  # noqa: BLE001 - reader may not have the table
            return None
        if not rows:
            return None
        first = rows[0]
        if not isinstance(first, dict) or "dp_wm" not in first:
            return None
        value = first["dp_wm"]
        return None if value is None else _iso(value)

    @staticmethod
    def _content_unchanged(archived: Optional[Any], duck: Optional[str]) -> bool:
        """May a table be skipped on the strength of the content watermark?

        Only when both sides answer and DuckDB is not newer, or when NEITHER side can
        answer (which keeps the existing, correct optimisation for readers that do not
        report ``ingested_at``). One side answering alone is never enough.
        """
        if duck is None and archived is None:
            return True
        if duck is None or archived is None:
            return False
        return str(duck) <= str(archived)

    @staticmethod
    def run_is_terminal(store: Any, run_id: str) -> Optional[bool]:
        """Has the run finished? ``None`` when the reader cannot say.

        ``run_meta.status`` is only written when a terminal ``run_status`` arrives, and
        ``run_export_state.status`` records the export outcome, so either one being set
        (and not an explicitly in-flight value) means the run is over.
        """
        probed = False
        get_meta = getattr(store, "get_run_meta", None)
        if callable(get_meta):
            probed = True
            try:
                status = (get_meta(run_id) or {}).get("status")
            except Exception:  # noqa: BLE001
                status = None
            if status and str(status).strip().lower() not in _NON_TERMINAL_STATUSES:
                return True
        get_export = getattr(store, "get_export_state", None)
        if callable(get_export):
            probed = True
            try:
                status = (get_export(run_id) or {}).get("status")
            except Exception:  # noqa: BLE001
                status = None
            if status and str(status).strip().lower() not in _NON_TERMINAL_STATUSES:
                return True
        return False if probed else None

    @classmethod
    def _duck_watermark(cls, store: Any, run_id: str) -> Dict[str, Optional[int]]:
        """How far the working set has got. Missing keys mean "cannot tell"."""
        wm: Dict[str, Optional[int]] = {}
        frame_range = getattr(store, "frame_range", None)
        if callable(frame_range):
            try:
                rng = frame_range(run_id)
            except Exception:  # noqa: BLE001
                rng = None
            wm["frame_max"] = int(rng[1]) if rng else None
        kpi_count = getattr(store, "kpi_row_count", None)
        if callable(kpi_count):
            try:
                wm["kpi_count"] = int(kpi_count(run_id))
            except Exception:  # noqa: BLE001
                pass
        bd = cls._duck_breakdown_count(store, run_id)
        if bd is not None:
            wm["breakdown_count"] = bd
        return wm

    # ── archive-side watermark ──────────────────────────────────────────────

    def _archive_frame_stats(self, run_id: str) -> Tuple[int, Optional[int], Optional[int]]:
        """``(count, min_frame_idx, max_frame_idx)`` actually present in Mongo."""
        try:
            count = int(self.frames.count_documents({"run_id": run_id}))
            if count == 0:
                return 0, None, None
            lo = self.frames.find({"run_id": run_id}, {"frame_idx": 1}).sort("frame_idx", 1).limit(1)
            hi = self.frames.find({"run_id": run_id}, {"frame_idx": 1}).sort("frame_idx", -1).limit(1)
            lo_doc = next(iter(lo), None)
            hi_doc = next(iter(hi), None)
            return (
                count,
                int(lo_doc["frame_idx"]) if lo_doc else None,
                int(hi_doc["frame_idx"]) if hi_doc else None,
            )
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"frame stats failed: {exc}") from exc

    @staticmethod
    def _already_whole(watermark: Dict[str, Optional[int]], key: str, archived: int) -> bool:
        """True when the archive already holds every row the working set has for ``key``."""
        if key not in watermark:
            return False
        expected = watermark.get(key)
        if expected is None:
            return False
        return archived > 0 and archived >= int(expected)

    def _count(self, collection, run_id: str, extra: Optional[Dict[str, Any]] = None) -> int:
        query: Dict[str, Any] = {"run_id": run_id}
        if extra:
            query.update(extra)
        try:
            return int(collection.count_documents(query))
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"count failed: {exc}") from exc

    # ── dump / reconcile ────────────────────────────────────────────────────

    def dump_run(
        self,
        store: DuckReader,
        run_id: str,
        *,
        reason: str = "",
        complete: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Copy one run's frames, kpi rows and breakdown rows out of DuckDB into Mongo.

        ``complete`` decides whether the summary CLOSES the run:

        * ``True`` / ``False`` — caller knows (terminal ``run_status`` handler passes nothing
          and lets the probe decide; a tool may force it).
        * ``None`` (default) — probe the reader (``run_meta.status`` / ``run_export_state``).
          A reader that cannot answer is treated as a closing dump, which keeps hand-written
          fakes and one-shot dumps behaving the way they read.

        A dump of a run that is still producing frames writes an OPEN summary, so the next
        sweep picks it up again. The summary is written LAST and describes what the archive
        actually holds, not what this one call transferred.

        The summary also carries the run's code books (``haulier_codes`` / ``slot_map`` /
        ``n_trucks``) probed from ``run_meta`` HERE, not bolted on by the caller: the
        reconcile sweep reaches this method directly, so a caller-side patch meant the
        sweep's ``replace_one`` blanked the code books — worst precisely in the
        missed-trigger case the reconciler exists for. The write is a ``$set`` upsert so
        no sweep can ever wipe a key it did not compute.

        **A breakdown read that RAISES does not close the run.** It forces the summary
        OPEN, withholds ``breakdown_max_ingested_at`` (so the next sweep re-reads the
        table rather than skipping it as "already whole"), reports
        ``breakdown_read_failed`` and then raises :class:`ArchiveIncomplete`. The summary
        is still written, because frames and kpi rows genuinely were archived and an open
        summary is exactly what makes the next sweep come back.
        """
        if complete is None:
            probed = self.run_is_terminal(store, run_id)
            terminal = True if probed is None else bool(probed)
        else:
            terminal = bool(complete)

        # Skip frames already durable: only when the archive holds a dense 0..max run,
        # otherwise write everything and let the deterministic _id swallow the dupes.
        prior_count, _prior_min, prior_max = self._archive_frame_stats(run_id)
        skip_upto = prior_max if (prior_max is not None and prior_count == prior_max + 1) else None

        scanned = 0

        def _selected(frames: Iterator[Frame]) -> Iterator[Frame]:
            nonlocal scanned
            for frame in frames:
                scanned += 1
                if skip_upto is not None and int(frame.frame_idx) <= skip_upto:
                    continue
                yield frame

        frames_inserted = self.write_frames(run_id, _selected(iter(store.iter_frames(run_id))))

        # A live run is swept every cycle; re-reading tables whose row count already
        # matches the archive would burn ~35 k dicts per sweep for nothing. Row _ids are
        # deterministic, so equal counts mean the archive is already whole for that table.
        # ... but ONLY when the content watermark also says nothing was revised: a
        # finalize recompute rewrites existing rows in place, leaving the count equal.
        watermark = self._duck_watermark(store, run_id)
        prior_summary = self.run_summary(run_id) or {}
        duck_kpi_wm = self._duck_max_ingested_at(store, run_id, "kpi_events")
        duck_bd_wm = self._duck_max_ingested_at(store, run_id, "kpi_breakdown_rows")

        kpi_whole = self._already_whole(
            watermark, "kpi_count", self._count(self.kpi, run_id)
        ) and self._content_unchanged(prior_summary.get("kpi_max_ingested_at"), duck_kpi_wm)
        if kpi_whole:
            rows: List[Any] = []
            kpi_inserted = 0
        else:
            rows = list(store.read_kpi_events(run_id))
            kpi_inserted = self.write_kpi_events(run_id, rows)

        archived_bd = self._count(self.breakdown, run_id, {"doc_type": "row"})
        bd_whole = self._already_whole(
            watermark, "breakdown_count", archived_bd
        ) and self._content_unchanged(
            prior_summary.get("breakdown_max_ingested_at"), duck_bd_wm
        )
        bd_rows: Optional[List[Dict[str, Any]]] = None
        bd_inserted = 0
        bd_read_failed = False
        bd_error: Optional[str] = None
        if not bd_whole:
            try:
                bd_rows = self._read_breakdown_rows(store, run_id)
            except BreakdownReadFailed as exc:
                # UNKNOWN, not zero. The run stays OPEN, no content watermark is written,
                # and the caller is told the dump failed so the run stays queued.
                bd_read_failed = True
                bd_error = str(exc)
                logger.error("breakdown read failed run_id=%s: %s", run_id, exc)
            else:
                bd_inserted = self.write_breakdown_rows(run_id, bd_rows) if bd_rows else 0
        if bd_read_failed:
            terminal = False

        frame_count, frame_min, frame_max = self._archive_frame_stats(run_id)
        kpi_count = self._count(self.kpi, run_id)
        breakdown_count = self._count(self.breakdown, run_id, {"doc_type": "row"})

        summary = {
            "_id": run_id,
            "run_id": run_id,
            "frame_count": frame_count,
            "kpi_count": kpi_count,
            "breakdown_count": breakdown_count,
            "frame_min": frame_min,
            "frame_max": frame_max,
            "complete": terminal,
            "reason": reason,
            "dumped_at": _utcnow(),
            # Recorded either way, so a later successful dump clears a stale True.
            "breakdown_read_failed": bd_read_failed,
        }
        # The code books that decode the archived uint8 haulier / uint32 slot columns.
        meta = self._probe_run_meta(store, run_id)
        for key in _SUMMARY_META_FIELDS:
            value = meta.get(key)
            if value is not None:
                summary[key] = value
        # The content watermark this dump has now made durable. Only recorded when the
        # reader actually answered — never blank a watermark we cannot recompute.
        for table, wm_key in _CONTENT_WATERMARKS:
            wm_value = duck_kpi_wm if table == "kpi_events" else duck_bd_wm
            if table == "kpi_breakdown_rows" and bd_read_failed:
                # Never claim rows are durable when the read of them failed: the watermark
                # comes from DuckDB, so writing it here would record 34 689 unarchived rows
                # as archived and skip the table on every later sweep.
                continue
            if wm_value is not None:
                summary[wm_key] = wm_value

        fields = {k: v for k, v in summary.items() if k != "_id"}
        try:
            # $set, never replace_one: a later sweep must not blank a key it did not
            # compute (the code books were lost exactly that way).
            self.runs.update_one({"_id": run_id}, {"$set": fields}, upsert=True)
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"run summary write failed: {exc}") from exc

        self.last_dump_at = datetime.now(timezone.utc).timestamp()
        log = logger.warning if bd_read_failed else logger.info
        log(
            "Archived run_id=%s frames=%d (+%d new) kpi=%d (+%d new) breakdown=%d (+%d new) "
            "complete=%s reason=%s breakdown_read_failed=%s",
            run_id,
            frame_count,
            frames_inserted,
            kpi_count,
            kpi_inserted,
            breakdown_count,
            bd_inserted,
            terminal,
            reason or "n/a",
            bd_read_failed,
        )
        result = {
            "run_id": run_id,
            "frames": frame_count,
            "frames_scanned": scanned,
            "frames_inserted": frames_inserted,
            "kpi_rows": len(rows),
            "kpi_inserted": kpi_inserted,
            "breakdown_rows": len(bd_rows) if bd_rows is not None else 0,
            "breakdown_inserted": bd_inserted,
            "frame_min": frame_min,
            "frame_max": frame_max,
            "complete": terminal,
            "reason": reason,
            "breakdown_read_failed": bd_read_failed,
        }
        if bd_read_failed:
            # The OPEN summary above is what makes the next sweep retry; this is what makes
            # the caller count a FAILED dump and leave the run queued. Frames and kpi rows
            # really were archived, so nothing is undone — the run is simply not finished.
            raise ArchiveIncomplete(
                f"run_id={run_id} archived without its breakdown rows: {bd_error}", result
            )
        return result

    def archived_run_ids(self) -> Set[str]:
        """Runs whose archive is CLOSED (terminal dump written).

        A run that has only been swept while still live is deliberately absent: it is
        not finished being archived, and reporting it as archived is what silently lost
        data before.
        """
        try:
            return {
                str(doc["_id"])
                for doc in self.runs.find({}, {"_id": 1, "complete": 1})
                if doc.get("complete", True)
            }
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"archived_run_ids failed: {exc}") from exc

    def run_is_pending(self, store: Any, run_id: str, summary: Optional[Dict[str, Any]] = None) -> bool:
        """Does this run still need a dump? Watermark comparison, not mere presence."""
        if summary is None:
            summary = self.run_summary(run_id)
        if summary is None:
            return True
        if self._duck_moved_past(store, run_id, summary):
            return True
        if not summary.get("complete", True):
            # An OPEN summary still needs a closing dump once the run is terminal. While
            # the run is genuinely live with nothing new, re-dumping it every cycle buys
            # nothing — it stays in ``pending_run_ids`` so /health keeps showing the lag.
            terminal = self.run_is_terminal(store, run_id)
            return terminal is None or bool(terminal)
        return False

    def _duck_moved_past(self, store: Any, run_id: str, summary: Dict[str, Any]) -> bool:
        """Has the working set produced rows the archive does not hold?"""
        wm = self._duck_watermark(store, run_id)
        duck_frame_max = wm.get("frame_max", None)
        if "frame_max" in wm and duck_frame_max is not None:
            archived_max = summary.get("frame_max")
            if archived_max is None or int(duck_frame_max) > int(archived_max):
                return True
        if "kpi_count" in wm and int(wm["kpi_count"] or 0) > int(summary.get("kpi_count") or 0):
            return True
        if "breakdown_count" in wm and int(wm["breakdown_count"] or 0) > int(
            summary.get("breakdown_count") or 0
        ):
            return True
        # A REVISION moves nothing forward by count: the finalize recompute rewrites the
        # same keys. Without this the sweep could never repair a stale archived value.
        for table, wm_key in _CONTENT_WATERMARKS:
            duck_wm = self._duck_max_ingested_at(store, run_id, table)
            if duck_wm is None:
                continue
            archived_wm = summary.get(wm_key)
            if archived_wm is None or str(duck_wm) > str(archived_wm):
                return True
        return False

    def reconcile(
        self,
        store: DuckReader,
        *,
        run_ids: Optional[Iterable[str]] = None,
        reason: str = "sweep",
    ) -> Dict[str, Any]:
        """Dump every run whose archive is behind what DuckDB holds.

        A missed run-end trigger costs a delay, never the data: a live run swept here
        gets an OPEN summary and is swept again next cycle, catching up incrementally
        until a terminal dump closes it. Never raises for a single run's failure and
        never deletes anything.
        """
        if run_ids is None:
            candidates = [str(r) for r in store.list_run_ids()]
        else:
            candidates = [str(r) for r in run_ids]

        summaries = self._summaries(candidates)
        pending = [r for r in candidates if self.run_is_pending(store, r, summaries.get(r))]

        dumped: List[str] = []
        errors: Dict[str, str] = {}
        for run_id in pending:
            try:
                self.dump_run(store, run_id, reason=reason)
                dumped.append(run_id)
            except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
                errors[run_id] = f"{type(exc).__name__}: {exc}"
                logger.warning("Reconcile failed for run_id=%s: %s", run_id, exc, exc_info=True)
        # Pending = every run whose archive is not CLOSED: failed dumps and still-live
        # runs both stay visible, so /health cannot show green over an unarchived run.
        after = self._summaries(candidates)
        still_pending = [
            r for r in candidates if not (after.get(r) or {}).get("complete", False)
        ]
        return {
            "checked": len(candidates),
            "dumped": len(dumped),
            "run_ids": dumped,
            "errors": errors,
            "pending_run_ids": sorted(set(still_pending)),
        }

    def _summaries(self, run_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        if not run_ids:
            return {}
        try:
            return {
                str(doc["_id"]): doc
                for doc in self.runs.find({"_id": {"$in": list(run_ids)}})
            }
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"summary lookup failed: {exc}") from exc

    # ── FrameSource (read side) ─────────────────────────────────────────────

    def has_run(self, run_id: str) -> bool:
        """Does the archive hold DATA for this run?

        The ``dataplane_runs`` summary alone is deliberately NOT sufficient. A summary is
        written for every swept run including empty ones, and ``DuckStore.rehydrate_run``
        uses this as its "the source can replace what I am about to delete" guard — a
        kpi-only headless run was cleared against a source holding nothing at all.
        """
        try:
            if self.frames.find_one({"run_id": run_id}, {"_id": 1}) is not None:
                return True
            if self.kpi.find_one({"run_id": run_id}, {"_id": 1}) is not None:
                return True
            return self.breakdown.find_one({"run_id": run_id}, {"_id": 1}) is not None
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"has_run failed: {exc}") from exc

    def read_run_frames(self, run_id: str) -> Iterator[Frame]:
        try:
            cursor = self.frames.find({"run_id": run_id}).sort("frame_idx", 1)
            for doc in cursor:
                yield doc_to_frame(doc)
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"read_run_frames failed: {exc}") from exc

    def read_run_kpi_events(self, run_id: str) -> Iterator[KpiRow]:
        try:
            cursor = self.kpi.find({"run_id": run_id}).sort([("sim_clock", 1), ("metric", 1)])
            for doc in cursor:
                yield (
                    str(doc.get("run_id", run_id)),
                    str(doc["metric"]),
                    float(doc["value"]),
                    doc["sim_clock"],
                )
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"read_run_kpi_events failed: {exc}") from exc

    def read_run_breakdown_rows(self, run_id: str) -> Iterator[Dict[str, Any]]:
        """Yield ``kpi_breakdown_rows``-shaped dicts, ready to re-insert into DuckDB."""
        try:
            cursor = self.breakdown.find({"run_id": run_id, "doc_type": "row"}).sort(
                [("sim_clock", 1), ("scope", 1), ("entity_id", 1)]
            )
            for doc in cursor:
                row = {k: v for k, v in doc.items() if k not in ("_id", "doc_type", "_created")}
                yield row
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"read_run_breakdown_rows failed: {exc}") from exc

    # ── columnar read side (what a fast rehydrate consumes) ─────────────────
    #
    # Row-at-a-time is the reason a real run's rehydrate measured 225.6 s. These three
    # methods hand back whole-run columns instead, so the store can do one registered-
    # numpy ``INSERT ... SELECT`` per table. They are OPTIONAL: the store probes for them
    # with ``getattr`` and falls back to the row readers above, so a hand-written source
    # keeps working.

    def read_run_frame_docs(self, run_id: str) -> Iterator[Dict[str, Any]]:
        """Raw frame documents, sorted by ``frame_idx`` — NOT :func:`doc_to_frame`.

        The caller does its own ``np.frombuffer`` on the ``bson.Binary`` columns
        (``Binary`` is a ``bytes`` subclass, so that view is zero-copy and the caller's
        subsequent ``concatenate`` produces owned, writable memory). Building a ``Frame``
        per document first would allocate 2 520 intermediate objects and copy every column
        twice for nothing.
        """
        try:
            cursor = self.frames.find({"run_id": run_id}).sort("frame_idx", 1)
            for doc in cursor:
                yield doc
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"read_run_frame_docs failed: {exc}") from exc

    def read_run_kpi_columns(self, run_id: str) -> Dict[str, np.ndarray]:
        """Whole-run ``kpi_events`` columns. Empty run -> empty, correctly-typed columns.

        ``ingested_at`` is not part of the archived kpi document (the durable record keys
        on ``(run_id, metric, sim_clock)``), so it falls back to ``_updated``/``_created``
        and finally to now — mirroring what the store's row path does with a missing value,
        which keeps the content watermark answerable after a rehydrate.
        """
        try:
            rows = list(self.kpi.find({"run_id": run_id}).sort([("sim_clock", 1), ("metric", 1)]))
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"read_run_kpi_columns failed: {exc}") from exc
        now = _utcnow()
        for row in rows:
            if row.get("ingested_at") is None and row.get("_updated") is None and row.get("_created") is None:
                row["_created"] = now
        return {
            "metric": np.array([str(r.get("metric", "")) for r in rows], dtype=object),
            "value": _num_column(rows, "value"),
            "sim_clock": _ts_column(rows, "sim_clock"),
            "ingested_at": _ts_column(rows, "ingested_at", ("_updated", "_created")),
            # "last one wins" must be exact and deterministic; ingested_at is NOT, because
            # the live path stamps one timestamp for a whole batch.
            "_seq": np.arange(len(rows), dtype=np.int64),
        }

    def read_run_breakdown_columns(self, run_id: str) -> Optional[Dict[str, np.ndarray]]:
        """Whole-run ``kpi_breakdown_rows`` columns, or ``None`` if the read FAILED.

        The same three-way distinction :meth:`_read_breakdown_rows` makes, from the other
        end of the pipe: an empty-but-present column dict means "this run has no breakdown
        rows" and lets the caller clear the table; ``None`` means "unknown", and a caller
        must NOT delete rows it cannot replace. Collapsing the two is how a durable table
        gets destroyed by a Mongo blip.

        ``run_id`` is deliberately not a column: the caller binds it once as a parameter
        (measured 0.295 s vs 0.554 s for 1.26 M rows, and it allocates no per-row object).
        """
        try:
            rows = list(
                self.breakdown.find({"run_id": run_id, "doc_type": "row"}).sort(
                    [("sim_clock", 1), ("scope", 1), ("entity_id", 1)]
                )
            )
        except Exception:  # noqa: BLE001 - unknown, not empty
            logger.warning("read_run_breakdown_columns failed run_id=%s", run_id, exc_info=True)
            return None
        now = _utcnow()
        for row in rows:
            if row.get("ingested_at") is None and row.get("_created") is None:
                row["_created"] = now
        cols: Dict[str, np.ndarray] = {}
        for key in _BREAKDOWN_STR_COLUMNS:
            cols[key] = _str_column(rows, key)
        for key in _BREAKDOWN_TS_COLUMNS:
            cols[key] = _ts_column(rows, key, ("_created",) if key == "ingested_at" else ())
        for key in _BREAKDOWN_BOOL_COLUMNS:
            cols[key] = _bool_column(rows, key)
        for key in _BREAKDOWN_NUM_COLUMNS:
            cols[key] = _num_column(rows, key)
        cols["_seq"] = np.arange(len(rows), dtype=np.int64)
        return cols

    def run_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.runs.find_one({"_id": run_id})
        except PyMongoError as exc:
            raise ArchiveUnavailable(f"run_summary failed: {exc}") from exc

    def stats(self) -> Dict[str, Any]:
        try:
            return {
                "available": True,
                "db": self.db_name,
                "runs": self.runs.estimated_document_count(),
                "frames": self.frames.estimated_document_count(),
                "kpi": self.kpi.estimated_document_count(),
                "breakdown": self.breakdown.estimated_document_count(),
                "last_dump_at": self.last_dump_at,
            }
        except PyMongoError as exc:
            return {"available": False, "error": str(exc), "last_dump_at": self.last_dump_at}
