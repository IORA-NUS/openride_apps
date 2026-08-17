"""Preflight checks before starting a long simulation run."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_APPS_ROOT = Path(__file__).resolve().parents[1]
_VENV_PYTHON = _APPS_ROOT / "venv" / "bin" / "python"


def _tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _inprocess_celery_ping(timeout: float) -> bool | None:
    """Fast in-process AMQP ping (early-returns on the first worker reply).

    Returns True if a worker replied, or None if inconclusive (the caller should
    fall back to the authoritative — but ~timeout-long — subprocess ping). See
    ``openride_control.registry._inprocess_celery_ping`` for the full rationale.
    """
    try:
        from apps.celery_worker import app  # type: ignore
    except Exception:
        return None
    try:
        replies = app.control.ping(timeout=max(0.5, min(timeout, 2.0)), limit=1)
    except Exception:
        return None
    return True if replies else None


def _celery_ping_subprocess(timeout: float) -> bool:
    python = str(_VENV_PYTHON if _VENV_PYTHON.is_file() else sys.executable)
    result = subprocess.run(
        [
            python,
            "-m",
            "celery",
            "-A",
            "apps.celery_worker",
            "inspect",
            "ping",
            f"--timeout={int(max(1, timeout))}",
        ],
        cwd=str(_APPS_ROOT),
        capture_output=True,
        text=True,
    )
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
    ok = result.returncode == 0 and "pong" in output
    if not ok:
        logger.error("Celery ping failed (rc=%s): %s", result.returncode, output.strip()[:500])
    return ok


def celery_ping(timeout: float = 10.0) -> bool:
    """True when at least one Celery worker responds.

    Fast in-process ping first (~80ms happy path); the slow subprocess ping only
    runs when the fast path is inconclusive.
    """
    fast = _inprocess_celery_ping(timeout)
    if fast:
        return True
    return _celery_ping_subprocess(timeout)


def _celery_worker_process_exists() -> bool:
    """Pure PID check — no ping. Used only to pick the right error message when a
    ping fails (a running-but-unresponsive worker vs no worker at all)."""
    for pattern in ("celery -A apps.celery_worker worker", "celery -A orsim.worker worker"):
        if subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0:
            return True
    return False


def celery_process_running() -> bool:
    try:
        from openride_control.registry import celery_is_healthy

        return celery_is_healthy(ping_timeout=3.0)
    except ImportError:
        return _celery_worker_process_exists()


def assert_simulation_dependencies() -> None:
    """Fail fast with a clear message if agent workers cannot run."""
    missing: list[str] = []

    if not _tcp_open("127.0.0.1", 5672):
        missing.append("RabbitMQ AMQP (127.0.0.1:5672) is not reachable")
    if not _tcp_open("127.0.0.1", 11654):
        missing.append("OpenRide API gateway (127.0.0.1:11654) is not reachable")

    if missing:
        raise RuntimeError(
            "Simulation dependencies not ready:\n- "
            + "\n- ".join(missing)
            + "\nStart backend services (rabbit, nginx, celery) before running the simulation."
        )

    # A single liveness ping (fast in-process happy path; slow subprocess only when
    # inconclusive). The error message is disambiguated by a cheap pgrep — NOT by
    # extra pings, which is what made preflight cost ~23s (3× 10s inspect-ping).
    if not celery_ping():
        if _celery_worker_process_exists():
            raise RuntimeError(
                "Celery worker process exists but is not responding to ping. "
                "The worker likely lost its RabbitMQ connection (common after RabbitMQ restarts). "
                "Restart Celery from the Services page or run: pkill -f 'celery -A'; ./start_celery.sh"
            )
        raise RuntimeError(
            "No healthy Celery worker is running. Agent steps will hang and the simulation will fail. "
            "Start Celery before launching the simulation."
        )

    logger.info("Preflight OK: RabbitMQ, API gateway, and Celery worker are reachable.")
