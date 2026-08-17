"""Payload parsing helpers for dataplane ingest.

Copied (deliberately, not imported) from ``apps/kpi_sink/kpi_parse.py`` and
``apps/kpi_sink/run_status.py``: ``apps.kpi_sink`` is being retired, and the status tables plus
the ``lifecycle_scope`` rule below are load-bearing — a run whose terminal message is missed is
a run that never gets archived. Behaviour is byte-for-byte equivalent to the sink's version.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from apps.utils.utils import str_to_time

# --------------------------------------------------------------------------- kpi payloads


def parse_sim_clock(raw: Any) -> datetime:
    """Parse a ``sim_clock`` field into a (naive, UTC) datetime.

    Fast path is the exact RFC-1123 format the publishers emit; dateutil is the fallback.
    """
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"sim_clock must be str or datetime, got {type(raw)!r}")
    try:
        return str_to_time(raw)
    except ValueError:
        from dateutil import parser as date_parser

        return date_parser.parse(raw)


def coerce_metric_value(raw: Any) -> float:
    if raw is None:
        return 0.0
    return float(raw)


def sim_time_ms(raw: Any) -> float:
    """``sim_clock`` -> UTC epoch milliseconds as float. Naive datetimes are treated as UTC.

    Mirrors ``apps.dataplane.contract.frame.sim_time_ms_from_iso`` but is computed locally so
    ingest never has to import the frame contract just to stamp a timestamp.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    dt = parse_sim_clock(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


# --------------------------------------------------------------------------- run_status

RunTerminalOutcome = Literal[
    "completed",
    "failed",
    "cancelled",
    "stopped",
    "aborted",
    "terminated",
    "interrupted",
    "crashed",
    "timeout",
    "inactive",
]

# Any of these statuses triggers the DuckDB -> Mongo archive dump.
_EXPORT_STATUSES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "ERROR",
        "CANCELLED",
        "CANCELED",
        "STOP",
        "STOPPED",
        "ABORT",
        "ABORTED",
        "KILLED",
        "TERMINATED",
        "INTERRUPTED",
        "CRASHED",
        "TIMEOUT",
        "TIMED_OUT",
    }
)


def _extract_status(payload: Any) -> Optional[str]:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("status", "run_status", "state"):
            raw = payload.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def is_simulation_lifecycle_message(payload: Any) -> bool:
    """Ignore non-simulation run_status events when ``lifecycle_scope`` is present."""
    if not isinstance(payload, dict):
        return True
    scope = payload.get("lifecycle_scope")
    if scope is None:
        return True
    return scope == "simulation"


def should_export_run_status(payload: Any) -> Optional[str]:
    """Return a short reason string when this run's rows should be archived to Mongo.

    Triggers on normal completion, failures, user/API stops, and any other non-RUNNING
    simulation lifecycle message with ``simulation_active=False``.
    """
    if not is_simulation_lifecycle_message(payload):
        return None

    status = _extract_status(payload)
    if status:
        normalized = status.upper()
        if normalized in _EXPORT_STATUSES:
            return _outcome_label(normalized)
        if normalized != "RUNNING":
            # Catch-all for future/custom stop statuses (e.g. STOPPING -> stopped).
            if isinstance(payload, dict) and payload.get("simulation_active") is False:
                return _outcome_label(normalized)
            if "STOP" in normalized or "CANCEL" in normalized or "ABORT" in normalized:
                return _outcome_label(normalized)

    if isinstance(payload, dict) and payload.get("simulation_active") is False:
        return "inactive"

    return None


def _outcome_label(status_upper: str) -> str:
    if status_upper == "COMPLETED":
        return "completed"
    if status_upper in ("FAILED", "ERROR", "CRASHED"):
        return "failed"
    if status_upper in ("CANCELLED", "CANCELED"):
        return "cancelled"
    if status_upper in ("STOP", "STOPPED", "STOPPING"):
        return "stopped"
    if status_upper in ("ABORT", "ABORTED"):
        return "aborted"
    if status_upper in ("KILLED", "TERMINATED"):
        return "terminated"
    if status_upper == "INTERRUPTED":
        return "interrupted"
    if status_upper in ("TIMEOUT", "TIMED_OUT"):
        return "timeout"
    return status_upper.lower()


def parse_run_terminal_outcome(value: Any) -> Optional[RunTerminalOutcome]:
    """Map run_status JSON (or a legacy bare string) to a known terminal outcome, if any."""
    reason = should_export_run_status(value)
    if reason is None:
        return None
    allowed: tuple[str, ...] = (
        "completed",
        "failed",
        "cancelled",
        "stopped",
        "aborted",
        "terminated",
        "interrupted",
        "crashed",
        "timeout",
        "inactive",
    )
    if reason in allowed:
        return reason  # type: ignore[return-value]
    return "inactive"
