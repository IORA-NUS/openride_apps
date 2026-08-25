"""In-process HTTP read API over the DuckDB breakdown store (Phase 2).

The dashboard's ``/api/breakdown`` + ``/api/breakdown/series`` routes proxy here when
``BREAKDOWN_SOURCE=duck``. Runs inside the kpi-duckdb-sink process because DuckDB is single-writer
per file — a separate reader process can't open a file the sink holds open, so the reader must share
the sink's own connection (via ``DuckDbKpiStore.query``, which locks).

Responses mirror the Mongo-backed routes exactly (``BreakdownPayload`` / ``BreakdownSeriesPayload``)
so no frontend rendering changes are needed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

MAX_SERIES_POINTS = 240


def _epoch_ms(dt: Any) -> Optional[int]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _cutoff_dt(sim_time_ms: Optional[int]) -> Optional[datetime]:
    if sim_time_ms is None:
        return None
    return datetime.fromtimestamp(sim_time_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)


def breakdown_latest(store, run_id: str, scope: str, sim_time_ms: Optional[int]) -> Dict[str, Any]:
    """Latest breakdown snapshot for one scope (≤ sim_time_ms when replaying)."""
    cutoff = _cutoff_dt(sim_time_ms)
    rows = store.query(
        run_id,
        """
        WITH latest AS (
            SELECT max(sim_clock) AS mc FROM kpi_breakdown_rows
            WHERE run_id = ? AND scope = ? AND (? IS NULL OR sim_clock <= ?)
        )
        SELECT payload, final, sim_clock FROM kpi_breakdown_rows
        WHERE run_id = ? AND scope = ? AND sim_clock = (SELECT mc FROM latest)
        """,
        [run_id, scope, cutoff, cutoff, run_id, scope],
    )
    if not rows:
        return {"entities": [], "final": False, "simTimeMs": None}
    entities: List[Dict[str, Any]] = []
    for r in rows:
        try:
            entities.append(json.loads(r["payload"]))
        except (TypeError, ValueError):
            continue
    return {
        "entities": entities,
        "final": bool(rows[0].get("final")),
        "simTimeMs": _epoch_ms(rows[0].get("sim_clock")),
    }


def breakdown_payload(store, run_id: str, sim_time_ms: Optional[int]) -> Dict[str, Any]:
    return {
        "runId": run_id,
        "truck": breakdown_latest(store, run_id, "truck", sim_time_ms),
        "haulier": breakdown_latest(store, run_id, "haulier", sim_time_ms),
    }


def _downsample(points: List[Dict[str, Any]], max_n: int) -> List[Dict[str, Any]]:
    if len(points) <= max_n:
        return points
    step = len(points) / max_n
    out = [points[int(i * step)] for i in range(max_n - 1)]
    out.append(points[-1])
    return out


def breakdown_series(store, run_id: str, sim_time_ms: Optional[int]) -> Dict[str, Any]:
    """Per-haulier metric series over time (mirrors /api/breakdown/series)."""
    cutoff = _cutoff_dt(sim_time_ms)
    rows = store.query(
        run_id,
        """
        SELECT sim_clock, entity_id, haulier_name, empty_ratio, orders_per_day,
               active_hours, dual_cycle_rate
        FROM kpi_breakdown_rows
        WHERE run_id = ? AND scope = 'haulier' AND (? IS NULL OR sim_clock <= ?)
        ORDER BY sim_clock
        """,
        [run_id, cutoff, cutoff],
    )
    by_haulier: Dict[str, Dict[str, Any]] = {}
    t_min: Optional[int] = None
    t_max: Optional[int] = None
    for r in rows:
        t = _epoch_ms(r.get("sim_clock"))
        if t is None:
            continue
        t_min = t if t_min is None else min(t_min, t)
        t_max = t if t_max is None else max(t_max, t)
        hid = str(r.get("entity_id"))
        s = by_haulier.get(hid)
        if s is None:
            s = {"id": hid, "name": r.get("haulier_name") or hid, "points": []}
            by_haulier[hid] = s
        s["points"].append({
            "t": t,
            "empty_ratio": float(r.get("empty_ratio") or 0.0),
            "orders_per_day": float(r.get("orders_per_day") or 0.0),
            "active_hours": float(r.get("active_hours") or 0.0),
            "dual_cycle_rate": float(r.get("dual_cycle_rate") or 0.0),
        })
    series = [
        {**s, "points": _downsample(s["points"], MAX_SERIES_POINTS)}
        for s in by_haulier.values()
    ]
    series.sort(key=lambda s: str(s["name"]))
    return {"runId": run_id, "tMin": t_min, "tMax": t_max, "series": series}


def make_breakdown_http_server(store, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr access log
            pass

        def _send(self, code: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            run_id = (qs.get("runId") or qs.get("run_id") or [""])[0]
            raw_ms = (qs.get("simTimeMs") or [""])[0]
            try:
                sim_time_ms = int(float(raw_ms)) if raw_ms not in ("", None) else None
            except (TypeError, ValueError):
                sim_time_ms = None
            try:
                if parsed.path == "/health":
                    self._send(200, {"ok": True})
                    return
                if not run_id:
                    self._send(400, {"error": "runId is required"})
                    return
                if parsed.path == "/breakdown":
                    self._send(200, breakdown_payload(store, run_id, sim_time_ms))
                elif parsed.path == "/breakdown/series":
                    self._send(200, breakdown_series(store, run_id, sim_time_ms))
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("breakdown api error path=%s", parsed.path)
                self._send(500, {"error": "breakdown query failed", "detail": str(exc)})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
