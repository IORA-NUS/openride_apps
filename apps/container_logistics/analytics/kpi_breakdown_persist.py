"""Persist per-entity KPI breakdown rows (truck / haulier) for distribution + company views.

Mirrors ``facility_snapshot_persist`` — POSTs a nested-dict document to the ``kpi_breakdown``
OpenRide resource. One document per (run_id, scope, sim_clock); ``final=True`` marks the
authoritative end-of-run recompute.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apps.common.resource_client_mixin import get_http_session
from apps.config import settings, simulation_domains
from apps.utils import time_to_str


def _publish_breakdown_kafka(body: Dict[str, Any]) -> None:
    """Publish a breakdown snapshot to ``kpi_breakdown_stream`` (keyed by run_id) for the DuckDB
    sink. Best-effort — never break the analytics tick / finalize if Kafka is unavailable."""
    try:
        from apps.utils.kafka_utils import push_event, resolve_topic

        push_event(resolve_topic("kpi_breakdown"), payload=body, key=body["run_id"])
    except Exception as exc:  # pragma: no cover - transport best-effort
        logging.debug("kpi_breakdown kafka publish skipped (%s)", exc)


def _sim_clock_for_api(raw: Any) -> Optional[str]:
    """OpenRide expects RFC1123 GMT strings, not ISO-8601 Z (Eve datetime validation)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return time_to_str(raw)
    if isinstance(raw, str):
        # Try ISO-8601 → RFC1123. Don't sniff for a 'T' separator: RFC1123 GMT strings
        # ("Wed, 08 Jan 2020 09:36:00 GMT") contain a 'T' inside "GMT" and would be
        # misrouted into fromisoformat and crash. If it isn't ISO it's already an
        # RFC1123 / server-acceptable string, so return it unchanged.
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return time_to_str(dt)
    return str(raw)


def persist_kpi_breakdown(
    user: Any,
    run_id: str,
    *,
    scope: str,
    sim_clock: Any,
    rows: List[Dict[str, Any]],
    final: bool = False,
) -> None:
    if user is None:
        return
    domain = simulation_domains.get("container_logistics", "container-logistics-sim")
    url = f"{settings['OPENRIDE_SERVER_URL']}/{domain}/{run_id}/kpi_breakdown"
    clock = _sim_clock_for_api(sim_clock)
    if not clock or scope not in ("truck", "haulier", "lane", "planner"):
        return
    body = {
        "run_id": run_id,
        "scope": scope,
        "sim_clock": clock,
        "final": bool(final),
        "breakdown": {"entities": rows, "count": len(rows)},
    }
    # Phase 2: also publish to kpi_breakdown_stream so the DuckDB sink can store rows for the
    # SQL-backed read API. Best-effort, independent of the Mongo POST below (kept during
    # transition). The sink upserts on (run_id, scope, sim_clock, entity_id) → idempotent.
    _publish_breakdown_kafka(body)
    try:
        response = get_http_session().post(
            url,
            headers=user.get_headers(),
            data=json.dumps(body),
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )
    except Exception as exc:  # best-effort: never break the analytics tick / finalize
        logging.warning("kpi_breakdown persist error (%s/%s): %s", run_id, scope, exc)
        return
    if response.status_code >= 400:
        logging.warning(
            "kpi_breakdown persist failed (%s) scope=%s: %s",
            response.status_code,
            scope,
            response.text[:500],
        )
