"""Read-only data access for the CLI — the same sources the dashboard reads.

Live tiles come from the ``container_logistics_kpi_breakdown`` snapshots the analytics
agent persists during the run (written by the app itself, not by any sink); the
authoritative end-of-run numbers carry ``final=True``.

Scalar KPIs go through :meth:`MongoReader.latest_scalar_kpis`, which reads the
**apps/dataplane read API first** and falls back to the legacy Mongo ``kpi`` collection on
an error or an empty result. The old docstring here said scalar KPIs are "populated at run
end" — that was a property of the retiring ``apps/kpi_sink``, not of the data, and it is
why a killed run showed no scalars at all.

Nothing in this module writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pymongo import DESCENDING, MongoClient

from . import config


@dataclass
class BreakdownSnapshot:
    entities: list[dict[str, Any]] = field(default_factory=list)
    final: bool = False
    sim_clock: Any = None


class MongoReader:
    """Lazy, read-only reader. Safe to construct even if Mongo is down (errors surface on use)."""

    def __init__(self, uri: str | None = None, db_name: str | None = None):
        self._uri = uri or config.MONGO_URI
        self._db_name = db_name or config.MONGO_DB
        self._client: MongoClient | None = None

    # -- connection -------------------------------------------------------
    @property
    def db(self):
        if self._client is None:
            self._client = MongoClient(self._uri, serverSelectionTimeoutMS=4000)
        return self._client[self._db_name]

    def ping(self) -> bool:
        try:
            self.db.command("ping")
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- breakdowns -------------------------------------------------------
    def latest_breakdown(self, run_id: str, scope: str, final_only: bool = False) -> BreakdownSnapshot:
        """Best breakdown snapshot for a scope ('truck' | 'haulier').

        Prefer the authoritative end-of-run ``final=True`` doc when one exists (it shares the
        last live snapshot's sim_clock, so a plain sort can't be relied on to surface it);
        otherwise fall back to the newest live snapshot.
        """
        col_match: dict[str, Any] = {"run_id": run_id, "scope": scope}
        try:
            col = self.db[config.BREAKDOWN_COLLECTION]
            doc = col.find_one({**col_match, "final": True}, sort=[("sim_clock", DESCENDING)])
            if doc is None and not final_only:
                doc = col.find_one(col_match, sort=[("sim_clock", DESCENDING)])
        except Exception:
            return BreakdownSnapshot()
        if not doc:
            return BreakdownSnapshot()
        breakdown = doc.get("breakdown") or {}
        entities = breakdown.get("entities") or []
        return BreakdownSnapshot(
            entities=list(entities),
            final=bool(doc.get("final")),
            sim_clock=doc.get("sim_clock"),
        )

    # -- scalar KPIs -------------------------------------------------------
    def latest_scalar_kpis(self, run_id: str) -> dict[str, float]:
        """``{metric: latest value}`` — dataplane first, legacy Mongo ``kpi`` as fallback.

        The same rule the dashboard follows (``analytics/lib/dataplaneSource.ts``): try the
        dataplane, fall back to Mongo on an error **or on an empty result**. The dataplane's
        DuckDB only holds runs it consumed (nothing before 2026-08-07), so an older run is an
        ordinary empty result rather than a failure — reading it as a failure is what caused
        the blank-page incident recorded in CLAUDE.md §6.7.

        Why the order flipped: the legacy ``apps/kpi_sink`` exported to ``kpi`` only when a
        terminal ``run_status`` arrived, so a killed or crashed run persisted **nothing**, and
        its consumer was at-most-once (auto-commit, no manual commit). The dataplane writes
        continuously at ingest. Neither collection is ever written from here.
        """
        out = self._scalar_kpis_from_dataplane(run_id)
        if out:
            return out
        return self._scalar_kpis_from_mongo(run_id)

    @staticmethod
    def _sim_clock_key(value: Any) -> float:
        """Order-comparable ``sim_clock``, tolerating both the numeric and ISO forms.

        The dataplane serves epoch-ms numbers; the legacy collection holds datetimes. Mixing
        them in one comparison is what ``types/index.ts`` warns about, so neither branch ever
        sorts across the two.
        """
        if isinstance(value, (int, float)):
            return float(value)
        try:
            from datetime import datetime

            if isinstance(value, datetime):
                return value.timestamp()
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")

    def _scalar_kpis_from_dataplane(self, run_id: str) -> dict[str, float]:
        """``GET /kpi/scalars`` -> ``{metric: [rows...]}``; take the newest row per metric."""
        import json
        import urllib.parse
        import urllib.request

        url = (
            f"{config.DATAPLANE_URL.rstrip('/')}/kpi/scalars?"
            + urllib.parse.urlencode({"run_id": run_id})
        )
        try:
            with urllib.request.urlopen(url, timeout=config.DATAPLANE_TIMEOUT_S) as resp:
                if resp.status != 200:
                    return {}
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # Down, hung past the timeout, or serving something unparseable — all the same
            # to the caller: fall through to Mongo.
            return {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, float] = {}
        for metric, rows in payload.items():
            if not isinstance(rows, list) or not rows:
                continue
            newest = max(
                (r for r in rows if isinstance(r, dict)),
                key=lambda r: self._sim_clock_key(r.get("sim_clock")),
                default=None,
            )
            if newest is None or newest.get("value") is None:
                continue
            try:
                out[str(metric)] = float(newest["value"])
            except (TypeError, ValueError):
                continue
        return out

    def _scalar_kpis_from_mongo(self, run_id: str) -> dict[str, float]:
        """The legacy ``kpi`` collection. Read-only, and frozen once the sink is retired."""
        out: dict[str, float] = {}
        try:
            col = self.db[config.KPI_COLLECTION]
            for metric in col.distinct("metric", {"run_id": run_id}):
                doc = col.find_one(
                    {"run_id": run_id, "metric": metric}, sort=[("sim_clock", DESCENDING)]
                )
                if doc and doc.get("value") is not None:
                    try:
                        out[metric] = float(doc["value"])
                    except (TypeError, ValueError):
                        pass
        except Exception:
            return out
        return out

    # -- run metadata -----------------------------------------------------
    def run_config(self, run_id: str) -> dict[str, Any]:
        try:
            doc = self.db[config.RUN_CONFIG_COLLECTION].find_one({"run_id": run_id})
            return doc or {}
        except Exception:
            return {}

    def list_runs(self, *, limit: int = 25, scenario: str | None = None) -> list[dict[str, Any]]:
        """Recent runs (newest first) from the ``run_config`` collection.

        Flattens the bits a CLI cares about out of the doc + its ``meta`` sub-doc
        (scenario slug, run name, status, agent counts, updated time).
        """
        out: list[dict[str, Any]] = []
        try:
            col = self.db[config.RUN_CONFIG_COLLECTION]
            query: dict[str, Any] = {}
            if scenario:
                query["meta.scenario_slug"] = scenario
            for doc in col.find(query, sort=[("_updated", DESCENDING)]).limit(limit):
                meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
                out.append(
                    {
                        "run_id": doc.get("run_id"),
                        "name": doc.get("name"),
                        "status": doc.get("status"),
                        "scenario_slug": meta.get("scenario_slug"),
                        "trucks": meta.get("num_truck_agents"),
                        "orders": meta.get("num_order_agents"),
                        "updated": doc.get("_updated"),
                    }
                )
        except Exception:
            return out
        return out


def fleet_totals(haulier_entities: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-haulier rows into one fleet-wide summary row."""
    total = {
        "num_trucks": 0,
        "num_orders_completed": 0,
        "empty_km": 0.0,
        "loaded_km": 0.0,
        "total_km": 0.0,
        "dual_cycle_count": 0,
        "chain_opportunities": 0,
        "active_hours": 0.0,
        "num_hauliers": len(haulier_entities),
    }
    for e in haulier_entities:
        total["num_trucks"] += int(e.get("num_trucks") or 0)
        total["num_orders_completed"] += int(e.get("num_orders_completed") or 0)
        total["empty_km"] += float(e.get("empty_km") or 0.0)
        total["loaded_km"] += float(e.get("loaded_km") or 0.0)
        total["total_km"] += float(e.get("total_km") or 0.0)
        total["dual_cycle_count"] += int(e.get("dual_cycle_count") or 0)
        total["chain_opportunities"] += int(e.get("chain_opportunities") or 0)
        total["active_hours"] += float(e.get("active_hours") or 0.0)
    total["empty_ratio"] = (total["empty_km"] / total["total_km"]) if total["total_km"] else 0.0
    total["dual_cycle_rate"] = (
        total["dual_cycle_count"] / total["chain_opportunities"]
        if total["chain_opportunities"]
        else 0.0
    )
    trucks = total["num_trucks"] or 0
    total["orders_per_truck"] = (total["num_orders_completed"] / trucks) if trucks else 0.0
    return total
