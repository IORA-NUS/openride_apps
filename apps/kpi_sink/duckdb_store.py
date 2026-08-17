"""Per-run DuckDB storage for KPI events."""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import duckdb

logger = logging.getLogger(__name__)

RowTuple = Tuple[str, str, float, datetime]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kpi_events (
    run_id VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    value DOUBLE NOT NULL,
    sim_clock TIMESTAMP NOT NULL,
    ingested_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, metric, sim_clock)
);

CREATE TABLE IF NOT EXISTS run_export_state (
    run_id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL,
    row_count BIGINT,
    exported_at TIMESTAMP,
    error VARCHAR
);

-- Phase 2: per-entity breakdown snapshots (truck / haulier). Flat numeric columns power SQL
-- aggregation (percentiles / histograms / GROUP BY haulier); ``payload`` keeps the exact entity
-- dict so the read API can reproduce the frontend shape byte-for-byte. Upsert key makes ingestion
-- idempotent under at-least-once Kafka redelivery.
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
"""

_BREAKDOWN_UPSERT = """
INSERT INTO kpi_breakdown_rows (
    run_id, scope, sim_clock, final, entity_id, haulier_id, haulier_name,
    num_orders_completed, empty_km, loaded_km, total_km, empty_ratio, active_hours,
    orders_per_day, dual_cycle_count, chain_opportunities, dual_cycle_rate, num_trucks,
    payload, ingested_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id, scope, sim_clock, entity_id) DO UPDATE SET
    final = excluded.final,
    haulier_id = excluded.haulier_id,
    haulier_name = excluded.haulier_name,
    num_orders_completed = excluded.num_orders_completed,
    empty_km = excluded.empty_km,
    loaded_km = excluded.loaded_km,
    total_km = excluded.total_km,
    empty_ratio = excluded.empty_ratio,
    active_hours = excluded.active_hours,
    orders_per_day = excluded.orders_per_day,
    dual_cycle_count = excluded.dual_cycle_count,
    chain_opportunities = excluded.chain_opportunities,
    dual_cycle_rate = excluded.dual_cycle_rate,
    num_trucks = excluded.num_trucks,
    payload = excluded.payload,
    ingested_at = excluded.ingested_at
