"""Copy the surviving per-run DuckDB files into the Mongo archive.

    python -m apps.dataplane.tools.preserve_surviving_runs [--dry-run]

Only two runs are left on disk (``run_20260805_150813`` and ``run_20260805_162056``
in ``~/.openride/kpi-duckdb/``). Both files are **held open by the live
kpi-duckdb-sink process**, which holds the file lock, and each has a ~16 MB ``.wal``
beside it carrying data that is not yet in the main file. Therefore:

* the tool NEVER opens a file under the data dir directly — it ``shutil.copy2``s both
  ``<run>.duckdb`` AND ``<run>.duckdb.wal`` into a fresh temp directory and opens the
  COPY read-only (verified: without the ``.wal`` copy the run loses ~18% of its kpi
  rows and half its breakdown rows);
* it never writes to, truncates, checkpoints or deletes anything under the data dir;
* it writes only into the new ``dataplane_*`` collections unless ``--also-legacy-kpi``
  is given, so the collection the dashboard reads is untouched by default.

**Both tables are copied.** The two surviving files hold 935 ``kpi_events`` rows each
and 34,689 / 34,438 ``kpi_breakdown_rows`` rows — the per-truck and per-haulier
distribution data. An earlier version read only ``kpi_events`` and reported success,
silently abandoning 97% of what it exists to rescue.

**No run-summary document is written.** This is a partial-shape rescue (these legacy
files have no frames), so closing the run in ``dataplane_runs`` would make the
reconciler treat it as fully archived and never look at it again.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import duckdb

from apps.config import kpi_sink_settings
from apps.dataplane.archive.mongo import MongoArchive
from apps.dataplane.contract.frame import Frame

logger = logging.getLogger(__name__)

DEFAULT_RUN_IDS: Tuple[str, ...] = ("run_20260805_150813", "run_20260805_162056")

#: The collection the dashboard reads today. Only written with --also-legacy-kpi.
LEGACY_KPI_COLLECTION = "kpi"

#: Tables read out of a legacy per-run file.
KPI_TABLE = "kpi_events"
BREAKDOWN_TABLE = "kpi_breakdown_rows"

KpiRow = Tuple[str, str, float, datetime]


@dataclass
class RunTables:
    """Everything read out of one legacy per-run DuckDB file, in one copy-then-open."""

    run_id: str
    kpi_events: List[KpiRow] = field(default_factory=list)
    breakdown_rows: List[Dict[str, Any]] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        return {KPI_TABLE: len(self.kpi_events), BREAKDOWN_TABLE: len(self.breakdown_rows)}


def _safe_run_id(run_id: str) -> str:
    """Same filename mangling the legacy per-run store used."""
    return re.sub(r"[^\w\-.]", "_", run_id)


def run_db_path(data_dir: str | Path, run_id: str) -> Path:
    return Path(data_dir) / f"{_safe_run_id(run_id)}.duckdb"


def copy_db_to_temp(src: str | Path) -> Tuple[Path, Path]:
    """Copy ``<src>`` and its ``.wal`` sidecar into a fresh temp dir.

    Returns ``(temp_dir, copied_db_path)``. The caller owns the temp dir and must
    ``shutil.rmtree`` it. The source is only ever read.
    """
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"DuckDB file not found: {src_path}")
    temp_dir = Path(tempfile.mkdtemp(prefix="dataplane_preserve_"))
    dst = temp_dir / src_path.name
    shutil.copy2(src_path, dst)
    wal = Path(str(src_path) + ".wal")
    if wal.exists():
        shutil.copy2(wal, temp_dir / wal.name)
    return temp_dir, dst


def read_run_tables(src: str | Path, run_id: str = "") -> RunTables:
    """Read ``kpi_events`` AND ``kpi_breakdown_rows`` out of a live-locked run file.

    Copy-then-open: never touches the original beyond reading its bytes. One copy, one
    connection, both tables — a table that does not exist yields an empty list rather
    than an error, and the count is reported so a zero is visible.
    """
    temp_dir, copied = copy_db_to_temp(src)
    result = RunTables(run_id=run_id or Path(src).stem)
    try:
        conn = duckdb.connect(str(copied), read_only=True)
        try:
            result.tables = [str(r[0]) for r in conn.execute("SHOW TABLES").fetchall()]
            if KPI_TABLE in result.tables:
                rows = conn.execute(
                    f"""
                    SELECT run_id, metric, value, sim_clock
                    FROM {KPI_TABLE}
                    ORDER BY sim_clock, metric
                    """
                ).fetchall()
                result.kpi_events = [(str(r[0]), str(r[1]), float(r[2]), r[3]) for r in rows]
            else:
                logger.warning("%s has no %s table", src, KPI_TABLE)
            if BREAKDOWN_TABLE in result.tables:
                cur = conn.execute(
                    f"""
                    SELECT * FROM {BREAKDOWN_TABLE}
                    ORDER BY sim_clock, scope, entity_id
                    """
                )
                columns = [d[0] for d in cur.description]
                result.breakdown_rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            else:
                logger.warning("%s has no %s table", src, BREAKDOWN_TABLE)
        finally:
            conn.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return result


def read_run_rows(src: str | Path) -> List[KpiRow]:
    """Back-compat shim: just the ``kpi_events`` rows of :func:`read_run_tables`."""
    return read_run_tables(src).kpi_events


class _LoadedRun:
    """A DuckReader over rows already read into memory (no frames in legacy files)."""

    def __init__(
        self,
        run_id: str,
        rows: Sequence[KpiRow],
        breakdown_rows: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        self.run_id = run_id
        self.rows = list(rows)
        self.breakdown = [dict(r) for r in (breakdown_rows or [])]

    def list_run_ids(self) -> List[str]:
        return [self.run_id]

    def iter_frames(self, run_id: str) -> Iterator[Frame]:
        return iter(())

    def read_kpi_events(self, run_id: str) -> List[KpiRow]:
        return list(self.rows) if run_id == self.run_id else []

    def read_breakdown_rows(self, run_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.breakdown] if run_id == self.run_id else []


def legacy_kpi_docs(rows: Sequence[KpiRow]) -> List[Dict[str, Any]]:
    """Exact field set of ``apps/kpi_sink/mongo_export.py::rows_to_mongo_docs``."""
    docs: List[Dict[str, Any]] = []
    for row in rows:
        sim_clock = row[3]
        docs.append(
            {
                "run_id": row[0],
                "metric": row[1],
                "value": float(row[2]),
                "sim_clock": sim_clock,
                "_created": sim_clock,
                "_updated": sim_clock,
            }
        )
    return docs


def preserve_run(
    archive: MongoArchive,
    data_dir: str | Path,
    run_id: str,
    *,
    also_legacy_kpi: bool = False,
) -> Dict[str, Any]:
    """Read one run out of its (locked) file and write both tables into Mongo.

    Writes no ``dataplane_runs`` summary: this rescue carries no frames, so declaring
    the run archived would take it out of the reconciler's sight for good.
    """
    src = run_db_path(data_dir, run_id)
    tables = read_run_tables(src, run_id)

    kpi_inserted = archive.write_kpi_events(run_id, tables.kpi_events)
    breakdown_inserted = archive.write_breakdown_rows(run_id, tables.breakdown_rows)

    result: Dict[str, Any] = {
        "run_id": run_id,
        "tables": tables.tables,
        "duckdb_rows": len(tables.kpi_events),  # kept for back-compat with the old output
        "duckdb_kpi_events": len(tables.kpi_events),
        "duckdb_breakdown_rows": len(tables.breakdown_rows),
        "kpi_inserted": kpi_inserted,
        "breakdown_inserted": breakdown_inserted,
        "mongo_docs": archive.kpi.count_documents({"run_id": run_id}),
        "mongo_kpi_docs": archive.kpi.count_documents({"run_id": run_id}),
        "mongo_breakdown_docs": archive.breakdown.count_documents(
            {"run_id": run_id, "doc_type": "row"}
        ),
    }
    if also_legacy_kpi and tables.kpi_events:
        legacy = archive.db[LEGACY_KPI_COLLECTION]
        result["legacy_kpi_inserted"] = archive.insert_documents(
            legacy, legacy_kpi_docs(tables.kpi_events)
        )
    return result


def format_result(result: Dict[str, Any]) -> str:
    line = (
        f"  {result['run_id']}: duckdb_rows={result['duckdb_kpi_events']} "
        f"kpi_events duckdb={result['duckdb_kpi_events']} mongo={result['mongo_kpi_docs']} "
        f"(+{result['kpi_inserted']} new) | "
        f"kpi_breakdown_rows duckdb={result['duckdb_breakdown_rows']} "
        f"mongo={result['mongo_breakdown_docs']} (+{result['breakdown_inserted']} new)"
    )
    if "legacy_kpi_inserted" in result:
        line += f" | legacy_kpi_inserted={result['legacy_kpi_inserted']}"
    return line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preserve_surviving_runs",
        description="Copy the surviving per-run DuckDB files into the Mongo dataplane archive.",
    )
    parser.add_argument(
        "--data-dir",
        default=kpi_sink_settings["data_dir"],
        help="Directory holding the per-run .duckdb files (default: kpi_sink_settings['data_dir']).",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        dest="run_ids",
        default=None,
        help="Run id to preserve; repeatable. Default: the two surviving runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Read and report counts without connecting to or writing anything to Mongo.",
    )
    parser.add_argument(
        "--also-legacy-kpi",
        action="store_true",
        default=False,
        help="Additionally write legacy-shaped docs into the existing 'kpi' collection.",
    )
    parser.add_argument(
        "--mongo-db",
        default=None,
        help="Override the Mongo database name (defaults to kpi_sink_settings['mongo_db']).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    run_ids = list(args.run_ids) if args.run_ids else list(DEFAULT_RUN_IDS)
    data_dir = Path(args.data_dir)

    print(f"data-dir: {data_dir}")
    print(f"runs:     {', '.join(run_ids)}")
    print(f"mode:     {'DRY RUN (no Mongo connection, no writes)' if args.dry_run else 'WRITE'}")

    if args.dry_run:
        failures = 0
        for run_id in run_ids:
            try:
                tables = read_run_tables(run_db_path(data_dir, run_id), run_id)
                counts = tables.counts()
                print(
                    f"  {run_id}: duckdb_rows={counts[KPI_TABLE]} "
                    f"kpi_events={counts[KPI_TABLE]} "
                    f"kpi_breakdown_rows={counts[BREAKDOWN_TABLE]} "
                    f"mongo_docs=(not queried, dry run)"
                )
            except Exception as exc:  # noqa: BLE001 - report and continue
                failures += 1
                print(f"  {run_id}: FAILED {type(exc).__name__}: {exc}")
        return 1 if failures else 0

    archive = MongoArchive(db_name=args.mongo_db)
    try:
        if not archive.ping():
            print("Mongo is not reachable — nothing written.")
            return 2
        archive.ensure_indexes()
        failures = 0
        for run_id in run_ids:
            try:
                result = preserve_run(
                    archive, data_dir, run_id, also_legacy_kpi=args.also_legacy_kpi
                )
                print(format_result(result))
            except Exception as exc:  # noqa: BLE001 - report and continue
                failures += 1
                print(f"  {run_id}: FAILED {type(exc).__name__}: {exc}")
                logger.exception("preserve failed for run_id=%s", run_id)
        return 1 if failures else 0
    finally:
        archive.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
