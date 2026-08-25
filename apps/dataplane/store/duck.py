"""Single-database DuckDB store for the dataplane.

This module exists to make the 2026-07-01 outage *unrepresentable*.

Root cause of that outage (``apps/kpi_sink/duckdb_store.py``): the store kept one cached
DuckDB connection **per run_id**, and ``_connection()`` — which lazily created the file and
ran ``CREATE TABLE`` — was reachable from ``flush()`` / ``fetch_all()`` / ``row_count()`` /
``get_export_state()`` / ``set_export_state()`` **outside** the lock.  Two threads
first-touching the same run both ran the schema DDL, DuckDB raised ``TransactionException``,
and because the consuming thread only caught ``KafkaException`` the thread died silently.

Three structural properties are non-negotiable:

(a) **One database file for every run.**  ``run_id`` is a column.  There is no per-run
    connection cache, so there is nothing to race on.
(b) ``self._conn`` is read in **exactly one place** — :meth:`DuckStore._cursor` — and that
    method takes ``self._conn_lock`` *itself*.  This is strictly stronger than the previous
    "every caller must remember to hold the lock" convention: an unlocked connection
    acquisition is no longer expressible, and the invariant is grep-checkable (one
    ``self._conn`` read in the file).
(c) **The unit of mutual exclusion is the run, not the store.**  Verified on duckdb 1.5.3:
    cursors of one connection have independent transactions *and* independent
    registered-view namespaces; two transactions touching **disjoint** rows of a table both
    commit, while two touching the **same** rows raise
    ``TransactionException: Conflict on update!`` — the exact 2026-07-01 exception.  So
    serialising per ``run_id`` is what correctness actually needs; the old global RLock
    additionally serialised every *read* (``/health``, ``get_run_meta`` on the poll thread)
    behind a 225 s rehydrate, for no safety gain.

Lock rules, all grep-checkable:

* ``_conn_lock`` guards ``_conn`` creation/close, ``_closed``, the thread-local cursor and
  the ``_run_locks`` map.  It is held for microseconds only — never across SQL.
* Statements execute on a **thread-local cursor, outside** ``_conn_lock``.
* Mutations take that run's lock (``_run_lock(run_id)``); reads take **no** run lock —
  DuckDB gives them a consistent snapshot.
* Lock order is always run lock -> conn lock.  ``_run_lock()`` releases ``_conn_lock``
  before returning, so a run lock is never acquired while holding the conn lock, and
  ``write_kpi_events`` takes each run's lock one at a time, so two run locks are never held
  simultaneously.  Deadlock is not expressible.

**Row writes are columnar.**  ``executemany`` over an ``ON CONFLICT`` upsert measures
~350 rows/s on this box (35 000 breakdown rows = 166 s, with the store lock held, inside a
6-minute run).  Every write path now registers a DataFrame over numpy columns and issues one
``INSERT … SELECT … QUALIFY … ON CONFLICT`` with ``run_id`` bound as a parameter — measured
~105 000 rows/s, and byte-identical in semantics (``None``/NaN -> NULL, ``3.7`` -> BIGINT 4,
last occurrence in a batch wins).

Nothing is imported from ``apps.kpi_sink`` (it is being retired).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

import duckdb
import numpy as np
import pandas as pd

from apps.config import kpi_sink_settings
from apps.dataplane.contract.frame import KIND_KEYFRAME, Frame

logger = logging.getLogger(__name__)

RowTuple = Tuple[str, str, float, datetime]

#: One breakdown snapshot as handed to :meth:`DuckStore.write_breakdown_batch`.
Snapshot = Tuple[str, datetime, bool, List[Dict[str, Any]]]


class DuckStoreError(Exception):
    """Any failure raised by :class:`DuckStore`."""


class RunNotInStore(DuckStoreError):
    """A run was asked for that is neither in the store nor in the rehydration source."""


class FrameSource(Protocol):
    """Structural type implemented by ``apps.dataplane.archive.mongo.MongoArchive``.

    Declared here (never imported) so ``store/duck.py`` and ``archive/mongo.py`` stay
    free of a cyclic dependency.

    Four further methods are **optional** and probed with ``getattr`` by
    :meth:`DuckStore.rehydrate_run` — they are deliberately *not* Protocol members, so a
    minimal hand-written source keeps satisfying this type:

    ``read_run_frame_docs(run_id) -> Iterator[Dict[str, Any]]``
        Raw frame documents (``frame_idx`` / ``sim_time_ms`` / ``n`` and the five column
        blobs as ``bytes``) in ``frame_idx`` order.  This is the *fast* path: the blobs go
        straight through ``np.frombuffer`` into the insert, never becoming ``Frame``
        objects.  Absent -> ``read_run_frames`` is used instead.

    ``read_run_kpi_columns(run_id) -> Optional[Mapping[str, Sequence]]``
        ``metric`` / ``value`` / ``sim_clock`` (and optionally ``ingested_at``) as parallel
        sequences.  ``None`` or absent -> ``read_run_kpi_events`` is used instead.

    ``read_run_breakdown_columns(run_id) -> Optional[Mapping[str, Sequence]]``
    ``read_run_breakdown_rows(run_id) -> Iterator[Dict[str, Any]]``
        ``kpi_breakdown_rows``-shaped data.  When **neither** is available (or the columnar
        reader explicitly answers ``None``, meaning "I cannot supply these") the existing
        ``kpi_breakdown_rows`` for the run are left **untouched**: a source that cannot
        replace those rows must never be allowed to destroy them.  A reader that *raises*
        aborts the rehydrate in phase 1, before any DELETE — also leaving them untouched.

    ``run_summary(run_id) -> Optional[Dict[str, Any]]``
        The archive's run summary.  ``haulier_codes`` / ``slot_map`` / ``n_trucks`` /
        ``status`` / ``first_seen`` are restored into ``run_meta`` (without the code books
        the archived uint8 ``haulier`` and uint32 ``slot`` columns are uninterpretable, and
        without ``status`` a recovered finished run re-opens as live), and ``complete``
        restores ``run_export_state``.
    """

    def has_run(self, run_id: str) -> bool: ...

    def read_run_frames(self, run_id: str) -> Iterator[Frame]: ...

    def read_run_kpi_events(self, run_id: str) -> Iterator[RowTuple]: ...


# ─────────────────────────────────────────────────────────────────────────────
# Schema — one database, run_id is a column.
#
# ``frames`` deliberately has NO primary key and NO index: zone maps over the
# (run_id, frame_idx) insertion order do the pruning, and an index would destroy
# append throughput.  Idempotence for frames is delete-then-insert per run inside
# ``rehydrate_run``.
# ─────────────────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS kpi_events (
    run_id VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    value DOUBLE NOT NULL,
    sim_clock TIMESTAMP NOT NULL,
    ingested_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, metric, sim_clock)
);

CREATE TABLE IF NOT EXISTS kpi_breakdown_rows (
    run_id VARCHAR NOT NULL,
    scope VARCHAR NOT NULL,
    sim_clock TIMESTAMP NOT NULL,
    final BOOLEAN DEFAULT FALSE,
    entity_id VARCHAR NOT NULL,
    haulier_id VARCHAR,
    haulier_name VARCHAR,
    num_orders_completed BIGINT,
    empty_km DOUBLE,
    loaded_km DOUBLE,
    total_km DOUBLE,
    empty_ratio DOUBLE,
    active_hours DOUBLE,
    orders_per_day DOUBLE,
    dual_cycle_count BIGINT,
    chain_opportunities BIGINT,
    dual_cycle_rate DOUBLE,
    num_trucks BIGINT,
    payload VARCHAR,
    ingested_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, scope, sim_clock, entity_id)
);

CREATE TABLE IF NOT EXISTS frames (
    run_id VARCHAR NOT NULL,
    frame_idx UINTEGER NOT NULL,
    sim_time_ms DOUBLE NOT NULL,
    slot UINTEGER NOT NULL,
    lng DOUBLE NOT NULL,
    lat DOUBLE NOT NULL,
    state UTINYINT NOT NULL,
    haulier UTINYINT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_meta (
    run_id VARCHAR PRIMARY KEY,
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    status VARCHAR,
    n_trucks INTEGER,
    max_frame_idx UINTEGER,
    haulier_codes VARCHAR,
    slot_map VARCHAR,
    source VARCHAR,
    run_name VARCHAR,
    scenario_slug VARCHAR,
    scenario_name VARCHAR
);

-- Added 2026-08-07 for /runs/live. ADD COLUMN IF NOT EXISTS keeps an existing database
-- readable without a migration step; CREATE TABLE IF NOT EXISTS above would silently skip
-- the new columns on a store that already exists.
ALTER TABLE run_meta ADD COLUMN IF NOT EXISTS run_name VARCHAR;
ALTER TABLE run_meta ADD COLUMN IF NOT EXISTS scenario_slug VARCHAR;
ALTER TABLE run_meta ADD COLUMN IF NOT EXISTS scenario_name VARCHAR;

CREATE TABLE IF NOT EXISTS run_export_state (
    run_id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL,
    row_count BIGINT,
    frame_count BIGINT,
    exported_at TIMESTAMP,
    error VARCHAR
);
"""

