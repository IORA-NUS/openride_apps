"""One-off: import the legacy sink's Mongo history into the dataplane's DuckDB.

Why this exists
---------------
The dataplane's cache fills itself from its archive on any read (``read_api._ensure_available``),
so a run it has ever seen is always available. Runs that predate it are a different matter: they
live in ``OpenRoadDB`` in the *old kpi_sink's* document shape —

    kpi:                              {run_id, metric, value, sim_clock}
    container_logistics_kpi_breakdown:{run_id, scope, sim_clock, final, breakdown:{entities}}

— not the dataplane archive's shape, so ``MongoArchive.has_run`` cannot see them and rehydrate
has nothing to pull. This walks that history into DuckDB once. From then on the ordinary
reconcile sweep dumps each run to the dataplane's own archive, and the automatic path owns it.

Idempotent: every write is an upsert on the store's primary keys, so re-running changes nothing.

Usage (the dataplane must be STOPPED — DuckDB takes a single writer):

    systemctl --user stop dataplane-live
    PYTHONPATH=/home/user:/home/user/openride_apps venv/bin/python -m apps.dataplane.tools.import_legacy \\
        --db /home/user/.openride/dataplane-live/dataplane.duckdb --mongo-db OpenRoadDB
    systemctl --user start dataplane-live
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("import_legacy")

LEGACY_KPI = "kpi"
LEGACY_BREAKDOWN = "container_logistics_kpi_breakdown"


def _as_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def import_run(store, db, run_id: str) -> Dict[str, int]:
    """Copy one run's legacy KPI and breakdown docs into the store. Returns what was written."""
    rows: List[Tuple[str, str, float, datetime]] = []
    for doc in db[LEGACY_KPI].find({"run_id": run_id}, {"_id": 0}):
        clock = _as_dt(doc.get("sim_clock"))
        metric = doc.get("metric")
        if clock is None or not metric:
            continue
        try:
            value = float(doc.get("value") or 0.0)
        except (TypeError, ValueError):
            continue
        rows.append((run_id, str(metric), value, clock))

    # One snapshot per (scope, sim_clock); the legacy collection stores the entity list nested.
    snaps: Dict[Tuple[str, datetime], Tuple[bool, List[Dict[str, Any]]]] = {}
    for doc in db[LEGACY_BREAKDOWN].find({"run_id": run_id}, {"_id": 0}):
        clock = _as_dt(doc.get("sim_clock"))
        scope = doc.get("scope")
        if clock is None or not scope:
            continue
        entities = ((doc.get("breakdown") or {}).get("entities")) or []
        if not isinstance(entities, list):
            continue
        key = (str(scope), clock)
        prev = snaps.get(key)
        # A re-sent snapshot at the same instant: keep the authoritative one.
        if prev is None or (bool(doc.get("final")) and not prev[0]):
            snaps[key] = (bool(doc.get("final")), [e for e in entities if isinstance(e, dict)])

    written_kpi = store.write_kpi_events(rows) if rows else 0
    written_bd = 0
    if snaps:
        batch = [(scope, clock, final, ents) for (scope, clock), (final, ents) in snaps.items()]
        written_bd = store.write_breakdown_batch(run_id, batch)

    clocks = [r[3] for r in rows] + [c for _s, c in snaps]
    if clocks:
        store.upsert_run_meta(
            run_id,
            first_seen=min(clocks),
            last_seen=max(clocks),
            status="completed",
            source="legacy-import",
        )
    return {"kpi": written_kpi, "breakdown": written_bd, "snapshots": len(snaps)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to the dataplane DuckDB file")
    ap.add_argument("--mongo-db", default="OpenRoadDB", help="legacy database to read")
    ap.add_argument("--mongo-host", default="localhost")
    ap.add_argument("--mongo-port", type=int, default=27017)
    ap.add_argument("--only", nargs="*", help="limit to these run_ids")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from pymongo import MongoClient

    from apps.dataplane.store.duck import DuckStore

    client = MongoClient(args.mongo_host, args.mongo_port, serverSelectionTimeoutMS=5000)
    db = client[args.mongo_db]

    run_ids = set(args.only or [])
    if not run_ids:
        for coll in (LEGACY_KPI, LEGACY_BREAKDOWN):
            run_ids.update(r for r in db[coll].distinct("run_id") if isinstance(r, str))
    ordered = sorted(run_ids)
    logger.info("legacy runs found: %d", len(ordered))
    if args.dry_run:
        for rid in ordered:
            logger.info(
                "  %s  kpi=%d breakdown=%d",
                rid,
                db[LEGACY_KPI].count_documents({"run_id": rid}),
                db[LEGACY_BREAKDOWN].count_documents({"run_id": rid}),
            )
        return 0

    store = DuckStore(args.db)
    totals = defaultdict(int)
    try:
        for i, rid in enumerate(ordered, 1):
            try:
                got = import_run(store, db, rid)
            except Exception:  # noqa: BLE001 - one bad run must not abort the history
                logger.exception("  [%d/%d] %s FAILED", i, len(ordered), rid)
                totals["failed"] += 1
                continue
            for k, v in got.items():
                totals[k] += v
            logger.info(
                "  [%d/%d] %s  kpi=%d breakdown=%d (%d snapshots)",
                i, len(ordered), rid, got["kpi"], got["breakdown"], got["snapshots"],
            )
    finally:
        store.close()
        client.close()
    logger.info(
        "imported: %d kpi rows, %d breakdown rows, %d runs failed",
        totals["kpi"], totals["breakdown"], totals["failed"],
    )
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
