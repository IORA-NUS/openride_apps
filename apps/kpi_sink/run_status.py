"""Parse run_status payloads for simulation stop / terminal events."""

from __future__ import annotations

from typing import Any, Literal, Optional

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

# Any of these statuses triggers DuckDB → Mongo export.
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


def parse_run_terminal_outcome(value: Any) -> Optional[RunTerminalOutcome]:
    """Map run_status JSON (or legacy string) to a known terminal outcome, if any."""
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


def should_export_run_status(payload: Any) -> Optional[str]:
    """
    Return a short reason string when DuckDB rows for this run should be exported to Mongo.

    Triggers on normal completion, failures, user/API stops, and any other non-RUNNING
    simulation lifecycle message with simulation_active=False.
    """
    if not is_simulation_lifecycle_message(payload):
        return None

    status = _extract_status(payload)
    if status:
        normalized = status.upper()
        if normalized in _EXPORT_STATUSES:
            return _outcome_label(normalized)
        if normalized != "RUNNING":
            # Catch-all for future/custom stop statuses (e.g. STOPPING → stopped).
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


def is_simulation_lifecycle_message(payload: Any) -> bool:
    """Ignore non-simulation run_status events when lifecycle_scope is present."""
    if not isinstance(payload, dict):
        return True
    scope = payload.get("lifecycle_scope")
    if scope is None:
        return True
    return scope == "simulation"