_EXPORT_STATE_UPSERT = """
INSERT INTO run_export_state (run_id, status, row_count, frame_count, exported_at, error)
VALUES (?, ?, ?, ?, current_timestamp, ?)
ON CONFLICT (run_id) DO UPDATE SET
    status = excluded.status,
    row_count = excluded.row_count,
    frame_count = excluded.frame_count,
    exported_at = excluded.exported_at,
    error = excluded.error
"""

# ── columnar INSERTs ─────────────────────────────────────────────────────────
#
# Shape, for all three tables:
#
#   INSERT INTO <t> (run_id, <cols>) SELECT ?, <cols> FROM <registered view>
#   QUALIFY row_number() OVER (PARTITION BY <key minus run_id> ORDER BY _seq DESC) = 1
#   ON CONFLICT (<pk>) DO UPDATE SET <non-key> = excluded.<non-key>
#
# * ``run_id`` is a BIND PARAMETER, never a column of the view: measured 0.295 s vs
#   0.554 s for 1.26 M frame rows, and it stops allocating 1.26 M Python object refs.
# * ``_seq = np.arange(n)`` with ``ORDER BY _seq DESC`` is what makes "the last occurrence
#   in the batch wins" exact — the property successive ``executemany`` upserts gave for
#   free.  ``ORDER BY ingested_at DESC`` would NOT be deterministic: the live path stamps
#   one ``datetime.utcnow()`` for a whole batch.
_FRAME_INSERT = (
    "INSERT INTO frames (run_id, frame_idx, sim_time_ms, slot, lng, lat, state, haulier) "
    "SELECT ?, frame_idx, sim_time_ms, slot, lng, lat, state, haulier FROM dp_frames"
)

# ``coalesce(value, 'nan'::DOUBLE)``: ``kpi_events.value`` is NOT NULL and DuckDB maps a
# numpy NaN to SQL NULL, whereas the ``executemany`` path this replaces bound a NaN through
# as a DOUBLE NaN.  The column cannot legitimately contain NULL, so the coalesce restores
# exactly the old behaviour instead of turning a junk metric into a constraint failure that
# the writer would then retry forever.
_KPI_INSERT = """
INSERT INTO kpi_events (run_id, metric, value, sim_clock, ingested_at)
SELECT ?, metric, coalesce(value, 'nan'::DOUBLE), sim_clock, ingested_at FROM dp_kpi
QUALIFY row_number() OVER (PARTITION BY metric, sim_clock ORDER BY _seq DESC) = 1
ON CONFLICT (run_id, metric, sim_clock) DO UPDATE SET
    value = excluded.value,
    ingested_at = excluded.ingested_at
"""

