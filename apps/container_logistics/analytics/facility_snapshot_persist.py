"""Persist facility_snapshot rows to OpenRide for map replay."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from apps.common.resource_client_mixin import get_http_session
from apps.config import settings, simulation_domains
from apps.utils import time_to_str


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


def persist_facility_snapshot(user: Any, run_id: str, payload: Dict[str, Any]) -> None:
    if user is None:
        return
    domain = simulation_domains.get("container_logistics", "container-logistics-sim")
    url = (
        f"{settings['OPENRIDE_SERVER_URL']}/{domain}/{run_id}/facility_snapshot"
    )
    sim_clock = _sim_clock_for_api(payload.get("sim_clock"))
    body = {
        "run_id": run_id,
        "facility_id": str(payload.get("facility_id") or ""),
        "sim_clock": sim_clock,
        "snapshot": payload,
    }
    if not body["facility_id"] or not body["sim_clock"]:
        return
    response = get_http_session().post(
        url,
        headers=user.get_headers(),
        data=json.dumps(body),
        timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
    )
    if response.status_code >= 400:
        logging.warning(
            "facility_snapshot persist failed (%s): %s",
            response.status_code,
            response.text[:500],
        )
