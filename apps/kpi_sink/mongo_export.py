"""Bulk export DuckDB KPI rows into MongoDB."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pymongo import MongoClient
from pymongo.errors import BulkWriteError, DuplicateKeyError

from apps.config import kpi_sink_settings
from apps.kpi_sink.duckdb_store import DuckDbKpiStore

logger = logging.getLogger(__name__)


def _mongo_client() -> MongoClient:
    uri = kpi_sink_settings.get("mongo_uri")
    if uri:
        return MongoClient(uri)
    return MongoClient(
        kpi_sink_settings["mongo_host"],
        int(kpi_sink_settings["mongo_port"]),
    )


def _mongo_db(client: MongoClient):
    db_name = kpi_sink_settings["mongo_db"]
    return client[db_name]


def rows_to_mongo_docs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    for row in rows:
        sim_clock = row["sim_clock"]
        docs.append(
            {
                "run_id": row["run_id"],
                "metric": row["metric"],
                "value": float(row["value"]),
                "sim_clock": sim_clock,
                "_created": sim_clock,
                "_updated": sim_clock,
            }
        )
    return docs


class MongoKpiExporter:
    def __init__(self, store: DuckDbKpiStore) -> None:
        self.store = store
        self.export_to_mongo = bool(kpi_sink_settings.get("export_to_mongo_on_complete", True))
        self.force_reexport = bool(kpi_sink_settings.get("force_reexport", False))
        self.batch_size = int(kpi_sink_settings.get("export_batch_size", 1000))

    def export_run(self, run_id: str, *, reason: str = "") -> Dict[str, Any]:
        if not self.export_to_mongo:
            logger.info("Mongo export disabled — skipping run_id=%s", run_id)
            return {"run_id": run_id, "skipped": True, "reason": "export_disabled"}

        self.store.flush(run_id)
        current_count = self.store.row_count(run_id)
        state = self.store.get_export_state(run_id)
        if state and state.get("status") == "exported" and not self.force_reexport:
            last_count = int(state.get("row_count") or 0)
            if current_count <= last_count:
                logger.info(
                    "Run already exported (%d rows) — skipping run_id=%s reason=%s",
                    last_count,
                    run_id,
                    reason or "n/a",
                )
                return {"run_id": run_id, "skipped": True, "reason": "already_exported"}
            logger.info(
                "Re-exporting run_id=%s (%d → %d rows) reason=%s",
                run_id,
                last_count,
                current_count,
                reason or "n/a",
            )

        rows = self.store.fetch_all(run_id)
        if not rows:
            self.store.set_export_state(run_id, status="exported", row_count=0, error=None)
            logger.info("No KPI rows to export for run_id=%s", run_id)
            return {"run_id": run_id, "exported": 0, "reason": reason}

        docs = rows_to_mongo_docs(rows)
        client = _mongo_client()
        collection = _mongo_db(client)["kpi"]

        try:
            inserted = self._insert_batches(collection, docs)
            self.store.set_export_state(
                run_id,
                status="exported",
                row_count=current_count,
                error=None,
            )
            logger.info(
                "Exported KPI data to Mongo for run_id=%s (%d doc(s), %d row(s) in DuckDB) reason=%s",
                run_id,
                inserted,
                current_count,
                reason or "n/a",
            )
            return {"run_id": run_id, "exported": inserted, "duckdb_rows": current_count, "reason": reason}
        except Exception as exc:
            msg = str(exc)
            self.store.set_export_state(run_id, status="failed", row_count=0, error=msg)
            logger.exception("Mongo export failed for run_id=%s", run_id)
            return {"run_id": run_id, "error": msg}
        finally:
            client.close()

    def _insert_batches(self, collection, docs: List[Dict[str, Any]]) -> int:
        inserted = 0
        for start in range(0, len(docs), self.batch_size):
            chunk = docs[start : start + self.batch_size]
            try:
                result = collection.insert_many(chunk, ordered=False)
                inserted += len(result.inserted_ids)
            except BulkWriteError as exc:
                details = exc.details or {}
                inserted += int(details.get("nInserted", 0))
                write_errors = details.get("writeErrors") or []
                non_dup = [e for e in write_errors if e.get("code") != 11000]
                if non_dup:
                    raise
            except DuplicateKeyError:
                for doc in chunk:
                    try:
                        collection.insert_one(doc)
                        inserted += 1
                    except DuplicateKeyError:
                        continue
        return inserted