#: ``kpi_breakdown_rows`` columns other than ``run_id``, in insert order.
_BD_COLUMNS = (
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
#: Breakdown scopes this table holds. Kept in step with ``service.BREAKDOWN_SCOPES`` and
#: duplicated rather than imported: ``service`` must stay importable without duckdb.
_STORED_SCOPES = ("truck", "haulier", "planner", "lane")
#: Primary key minus ``run_id`` — the QUALIFY / ON CONFLICT key.
_BD_KEY = ("scope", "sim_clock", "entity_id")
_BD_STR = ("scope", "entity_id", "haulier_id", "haulier_name", "payload")
_BD_TS = ("sim_clock", "ingested_at")
_BD_BOOL = ("final",)
_BD_NUM = tuple(c for c in _BD_COLUMNS if c not in _BD_STR + _BD_TS + _BD_BOOL)
#: The BIGINT ones.  Every numeric column arrives as float64, and orjson happily parses
#: ``1e30`` (and silently widens any integer over 2**63 to a float), so a single junk
#: metric in one entity would abort the whole INSERT with ``Conversion Error: … out of
#: range for the destination type INT64`` and wedge the writer on that item forever.
#: ``try_cast`` stores NULL for a value the column cannot hold — exactly what ``None``
#: already does — while rounding in-range values identically to ``cast`` (3.7 -> 4).
_BD_INT = ("num_orders_completed", "dual_cycle_count", "chain_opportunities", "num_trucks")


def _column_kind(col: str) -> str:
    if col in _BD_STR:
        return "str"
    if col in _BD_TS:
        return "ts"
    if col in _BD_BOOL:
        return "bool"
    return "num"


#: (column, kind) for every column a source-supplied column mapping may carry.
_BD_COLUMN_SPEC = tuple((c, _column_kind(c)) for c in _BD_COLUMNS)
_KPI_COLUMN_SPEC = (
    ("metric", "str"),
    ("value", "num"),
    ("sim_clock", "ts"),
    ("ingested_at", "ts"),
)

_TS_DTYPE = np.dtype("datetime64[us]")

_BREAKDOWN_INSERT = (
    "INSERT INTO kpi_breakdown_rows (run_id, " + ", ".join(_BD_COLUMNS) + ") "
    "SELECT ?, "
    + ", ".join(f"try_cast({c} AS BIGINT) AS {c}" if c in _BD_INT else c for c in _BD_COLUMNS)
    + " FROM dp_bd "
    "QUALIFY row_number() OVER "
    "(PARTITION BY scope, sim_clock, entity_id ORDER BY _seq DESC) = 1 "
    "ON CONFLICT (run_id, scope, sim_clock, entity_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _BD_COLUMNS if c not in _BD_KEY)
)

#: ``run_summary()`` keys restored into ``run_meta`` on rehydrate.  ``status`` and
#: ``first_seen`` are what stop a recovered finished run from re-opening as live.
_SUMMARY_META_KEYS = ("haulier_codes", "slot_map", "n_trucks", "status", "first_seen")

_RUN_META_COLUMNS = (
    "first_seen",
    "last_seen",
    "status",
    "n_trucks",
    "max_frame_idx",
    "haulier_codes",
    "slot_map",
    "source",
    # Identity from run_status. This tuple is a WHITELIST: adding the columns to the schema
    # without adding them here made every write raise "unknown run_meta column(s)", and the
    # caller logged it at debug — a silent failure of exactly the kind this package exists to
    # eliminate. Keep the two in step.
    "run_name",
    "scenario_slug",
    "scenario_name",
)

#: Every table ``evict_run`` clears.  ``run_export_state`` is included so a stale
#: "exported" row cannot mask a missing ``run_meta.status`` after an eviction.
_RUN_TABLES = ("frames", "kpi_events", "kpi_breakdown_rows", "run_meta", "run_export_state")

#: Frames materialised per read window by :meth:`DuckStore.iter_frames`.
_ITER_WINDOW = 200

#: Position rows buffered before a batched INSERT is issued.
_WRITE_BATCH_ROWS = 250_000

#: Refuse (rather than OOM) a rehydrate whose source hands back an implausible run.
#: A real worst-case run is 2 520 x 500 = 1.26 M rows ~ 42 MB of columns.
REHYDRATE_MAX_POSITION_ROWS = 20_000_000


def _num(v: Any) -> Optional[float]:
    """Coercion copied from the retiring kpi_sink store."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _naive_utc(v: Any) -> Any:
    """Timestamps the way this database stores them: naive UTC.

    Mongo hands back tz-aware datetimes when the client is tz-aware; the TIMESTAMP columns
    here are naive UTC, and an offset-carrying value would land at a different instant (and
    so under a different primary key).  It is also mandatory *before* array construction:
    ``np.array([tz_aware], dtype="datetime64[us]")`` emits ``DeprecationWarning: parsing
    timezone aware datetimes is deprecated; this will raise an error in the future``.
    Anything that is not a datetime passes through.
    """
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _str_col(values: Sequence[Any]) -> np.ndarray:
    return np.array(list(values), dtype=object)


def _ts_col(values: Sequence[Any]) -> np.ndarray:
    """Timestamp column. ``_naive_utc`` runs on every value BEFORE array construction."""
    return np.array([_naive_utc(v) for v in values], dtype="datetime64[us]")


def _bool_col(values: Sequence[Any]) -> np.ndarray:
    return np.array([bool(v) for v in values], dtype=bool)


def _num_col(values: Sequence[Any]) -> np.ndarray:
    """Numeric column. ``None`` -> NaN -> SQL NULL; ``3.7`` -> BIGINT 4 (verified to be
    identical to what the replaced ``executemany`` binding produced)."""
    out = np.empty(len(values), dtype=np.float64)
    for i, v in enumerate(values):
        n = _num(v)
        out[i] = np.nan if n is None else n
    return out


def _seq_col(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.int64)


def _unmask(arr: Any, dtype: Any) -> np.ndarray:
    """``fetchnumpy()`` can hand back masked arrays; pin them to a plain ndarray."""
    if isinstance(arr, np.ma.MaskedArray):
        arr = arr.filled(0)
    return np.ascontiguousarray(arr, dtype=dtype)


def default_db_path() -> str:
    """``DATAPLANE_DUCKDB_PATH`` if set, else ``<kpi data_dir>/dataplane.duckdb``."""
    env = os.environ.get("DATAPLANE_DUCKDB_PATH")
    if env:
        return env
    return os.path.join(kpi_sink_settings["data_dir"], "dataplane.duckdb")


def _empty_frame_columns() -> Dict[str, np.ndarray]:
    return {
        "frame_idx": np.empty(0, np.uint32),
        "sim_time_ms": np.empty(0, np.float64),
        "slot": np.empty(0, np.uint32),
        "lng": np.empty(0, np.float64),
        "lat": np.empty(0, np.float64),
        "state": np.empty(0, np.uint8),
        "haulier": np.empty(0, np.uint8),
    }


class DuckStore:
    """One DuckDB database for every run; one connection, per-thread cursors, per-run locks.

    See the module docstring for the full locking contract.  In one line: ``_conn_lock``
    is a microsecond lock around connection/cursor/lock-map bookkeeping, mutations
    serialise per ``run_id``, and reads serialise on nothing at all.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._conn_lock = threading.RLock()
        self._local = threading.local()
        self._run_locks: Dict[str, threading.RLock] = {}
        self._closed = False
        with self._conn_lock:
            path = Path(db_path) if db_path is not None else Path(default_db_path())
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)
            try:
                self._conn = duckdb.connect(self.db_path)
                # Schema DDL runs exactly once, here, under the lock.
                self._conn.execute(_SCHEMA)
            except duckdb.Error as exc:  # pragma: no cover - environment dependent
                raise DuckStoreError(f"cannot open DuckDB at {self.db_path}: {exc}") from exc

    # ── internals ────────────────────────────────────────────────────────────

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        """This thread's cursor. Takes ``_conn_lock`` ITSELF — never call it holding one.

        The ONE place in this module that reads ``self._conn``.
        """
        with self._conn_lock:
            if self._closed:
                raise DuckStoreError("DuckStore is closed")
            cur = getattr(self._local, "cur", None)
            if cur is None:
                cur = self._conn.cursor()
                self._local.cur = cur
            return cur

    def _run_lock(self, run_id: str) -> threading.RLock:
        """This run's mutation lock. Returns it *after* releasing ``_conn_lock``.

        Locks are never removed — not even by ``evict_run``.  Removing one while another
        thread holds it would hand the next caller a *different* lock object for the same
        run, silently losing mutual exclusion; the price of keeping it is ~200 bytes per
        distinct run_id the process has ever written to.
        """
        with self._conn_lock:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = threading.RLock()
                self._run_locks[run_id] = lock
            return lock

    @staticmethod
    def _rows_to_dicts(cursor: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _insert_columnar(
        cur: duckdb.DuckDBPyConnection,
        view: str,
        sql: str,
        params: List[Any],
        columns: Dict[str, np.ndarray],
        what: str,
    ) -> None:
        """register -> INSERT … SELECT -> unregister, always unregistering.

        Registered views live in the *cursor's* namespace (verified: a second cursor
        cannot see them), so the fixed view names are safe across threads.

        The columns are wrapped in a ``DataFrame`` (``copy=False`` — no data is touched,
        measured identical insert time for 1.26 M frame rows) because a **bare dict of
        object ndarrays is not registerable**: for >=2 000 rows duckdb 1.5.3 runs its
        pandas analyzer over object columns and, when its 1 000-value sample is all NULL,
        calls the pandas-only ``first_valid_index()`` on the raw ndarray ->
        ``AttributeError: 'numpy.ndarray' object has no attribute 'first_valid_index'``.
        That is the real haulier-scope shape (``haulier_id`` is NULL on every haulier row),
        and because the writer never drops, the failure wedged every topic forever.
        """
        try:
            cur.register(view, pd.DataFrame(columns, copy=False))
            cur.execute(sql, params)
        except duckdb.Error as exc:
            raise DuckStoreError(f"{what} failed: {exc}") from exc
        finally:
            try:
                cur.unregister(view)
            except Exception:  # pragma: no cover - unregister is best effort
                pass

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the connection. Idempotent.

        A statement already executing on another thread's cursor is not waited for — it
        raises a duckdb error (verified: ``InvalidInputException``, which every method here
        wraps into :class:`DuckStoreError`), it does not crash.  That is deliberate: the
        old global lock made ``close()`` wait behind whatever was running, which is how a
        253 s rehydrate came to block shutdown.  ``DataplaneService.stop()`` drains the
        writer *before* closing, so an in-flight write at close time is a bug elsewhere.
        """
        with self._conn_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - close is best effort
                pass

    def checkpoint(self) -> None:
        """Fold the WAL into the database file (what the old per-run files never did)."""
        try:
            self._cursor().execute("CHECKPOINT")
        except duckdb.Error as exc:
            raise DuckStoreError(f"checkpoint failed: {exc}") from exc

    # ── writes ───────────────────────────────────────────────────────────────

    def write_kpi_events(self, rows: Sequence[RowTuple]) -> int:
        """Upsert ``(run_id, metric, value, sim_clock)`` tuples. Idempotent.

        Rows are grouped by ``run_id`` and each group's lock is taken **one at a time**,
        so two run locks are never held simultaneously.
        """
        grouped: Dict[str, List[Tuple[Any, Any, Any]]] = {}
        for run_id, metric, value, sim_clock in rows:
            grouped.setdefault(str(run_id), []).append((metric, value, sim_clock))
        if not grouped:
            return 0
        now = datetime.utcnow()
        written = 0
        for run_id, items in grouped.items():
            columns = self._kpi_columns(
                [m for m, _, _ in items],
                [v for _, v, _ in items],
                [c for _, _, c in items],
                [now] * len(items),
            )
            with self._run_lock(run_id):
                self._insert_kpi_columnar(self._cursor(), run_id, columns)
            written += len(items)
        return written

    @staticmethod
    def _kpi_columns(
        metrics: Sequence[Any],
        values: Sequence[Any],
        clocks: Sequence[Any],
        ingested: Sequence[Any],
    ) -> Dict[str, np.ndarray]:
        return {
            "metric": _str_col([str(m) for m in metrics]),
            "value": np.array([float(v) for v in values], dtype=np.float64),
            "sim_clock": _ts_col(clocks),
            "ingested_at": _ts_col(ingested),
            "_seq": _seq_col(len(metrics)),
        }

    def _insert_kpi_columnar(
        self, cur: duckdb.DuckDBPyConnection, run_id: str, columns: Dict[str, np.ndarray]
    ) -> int:
        n = int(columns["_seq"].size)
        if n == 0:
            return 0
        self._insert_columnar(
            cur, "dp_kpi", _KPI_INSERT, [run_id], columns, "write_kpi_events"
        )
        return n

    def write_breakdown(
        self,
        run_id: str,
        scope: str,
        sim_clock: datetime,
        final: bool,
        entities: List[Dict[str, Any]],
    ) -> int:
        """Upsert every entity row of one breakdown snapshot. Returns rows written.

        0 for a scope outside ``_STORED_SCOPES``, 0 for no entities, and entities without
        an ``id`` are skipped.
        """
        return self.write_breakdown_batch(run_id, [(scope, sim_clock, final, entities)])

    def write_breakdown_batch(self, run_id: str, snapshots: Sequence[Snapshot]) -> int:
        """Upsert many breakdown snapshots of ONE run in a single columnar INSERT.

        This is what the writer task calls: a run publishes ~196 truck-scope snapshots of
        ~500 entities, and coalescing them costs one statement instead of 98 000.
        """
        records: List[Dict[str, Any]] = []
        now = datetime.utcnow()
        for scope, sim_clock, final, entities in snapshots:
            if scope not in _STORED_SCOPES or not entities:
                continue
            clock = _naive_utc(sim_clock)
            is_final = bool(final)
            for entity in entities:
                eid = entity.get("id")
                if eid is None:
                    continue
                record = {
                    "scope": str(scope),
                    "sim_clock": clock,
                    "final": is_final,
                    "entity_id": str(eid),
                    "haulier_id": (
                        str(entity["haulier_id"])
                        if entity.get("haulier_id") is not None
                        else None
                    ),
                    "haulier_name": (
                        str(entity["haulier_name"])
                        if entity.get("haulier_name") is not None
                        else None
                    ),
                    "payload": json.dumps(entity),
                    "ingested_at": now,
                }
                for col in _BD_NUM:
                    record[col] = entity.get(col)
                records.append(record)
        if not records:
            return 0
        columns = self._breakdown_columns(records)
        with self._run_lock(run_id):
            self._insert_breakdown_columnar(self._cursor(), run_id, columns)
        return len(records)

    @classmethod
    def _breakdown_columns(cls, records: Sequence[Dict[str, Any]]) -> Dict[str, np.ndarray]:
        """Column dict for ``kpi_breakdown_rows`` from already-normalised records."""
        columns: Dict[str, np.ndarray] = {}
        for col, kind in _BD_COLUMN_SPEC:
            columns[col] = cls._coerce_column(kind, [r.get(col) for r in records])
        columns["_seq"] = _seq_col(len(records))
        return columns

    def _insert_breakdown_columnar(
        self, cur: duckdb.DuckDBPyConnection, run_id: str, columns: Dict[str, np.ndarray]
    ) -> int:
        n = int(columns["_seq"].size)
        if n == 0:
            return 0
        self._insert_columnar(
            cur, "dp_bd", _BREAKDOWN_INSERT, [run_id], columns, "write_breakdown"
        )
        return n

    def write_frame(self, run_id: str, frame: Frame) -> int:
        """Append one frame's position rows. Returns rows written (``frame.n``)."""
        return self.write_frames(run_id, (frame,))

    def write_frames(self, run_id: str, frames: Iterable[Frame]) -> int:
        """Append many frames in batched inserts. Returns total rows written."""
        with self._run_lock(run_id):
            written = 0
            buf: List[Frame] = []
            buffered_rows = 0
            for frame in frames:
                if frame.n == 0:
                    continue
                buf.append(frame)
                buffered_rows += frame.n
                if buffered_rows >= _WRITE_BATCH_ROWS:
                    written += self._insert_frame_batch(run_id, buf)
                    buf, buffered_rows = [], 0
            if buf:
                written += self._insert_frame_batch(run_id, buf)
            return written

    def _insert_frame_batch(self, run_id: str, frames: Sequence[Frame]) -> int:
        """One columnar INSERT for a list of Frames. Caller holds the run lock."""
        columns, _n_frames, total = self._frame_columns_from_frames(frames)
        if total == 0:
            return 0
        self._insert_frames_columnar(self._cursor(), run_id, columns)
        return total

    def _insert_frames_columnar(
        self, cur: duckdb.DuckDBPyConnection, run_id: str, columns: Dict[str, np.ndarray]
    ) -> int:
        n = int(columns["lng"].size)
        if n == 0:
            return 0
        self._insert_columnar(
            cur, "dp_frames", _FRAME_INSERT, [run_id], columns, "frame insert"
        )
        return n

    @staticmethod
    def _frame_columns_from_frames(
        frames: Iterable[Frame], *, max_rows: Optional[int] = None
    ) -> Tuple[Dict[str, np.ndarray], int, int]:
        """(columns, non-empty frame count, position rows) from ``Frame`` objects."""
        lng: List[np.ndarray] = []
        lat: List[np.ndarray] = []
        slot: List[np.ndarray] = []
        state: List[np.ndarray] = []
        haul: List[np.ndarray] = []
        fidx: List[np.ndarray] = []
        simt: List[np.ndarray] = []
        total = 0
        count = 0
        for frame in frames:
            n = int(frame.n)
            if n == 0:
                continue
            total += n
            count += 1
            if max_rows is not None and total > max_rows:
                raise DuckStoreError(
                    f"refusing to materialise {total} position rows (> {max_rows})"
                )
            lng.append(np.asarray(frame.lng, dtype=np.float64))
            lat.append(np.asarray(frame.lat, dtype=np.float64))
            slot.append(np.asarray(frame.slot, dtype=np.uint32))
            state.append(np.asarray(frame.state, dtype=np.uint8))
            haul.append(np.asarray(frame.haulier, dtype=np.uint8))
            fidx.append(np.full(n, int(frame.frame_idx), dtype=np.uint32))
            simt.append(np.full(n, float(frame.sim_time_ms), dtype=np.float64))
        if total == 0:
            return _empty_frame_columns(), 0, 0
        columns = {
            "frame_idx": np.concatenate(fidx),
            "sim_time_ms": np.concatenate(simt),
            "slot": np.concatenate(slot),
            "lng": np.concatenate(lng),
            "lat": np.concatenate(lat),
            "state": np.concatenate(state),
            "haulier": np.concatenate(haul),
        }
        return columns, count, total

    @staticmethod
    def _frame_columns_from_docs(
        docs: Iterable[Dict[str, Any]], *, max_rows: Optional[int] = None
    ) -> Tuple[Dict[str, np.ndarray], int, int]:
        """(columns, frame count, rows) straight from raw archive documents.

        ``bson.Binary`` is a ``bytes`` subclass, so ``np.frombuffer`` is zero-copy; the
        per-column ``concatenate`` at the end gives owned, writable memory.  ``Frame``
        objects are never built.
        """
        lng: List[np.ndarray] = []
        lat: List[np.ndarray] = []
        slot: List[np.ndarray] = []
        state: List[np.ndarray] = []
        haul: List[np.ndarray] = []
        fidx: List[np.ndarray] = []
        simt: List[np.ndarray] = []
        total = 0
        count = 0
        for doc in docs:
            blob = doc["lng"]
            n = int(doc["n"]) if doc.get("n") is not None else len(blob) // 8
            if n == 0:
                continue
            total += n
            count += 1
            if max_rows is not None and total > max_rows:
                raise DuckStoreError(
                    f"refusing to materialise {total} position rows (> {max_rows})"
                )
            lng.append(np.frombuffer(blob, dtype="<f8"))
            lat.append(np.frombuffer(doc["lat"], dtype="<f8"))
            slot.append(np.frombuffer(doc["slot"], dtype="<u4"))
            state.append(np.frombuffer(doc["state"], dtype=np.uint8))
            haul.append(np.frombuffer(doc["haulier"], dtype=np.uint8))
            fidx.append(np.full(n, int(doc["frame_idx"]), dtype=np.uint32))
            simt.append(np.full(n, float(doc["sim_time_ms"]), dtype=np.float64))
        if total == 0:
            return _empty_frame_columns(), 0, 0
        columns = {
            "frame_idx": np.concatenate(fidx),
            "sim_time_ms": np.concatenate(simt),
            "slot": np.concatenate(slot),
            "lng": np.concatenate(lng),
            "lat": np.concatenate(lat),
            "state": np.concatenate(state),
            "haulier": np.concatenate(haul),
        }
        return columns, count, total

    def upsert_run_meta(self, run_id: str, **fields: Any) -> None:
        """Insert-or-update ``run_meta``; keys must be ``run_meta`` column names."""
        with self._run_lock(run_id):
            self._upsert_run_meta_on(self._cursor(), run_id, **fields)

    @staticmethod
    def _upsert_run_meta_on(
        cur: duckdb.DuckDBPyConnection, run_id: str, **fields: Any
    ) -> None:
        unknown = [k for k in fields if k not in _RUN_META_COLUMNS]
        if unknown:
            raise DuckStoreError(f"unknown run_meta column(s): {sorted(unknown)}")
        cols = [k for k in _RUN_META_COLUMNS if k in fields]
        values = [fields[k] for k in cols]
        placeholders = ", ".join(["?"] * (1 + len(cols)))
        col_sql = ", ".join(["run_id"] + cols)
        if cols:
            update_sql = ", ".join(f"{c} = excluded.{c}" for c in cols)
        else:
            update_sql = "run_id = excluded.run_id"
        sql = (
            f"INSERT INTO run_meta ({col_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT (run_id) DO UPDATE SET {update_sql}"
        )
        try:
            cur.execute(sql, [run_id] + values)
        except duckdb.Error as exc:
            raise DuckStoreError(f"upsert_run_meta failed: {exc}") from exc

    def set_export_state(
        self,
        run_id: str,
        *,
        status: str,
        row_count: Optional[int] = None,
        frame_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._run_lock(run_id):
            self._set_export_state_on(
                self._cursor(),
                run_id,
                status=status,
                row_count=row_count,
                frame_count=frame_count,
                error=error,
            )

    @staticmethod
    def _set_export_state_on(
        cur: duckdb.DuckDBPyConnection,
        run_id: str,
        *,
        status: str,
        row_count: Optional[int] = None,
        frame_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        try:
            cur.execute(_EXPORT_STATE_UPSERT, [run_id, status, row_count, frame_count, error])
        except duckdb.Error as exc:
            raise DuckStoreError(f"set_export_state failed: {exc}") from exc

    # ── reads (no run lock: DuckDB gives readers a consistent snapshot) ───────

    def query(self, sql: str, params: Optional[list] = None) -> List[Dict[str, Any]]:
        try:
            cur = self._cursor().execute(sql, params or [])
        except duckdb.Error as exc:
            raise DuckStoreError(f"query failed: {exc}") from exc
        if cur.description is None:
            return []
        return self._rows_to_dicts(cur)

    def get_run_meta(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self.query("SELECT * FROM run_meta WHERE run_id = ?", [run_id])
        return rows[0] if rows else None

    def get_export_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT run_id, status, row_count, frame_count, exported_at, error "
            "FROM run_export_state WHERE run_id = ?",
            [run_id],
        )
        return rows[0] if rows else None

    def list_run_ids(self) -> List[str]:
        """Every run_id known to the store, from any table."""
        rows = self.query(
            """
            SELECT DISTINCT run_id FROM (
                SELECT run_id FROM frames
                UNION SELECT run_id FROM kpi_events
                UNION SELECT run_id FROM kpi_breakdown_rows
                UNION SELECT run_id FROM run_meta
            ) ORDER BY run_id
            """
        )
        return [r["run_id"] for r in rows]

    def open_run_ids(self) -> List[str]:
        """Runs that have data but no terminal status — in progress, or nobody closed them.

        A headless run never reaches ``run_meta`` at all until it is finalized, so "no row"
        counts as open exactly like "a row whose status is NULL"; both are runs the archive
        will never be told about.
        """
        rows = self.query(
            """
            SELECT DISTINCT run_id FROM (
                SELECT run_id FROM frames
                UNION SELECT run_id FROM kpi_events
                UNION SELECT run_id FROM kpi_breakdown_rows
                UNION SELECT run_id FROM run_meta
            ) WHERE run_id NOT IN (SELECT run_id FROM run_meta WHERE status IS NOT NULL)
            ORDER BY run_id
            """
        )
        return [r["run_id"] for r in rows]

    def frame_range(self, run_id: str) -> Optional[Tuple[int, int]]:
        rows = self.query(
            "SELECT min(frame_idx) AS lo, max(frame_idx) AS hi FROM frames WHERE run_id = ?",
            [run_id],
        )
        if not rows or rows[0]["lo"] is None:
            return None
        return int(rows[0]["lo"]), int(rows[0]["hi"])

    def run_frame_count(self, run_id: str) -> int:
        rows = self.query(
            "SELECT count(DISTINCT frame_idx) AS c FROM frames WHERE run_id = ?", [run_id]
        )
        return int(rows[0]["c"]) if rows else 0

    def kpi_row_count(self, run_id: str) -> int:
        rows = self.query("SELECT count(*) AS c FROM kpi_events WHERE run_id = ?", [run_id])
        return int(rows[0]["c"]) if rows else 0

    def breakdown_row_count(self, run_id: str) -> int:
        rows = self.query(
            "SELECT count(*) AS c FROM kpi_breakdown_rows WHERE run_id = ?", [run_id]
        )
        return int(rows[0]["c"]) if rows else 0

    def read_kpi_events(self, run_id: str) -> List[RowTuple]:
        rows = self.query(
            "SELECT run_id, metric, value, sim_clock FROM kpi_events "
            "WHERE run_id = ? ORDER BY sim_clock, metric",
            [run_id],
        )
        return [(r["run_id"], r["metric"], float(r["value"]), r["sim_clock"]) for r in rows]

    def read_frames(self, run_id: str, frame_from: int, frame_to: int) -> Dict[str, np.ndarray]:
        """Raw ``fetchnumpy()`` columns for an **inclusive** frame_idx range.

        Deliberately not reshaped: this is the hot tier's own column layout, which is
        also the wire layout and the deck.gl binary-attribute layout.
        """
        try:
            cur = self._cursor().execute(
                "SELECT frame_idx, sim_time_ms, slot, lng, lat, state, haulier "
                "FROM frames WHERE run_id = ? AND frame_idx >= ? AND frame_idx <= ? "
                "ORDER BY frame_idx, slot",
                [run_id, int(frame_from), int(frame_to)],
            )
            return cur.fetchnumpy()
        except duckdb.Error as exc:
            raise DuckStoreError(f"read_frames failed: {exc}") from exc

    def read_frame(self, run_id: str, frame_idx: int) -> Optional[Frame]:
        cols = self.read_frames(run_id, frame_idx, frame_idx)
        return self._frame_from_columns(int(frame_idx), cols, slice(None))

    @staticmethod
    def _frame_from_columns(
        frame_idx: int, cols: Dict[str, np.ndarray], sel: Any
    ) -> Optional[Frame]:
        lng = _unmask(cols["lng"], np.float64)[sel]
        if lng.size == 0:
            return None
        sim_time = float(_unmask(cols["sim_time_ms"], np.float64)[sel][0])
        return Frame(
            frame_idx=frame_idx,
            sim_time_ms=sim_time,
            lng=lng,
            lat=_unmask(cols["lat"], np.float64)[sel],
            slot=_unmask(cols["slot"], np.uint32)[sel],
            state=_unmask(cols["state"], np.uint8)[sel],
            haulier=_unmask(cols["haulier"], np.uint8)[sel],
            kind=KIND_KEYFRAME,
        )

    def iter_frames(self, run_id: str) -> Iterator[Frame]:
        """Yield every frame of a run in ascending ``frame_idx``.

        Only a bounded window is materialised at a time, and nothing is held while the
        consumer processes a yielded Frame (that would let a slow Mongo dump keep a
        window's worth of memory alive for the length of a run).
        """
        rng = self.frame_range(run_id)
        if rng is None:
            return
        lo, hi = rng
        cursor = lo
        while cursor <= hi:
            window_hi = min(cursor + _ITER_WINDOW - 1, hi)
            cols = self.read_frames(run_id, cursor, window_hi)
            cursor = window_hi + 1
            idx = _unmask(cols["frame_idx"], np.uint32)
            if idx.size == 0:
                continue
            uniq, starts = np.unique(idx, return_index=True)
            order = np.argsort(starts)
            uniq, starts = uniq[order], starts[order]
            bounds = list(starts) + [idx.size]
            for i, fidx in enumerate(uniq):
                frame = self._frame_from_columns(
                    int(fidx), cols, slice(int(bounds[i]), int(bounds[i + 1]))
                )
                if frame is not None:
                    yield frame

    # ── lifecycle over whole runs ────────────────────────────────────────────

    @staticmethod
    def _records_from_breakdown_rows(
        run_id: str, rows: Iterable[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Archive-shaped ``kpi_breakdown_rows`` dicts -> normalised records.

        An archived document also carries ``_id`` / ``doc_type`` / ``_created``; anything
        that is not a real column is dropped, and a row without ``entity_id`` / ``scope`` /
        ``sim_clock`` (nothing addressable to key on) is skipped, exactly as before.
        """
        now = datetime.utcnow()
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            entity_id = row.get("entity_id")
            scope = row.get("scope")
            sim_clock = row.get("sim_clock")
            if entity_id is None or scope is None or sim_clock is None:
                continue
            record = {
                "scope": str(scope),
                "entity_id": str(entity_id),
                "sim_clock": _naive_utc(sim_clock),
                "final": bool(row.get("final")),
                "ingested_at": _naive_utc(row.get("ingested_at")) or now,
            }
            for col in _BD_COLUMNS:
                if col not in record:
                    record[col] = row.get(col)
            out.append(record)
        return out

    @staticmethod
    def _coerce_column(kind: str, values: Any) -> np.ndarray:
        """One column, in the dtype the INSERT needs. Already-correct arrays pass through."""
        if kind == "str":
            if isinstance(values, np.ndarray) and values.dtype == object:
                return values
            return _str_col([None if v is None else str(v) for v in values])
        if kind == "ts":
            if isinstance(values, np.ndarray) and values.dtype == _TS_DTYPE:
                return values
            return _ts_col(values)
        if kind == "bool":
            if isinstance(values, np.ndarray) and values.dtype == np.bool_:
                return values
            return _bool_col(values)
        if isinstance(values, np.ndarray) and values.dtype == np.float64:
            return values
        return _num_col(values)

    @classmethod
    def _adopt_columns(
        cls,
        mapping: Any,
        spec: Sequence[Tuple[str, str]],
        *,
        required: Sequence[str],
        defaults: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, np.ndarray]]:
        """Normalise a source-supplied column mapping, or ``None`` if it is unusable.

        This is the validation for the optional columnar reader seams.  A source that
        answers with something this module cannot interpret is treated exactly like a
        source that cannot answer at all — never like a source that answered "empty".
        """
        if not isinstance(mapping, dict):
            return None
        if any(key not in mapping for key in required):
            return None
        try:
            lengths = {len(mapping[key]) for key in required}
        except TypeError:
            return None
        if len(lengths) != 1:
            return None
        n = lengths.pop()
        out: Dict[str, np.ndarray] = {}
        for col, kind in spec:
            values = mapping.get(col)
            if values is None:
                fill = (defaults or {}).get(col)
                values = [fill] * n
            elif len(values) != n:
                return None
            out[col] = cls._coerce_column(kind, values)
        out["_seq"] = _seq_col(n)
        return out

    def _read_frame_columns(
        self, source: Any, run_id: str
    ) -> Tuple[Dict[str, np.ndarray], int]:
        """PHASE 1 frame read. Issues no SQL; opens no transaction."""
        docs_reader = getattr(source, "read_run_frame_docs", None)
        if callable(docs_reader):
            columns, count, _rows = self._frame_columns_from_docs(
                docs_reader(run_id), max_rows=REHYDRATE_MAX_POSITION_ROWS
            )
            return columns, count
        columns, count, _rows = self._frame_columns_from_frames(
            source.read_run_frames(run_id), max_rows=REHYDRATE_MAX_POSITION_ROWS
        )
        return columns, count

    def _read_kpi_columns(self, source: Any, run_id: str) -> Dict[str, np.ndarray]:
        """PHASE 1 kpi read. Issues no SQL; opens no transaction."""
        now = datetime.utcnow()
        reader = getattr(source, "read_run_kpi_columns", None)
        if callable(reader):
            adopted = self._adopt_columns(
                reader(run_id),
                _KPI_COLUMN_SPEC,
                required=("metric", "value", "sim_clock"),
                defaults={"ingested_at": now},
            )
            if adopted is not None:
                return adopted
            logger.warning(
                "rehydrate_run run_id=%s: read_run_kpi_columns returned an uninterpretable "
                "mapping; falling back to read_run_kpi_events",
                run_id,
            )
        rows = list(source.read_run_kpi_events(run_id))
        return self._kpi_columns(
            [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [now] * len(rows)
        )

    def _read_breakdown_columns(
        self, source: Any, run_id: str
    ) -> Optional[Dict[str, np.ndarray]]:
        """PHASE 1 breakdown read. ``None`` means "this source cannot supply these rows".

        ``None`` is the only value that leaves ``kpi_breakdown_rows`` alone; a source that
        genuinely holds no rows answers with an EMPTY column dict, which correctly clears
        the table.  A reader that raises propagates out of phase 1 — before any DELETE.

        "Leave them alone" only preserves anything when there is something to preserve.
        Against an EMPTY working set — the ordinary case, since DuckDB evicts freely — it
        silently means "restore zero rows", and the rehydrate would still go on to stamp
        ``run_meta.status='completed'`` / ``run_export_state='exported'``, so one transient
        Mongo blip left a permanently half-restored run that PHASE 0 then refused to
        re-attempt.  With nothing to preserve, an unanswerable reader is a failed read.
        """
        columnar = getattr(source, "read_run_breakdown_columns", None)
        if callable(columnar):
            answer = columnar(run_id)
            if answer is None and not self.breakdown_row_count(run_id):
                raise DuckStoreError(
                    f"rehydrate_run {run_id!r}: {type(source).__name__} could not read "
                    "breakdown rows and the run holds none to keep; aborting before any "
                    "DELETE so the rehydrate can be retried"
                )
            if answer is None:
                logger.warning(
                    "rehydrate_run run_id=%s: %s cannot supply breakdown rows; "
                    "leaving existing kpi_breakdown_rows untouched",
                    run_id,
                    type(source).__name__,
                )
                return None
            adopted = self._adopt_columns(
                answer,
                _BD_COLUMN_SPEC,
                required=_BD_KEY,
                defaults={"ingested_at": datetime.utcnow()},
            )
            if adopted is not None:
                return adopted
            logger.warning(
                "rehydrate_run run_id=%s: read_run_breakdown_columns returned an "
                "uninterpretable mapping; falling back to read_run_breakdown_rows",
                run_id,
            )
        rows_reader = getattr(source, "read_run_breakdown_rows", None)
        if callable(rows_reader):
            return self._breakdown_columns(
                self._records_from_breakdown_rows(run_id, rows_reader(run_id))
            )
        logger.info(
            "rehydrate_run run_id=%s: source %s has no breakdown reader; "
            "leaving existing kpi_breakdown_rows untouched",
            run_id,
            type(source).__name__,
        )
        return None

    @staticmethod
    def _run_summary(source: Any, run_id: str) -> Dict[str, Any]:
        """The source's run summary, or ``{}``. A probe must never abort a rehydrate."""
        getter = getattr(source, "run_summary", None)
        if not callable(getter):
            return {}
        try:
            summary = getter(run_id)
        except Exception:  # noqa: BLE001 - a probe must never abort a rehydrate
            logger.warning("run_summary probe failed run_id=%s", run_id, exc_info=True)
            return {}
        return summary if isinstance(summary, dict) else {}

    @staticmethod
    def _summary_meta(summary: Dict[str, Any]) -> Dict[str, Any]:
        """``run_meta`` fields recovered from a run summary, coerced PER COLUMN TYPE.

        A blanket ``str(value)`` would write a stringified datetime into the ``first_seen``
        TIMESTAMP column; ``n_trucks`` is INTEGER; the code books are JSON text.
        """
        out: Dict[str, Any] = {}
        for key in _SUMMARY_META_KEYS:
            value = summary.get(key)
            if value is None:
                continue
            if key == "n_trucks":
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    continue
            elif key == "first_seen":
                if isinstance(value, datetime):
                    out[key] = _naive_utc(value)
            elif key == "status":
                out[key] = str(value)
            elif isinstance(value, (dict, list)):
                out[key] = json.dumps(value)
            else:
                out[key] = str(value)
        return out

    def rehydrate_run(self, run_id: str, source: FrameSource, *, force: bool = False) -> int:
        """Pull a run back from the durable archive. Returns frames written.

        Three phases, inside this run's lock and nothing else's:

        **PHASE 0 — guards.** No transaction is open.  No-op (returns 0) when the run
        already holds frames **or** kpi rows **or** breakdown rows, unless ``force=True``.
        Frames alone is not the test: headless runs (the OpenRide default) publish no
        ``truck_loc`` at all, so a kpi-only run would otherwise look absent and be cleared.

        **PHASE 1 — read the ENTIRE source into memory.**  Issues no mutating SQL and opens
        no transaction, so a source that dies half-way (a Mongo cursor timeout is the
        realistic case) leaves DuckDB byte-identical — not even a ``BEGIN`` having happened.
        The first DELETE is unreachable until every byte the source will supply is already
        in local numpy arrays.

        **PHASE 2 — one short transaction of purely local work** (~0.6 s for a full run):
        DELETE, columnar INSERT x3, ``run_meta``, ``run_export_state``, COMMIT.  Any
        exception rolls the whole thing back, so **atomicity, not ``force=True``, is the
        repair path**.

        The run lock spans phases 1 and 2, so no writer can append to this run between the
        read and the DELETE.  Readers and other runs' writers are never blocked.

        ``source`` is any :class:`FrameSource` — structural typing, so this module never
        imports ``apps.dataplane.archive.mongo``.  ``kpi_breakdown_rows`` is deleted **only**
        when the source supplied breakdown columns: a source cannot be allowed to destroy
        rows it is unable to replace.
        """
        with self._run_lock(run_id):
            # ── PHASE 0: cheap guards. No transaction is open. ──
            if not force and (
                self.run_frame_count(run_id)
                or self.kpi_row_count(run_id)
                or self.breakdown_row_count(run_id)
            ):
                return 0
            if not source.has_run(run_id):
                raise RunNotInStore(f"run {run_id!r} is not available in the frame source")

            # ── PHASE 1: read everything. No DuckDB mutation whatsoever. ──
            frame_cols, n_frames = self._read_frame_columns(source, run_id)
            kpi_cols = self._read_kpi_columns(source, run_id)
            bd_cols = self._read_breakdown_columns(source, run_id)
            summary = self._run_summary(source, run_id)
            meta_fields = self._summary_meta(summary)
            max_frame_idx = (
                int(frame_cols["frame_idx"].max()) if frame_cols["frame_idx"].size else None
            )

            # ── PHASE 2: ONE short transaction, entirely local work. ──
            cur = self._cursor()
            try:
                cur.execute("BEGIN TRANSACTION")
            except duckdb.Error as exc:
                raise DuckStoreError(f"rehydrate_run could not begin: {exc}") from exc
            try:
                cur.execute("DELETE FROM frames WHERE run_id = ?", [run_id])
                cur.execute("DELETE FROM kpi_events WHERE run_id = ?", [run_id])
                if bd_cols is not None:
                    cur.execute("DELETE FROM kpi_breakdown_rows WHERE run_id = ?", [run_id])

                self._insert_frames_columnar(cur, run_id, frame_cols)
                self._insert_kpi_columnar(cur, run_id, kpi_cols)
                if bd_cols is not None:
                    self._insert_breakdown_columnar(cur, run_id, bd_cols)

                meta_fields.update(
                    last_seen=datetime.utcnow(),
                    max_frame_idx=max_frame_idx,
                    source="rehydrated",
                )
                self._upsert_run_meta_on(cur, run_id, **meta_fields)
                if summary.get("complete"):
                    # Without this, run_is_terminal() answers False for a fully archived
                    # run, reconcile() flips its durable complete=True back to False, and
                    # /health counts it as pending for the life of the process.
                    self._set_export_state_on(
                        cur,
                        run_id,
                        status="exported",
                        row_count=_as_int(summary.get("kpi_count")),
                        frame_count=_as_int(summary.get("frame_count")),
                    )
                cur.execute("COMMIT")
            except BaseException:
                # Anything raised here — a DuckDB error or a store-side failure — undoes
                # the whole rehydrate before propagating.
                self._rollback(cur, run_id)
                raise
            return n_frames

    @staticmethod
    def _rollback(cur: duckdb.DuckDBPyConnection, run_id: str) -> None:
        try:
            cur.execute("ROLLBACK")
        except Exception:  # pragma: no cover - rollback is best effort
            logger.warning("rehydrate_run rollback failed run_id=%s", run_id, exc_info=True)

    def evict_run(self, run_id: str) -> int:
        """Transactionally drop a run from DuckDB. Returns rows deleted.

        DuckDB is a working set, not the archive: this touches **no** Mongo document and
        deletes **no** file.  ``run_export_state`` goes too, so a stale "exported" row
        cannot outlive the run and mask a missing ``run_meta.status``.
        """
        with self._run_lock(run_id):
            try:
                cur = self._cursor()
                counts = 0
                for table in _RUN_TABLES:
                    row = cur.execute(
                        f"SELECT count(*) FROM {table} WHERE run_id = ?", [run_id]
                    ).fetchone()
                    counts += int(row[0]) if row else 0
                cur.execute("BEGIN TRANSACTION")
                try:
                    for table in _RUN_TABLES:
                        cur.execute(f"DELETE FROM {table} WHERE run_id = ?", [run_id])
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise
            except duckdb.Error as exc:
                raise DuckStoreError(f"evict_run failed: {exc}") from exc
            return counts


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "DuckStore",
    "DuckStoreError",
    "RunNotInStore",
    "FrameSource",
    "Snapshot",
    "default_db_path",
    "REHYDRATE_MAX_POSITION_ROWS",
]