"""

_UPSERT = """
INSERT INTO kpi_events (run_id, metric, value, sim_clock, ingested_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (run_id, metric, sim_clock) DO UPDATE SET
    value = excluded.value,
    ingested_at = excluded.ingested_at
"""


def _safe_run_id(run_id: str) -> str:
    return re.sub(r"[^\w\-.]", "_", run_id)


class DuckDbKpiStore:
    """Buffered writer with one DuckDB file per run_id."""

    def __init__(self, data_dir: str | Path, *, batch_max_rows: int = 500) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.batch_max_rows = max(1, batch_max_rows)
        # RLock, not Lock: _connection() now takes the lock itself, and several callers
        # already hold it (write_breakdown, query). Re-entrant acquisition is what lets the
        # cache be guarded without deadlocking those paths.
        self._lock = threading.RLock()
        self._connections: Dict[str, duckdb.DuckDBPyConnection] = {}
        self._pending: Dict[str, List[RowTuple]] = defaultdict(list)
        self._known_run_ids: set[str] = set()

    def db_path(self, run_id: str) -> Path:
        return self.data_dir / f"{_safe_run_id(run_id)}.duckdb"

    def _connection(self, run_id: str) -> duckdb.DuckDBPyConnection:
        """Get (or open) this run's connection.

        The lock is not optional. Two threads reaching a run's file for the first time both
        ran ``CREATE TABLE`` here, and DuckDB rejected the loser with
        ``TransactionException: Catalog write-write conflict on create``. That exception is
        not a KafkaException, so the run_status consumer's ``except KafkaException`` did not
        catch it, the thread died, and no run was exported to Mongo from 2026-07-01 onward
        while systemd still reported the unit active.
        """
        with self._lock:
            if run_id not in self._connections:
                path = self.db_path(run_id)
                conn = duckdb.connect(str(path))
                conn.execute(_SCHEMA)
                self._connections[run_id] = conn
            return self._connections[run_id]

    def enqueue(self, run_id: str, metric: str, value: float, sim_clock: datetime) -> bool:
        """Buffer a row; flush when batch_max_rows is reached."""
        with self._lock:
            self._known_run_ids.add(run_id)
            self._pending[run_id].append((run_id, metric, value, sim_clock))
            should_flush = len(self._pending[run_id]) >= self.batch_max_rows
        if should_flush:
            self.flush(run_id)
        return should_flush

    def flush(self, run_id: Optional[str] = None) -> int:
        """Write pending rows for one run or all runs. Returns rows written."""
        with self._lock:
            if run_id is not None:
                targets = {run_id: self._pending.pop(run_id, [])}
            else:
                targets = {rid: self._pending.pop(rid, []) for rid in list(self._pending.keys())}

        written = 0
        now = datetime.utcnow()
        for rid, rows in targets.items():
            if not rows:
                continue
            # The write is inside the lock too: a DuckDB connection is not safe to use from
            # two threads at once, and the flush timer, the KPI consumer and the export path
            # all reach this line for the same run.
            with self._lock:
                conn = self._connection(rid)
                conn.executemany(
                    _UPSERT,
                    [(r, m, v, sc, now) for r, m, v, sc in rows],
                )
            written += len(rows)
            logger.debug("Flushed %d KPI row(s) to DuckDB for run_id=%s", len(rows), rid)
        return written

    def fetch_all(self, run_id: str) -> List[Dict[str, Any]]:
        """Read a run's KPI rows.

        The whole execute/description/fetchall sequence must be inside the lock. DuckDB's
        ``conn.execute()`` returns the CONNECTION, not an independent cursor, so
        ``.description`` and ``.fetchall()`` describe whatever statement ran last on it. A
        concurrent ``flush()`` on the timer thread replaced this SELECT's description with
        its INSERT's, and the rows came back without a ``sim_clock`` key —
        ``KeyError: 'sim_clock'`` out of ``rows_to_mongo_docs`` on 2026-08-07, which lost the
        export of a completed run even though the terminal trigger fired correctly.
        """
        self.flush(run_id)
        with self._lock:
            conn = self._connection(run_id)
            cursor = conn.execute(
                """
                SELECT run_id, metric, value, sim_clock
                FROM kpi_events
                WHERE run_id = ?
                ORDER BY sim_clock, metric
                """,
                [run_id],
            )
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # ── Phase 2: per-entity breakdown snapshots ──────────────────────────────

    def write_breakdown(
        self,
        run_id: str,
        scope: str,
        sim_clock: datetime,
        final: bool,
        entities: List[Dict[str, Any]],
    ) -> int:
        """Upsert all entity rows of one breakdown snapshot. Returns rows written."""
        import json as _json

        if scope not in ("truck", "haulier") or not entities:
            return 0

        def _num(v: Any) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        params: List[tuple] = []
        now = datetime.utcnow()
        for e in entities:
            eid = e.get("id")
            if eid is None:
                continue
            params.append((
                run_id, scope, sim_clock, bool(final), str(eid),
                (str(e["haulier_id"]) if e.get("haulier_id") is not None else None),
                (str(e["haulier_name"]) if e.get("haulier_name") is not None else None),
                _num(e.get("num_orders_completed")), _num(e.get("empty_km")),
                _num(e.get("loaded_km")), _num(e.get("total_km")), _num(e.get("empty_ratio")),
                _num(e.get("active_hours")), _num(e.get("orders_per_day")),
                _num(e.get("dual_cycle_count")), _num(e.get("chain_opportunities")),
                _num(e.get("dual_cycle_rate")), _num(e.get("num_trucks")),
                _json.dumps(e), now,
            ))
        if not params:
            return 0
        with self._lock:
            self._known_run_ids.add(run_id)
            conn = self._connection(run_id)
            conn.executemany(_BREAKDOWN_UPSERT, params)
        logger.debug("Wrote %d breakdown row(s) run_id=%s scope=%s", len(params), run_id, scope)
        return len(params)

    def query(self, run_id: str, sql: str, params: Optional[list] = None) -> List[Dict[str, Any]]:
        """Run a read-only SELECT against a run's DuckDB file. Returns [] if the file doesn't
        exist yet (avoids creating an empty db for an unknown run). Locked so it serializes
        with the writer on the shared per-run connection."""
        if not self.db_path(run_id).exists():
            return []
        with self._lock:
            conn = self._connection(run_id)
            cursor = conn.execute(sql, params or [])
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def row_count(self, run_id: str) -> int:
        self.flush(run_id)
        with self._lock:  # see fetch_all: execute+fetch must not straddle another statement
            conn = self._connection(run_id)
            result = conn.execute(
                "SELECT COUNT(*) FROM kpi_events WHERE run_id = ?",
                [run_id],
            ).fetchone()
        return int(result[0]) if result else 0

    def get_export_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        self.flush(run_id)
        with self._lock:  # see fetch_all
            conn = self._connection(run_id)
            cursor = conn.execute(
                "SELECT run_id, status, row_count, exported_at, error FROM run_export_state WHERE run_id = ?",
                [run_id],
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))

    def set_export_state(
        self,
        run_id: str,
        *,
        status: str,
        row_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:  # see fetch_all
            conn = self._connection(run_id)
            conn.execute(
                """
                INSERT INTO run_export_state (run_id, status, row_count, exported_at, error)
                VALUES (?, ?, ?, current_timestamp, ?)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = excluded.status,
                    row_count = excluded.row_count,
                    exported_at = excluded.exported_at,
                    error = excluded.error
                """,
                [run_id, status, row_count, error],
            )

    def close(self) -> None:
        self.flush()
        for conn in self._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()

    def pending_run_ids(self) -> Iterable[str]:
        with self._lock:
            return list(self._pending.keys())

    def list_run_ids(self) -> Iterable[str]:
        """Run IDs seen by this process plus any DuckDB files on disk."""
        with self._lock:
            ids = set(self._known_run_ids)
        if self.data_dir.is_dir():
            for path in self.data_dir.glob("*.duckdb"):
                ids.add(path.stem)
        return ids
