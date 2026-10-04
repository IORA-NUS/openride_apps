"""Preflight checks before starting a long simulation run."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_APPS_ROOT = Path(__file__).resolve().parents[1]
_VENV_PYTHON = _APPS_ROOT / "venv" / "bin" / "python"

# Deliberately short: a HEALTHY local OSRM answers in single-digit ms, so this only
# has to outlast a scheduling blip, not a slow route.
_OSRM_PROBE_TIMEOUT = float(os.environ.get("OPENRIDE_OSRM_PREFLIGHT_TIMEOUT", "5"))

# A healthy local broker answers a metadata request in single-digit ms; this only has
# to outlast a scheduling blip. Kept separate from the OSRM budget so either can be
# tuned without moving the other.
_KAFKA_PROBE_TIMEOUT = float(os.environ.get("OPENRIDE_KAFKA_PREFLIGHT_TIMEOUT", "5"))


def _tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _osrm_serving(base_url: str, timeout: float = _OSRM_PROBE_TIMEOUT) -> bool:
    """True when OSRM actually ANSWERS a route request.

    A TCP check is not enough, and that is the whole point of this probe. On
    2026-09-07 OSRM stopped serving 58 s into a 572 s run while the container still
    reported ``Up`` and the port still accepted connections: every route request then
    hung until ``OSRM_TIMEOUT_SECONDS`` (5 s), and an assignment plans TWO legs, so each
    one cost a flat 10 s. That produced 88 barrier stalls, 5,945 agent prunes, and left
    90.1% of the run's trips with ``geometry_source: unavailable`` — i.e. haversine
    distance instead of road distance (~50% low, CLAUDE.md §6.3) — while the run still
    reported ``status: completed, ok: true``.

    ANY HTTP status counts as alive: a 400 means OSRM parsed the request, which is all
    we are testing. Only a hang, a refused connection, or a malformed reply means wedged,
    so the probe never depends on which region graph is loaded.
    """
    url = f"{base_url.rstrip('/')}/route/v1/driving/0,0;0,0?overview=false"
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
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


def _celery_broker_url() -> str:
    """The broker URL the celery workers are actually configured with.

    Read from the environment exactly as ``apps/celery_worker.py`` reads it, so
    the preflight gate and the workers can never disagree about which broker
    this run needs.
    """
    return os.environ.get("CELERY_BROKER_URL", "amqp://guest:guest@127.0.0.1:5672//")


def _agent_transport_name() -> str:
    """``paho`` (today's default) or the name of the transport that replaced it."""
    try:
        from apps.config import messenger_backend

        return str(messenger_backend.get("AGENT_TRANSPORT", "paho") or "paho").lower()
    except Exception:
        return "paho"


def _kafka_serving(bootstrap: str, run_id: str | None, timeout: float) -> str | None:
    """None when Kafka really answers; otherwise the reason it does not.

    A REAL REQUEST, not a TCP check — the same lesson §6.21 paid for with OSRM,
    whose port stayed open for the whole outage. ``list_topics`` is a metadata
    round trip the broker has to serve, and when a run id is known the lane
    topics it already carries are checked for a SHRUNKEN partition count: a lane
    topic resurrected by ``auto.create.topics.enable=true`` at
    ``num.partitions=1`` makes publisher and subscriber compute different
    partitions from the same key, after which records vanish with no error
    anywhere (``lane_admin._reject_shrunken_lanes`` refuses that at boot; this
    says so before the run starts instead).
    """
    try:
        from confluent_kafka.admin import AdminClient
    except Exception as exc:  # pragma: no cover - confluent_kafka is in requirements
        return f"confluent_kafka is not importable ({exc})"
    try:
        metadata = AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=timeout)
    except Exception as exc:
        return f"metadata request failed ({exc})"
    if not metadata or not metadata.brokers:
        return "the broker answered with no brokers in its metadata"

    if not run_id:
        return None
    try:
        from apps.transport.lane_admin import (
            DEFAULT_APP_PARTITIONS,
            DEFAULT_BCAST_PARTITIONS,
            DEFAULT_REPLY_PARTITIONS,
            lane_topic_names,
        )

        wanted = {
            "bcast": DEFAULT_BCAST_PARTITIONS,
            "reply": int(os.environ.get("KAFKA_REPLY_PARTITIONS", DEFAULT_REPLY_PARTITIONS)),
            "app": int(os.environ.get("KAFKA_APP_PARTITIONS", DEFAULT_APP_PARTITIONS)),
        }
        for lane, name in lane_topic_names(run_id).items():
            existing = metadata.topics.get(name)
            if existing is None or existing.error is not None:
                continue        # not created yet: the bus creates it at boot
            have = len(existing.partitions)
            if have < wanted.get(lane, 1):
                return (
                    f"lane topic {name!r} already exists with {have} partition(s) but this "
                    f"run needs {wanted.get(lane, 1)}; a run on a shrunken lane topic loses "
                    "records silently (delete the topic while nothing is publishing)"
                )
    except Exception:
        logger.debug("lane-topic partition preflight skipped", exc_info=True)
    return None


def assert_simulation_dependencies(run_id: str | None = None) -> None:
    """Fail fast with a clear message if agent workers cannot run.

    TRANSPORT-AWARE, and that is not cosmetic. The RabbitMQ check below used to
    be unconditional and there was no Kafka check of any kind, so the dependency
    gating was exactly inverted for the cutover: the substrate being deleted was
    a hard gate, while the new critical-path substrate had none —
    ``kafka_utils.bootstrap_kafka_for_run`` explicitly warns-and-continues when
    the broker is unreachable. The first run after RabbitMQ was removed would
    have aborted here pointing the operator at a service they had deliberately
    deleted, and a run whose Kafka was wrong would have started regardless.

    So: Rabbit is gated on the celery broker URL actually being an ``amqp://``
    one (it still is, so the check still runs — the two halves of the cutover
    sequence independently), and a non-paho agent transport adds a REAL Kafka
    probe. Note what this cannot catch, for the same reason the OSRM probe
    cannot (§6.21): a broker that is healthy at launch and wedges 30 s in. The
    transport's own counters are the detector for that.
    """
    missing: list[str] = []

    broker_url = _celery_broker_url()
    if broker_url.startswith("amqp"):
        if not _tcp_open("127.0.0.1", 5672):
            missing.append(
                "RabbitMQ AMQP (127.0.0.1:5672) is not reachable, and CELERY_BROKER_URL "
                f"is {broker_url.split('@')[-1]!r} — every agent is LAUNCHED over that "
                "broker, so no agent can start"
            )
    else:
        logger.info(
            "Preflight: celery broker is %r, not AMQP — skipping the RabbitMQ check.",
            broker_url.split("://")[0],
        )

    transport = _agent_transport_name()
    if transport != "paho":
        try:
            from apps.config import messenger_backend

            bootstrap = messenger_backend.get("KAFKA_BOOTSTRAP_SERVERS")
        except Exception:
            bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
        if not bootstrap:
            missing.append(
                f"AGENT_TRANSPORT is {transport!r} but no KAFKA_BOOTSTRAP_SERVERS is "
                "configured, so no agent can reach the step barrier"
            )
        else:
            reason = _kafka_serving(bootstrap, run_id, _KAFKA_PROBE_TIMEOUT)
            if reason:
                missing.append(
                    f"Kafka ({bootstrap}) is not usable for AGENT_TRANSPORT={transport}: "
                    f"{reason}. Every agent reply and every step broadcast rides it"
                )

    if not _tcp_open("127.0.0.1", 11654):
        missing.append("OpenRide API gateway (127.0.0.1:11654) is not reachable")

    # Routing is a HARD dependency whenever routes are planned at assignment: without it
    # every leg silently falls back to haversine and the run's distance KPIs are ~50% low
    # while still reporting success. See ``_osrm_serving``.
    try:
        from apps.config import settings as _app_settings

        routing_server = _app_settings.get("ROUTING_SERVER", "http://localhost:10001")
    except Exception:
        routing_server = "http://localhost:10001"
    if not _osrm_serving(routing_server):
        missing.append(
            f"OSRM routing ({routing_server}) is not answering route requests "
            f"within {_OSRM_PROBE_TIMEOUT:g}s (the port may be open but the engine wedged; "
            f"restart it, then re-check)"
        )

    if missing:
        raise RuntimeError(
            "Simulation dependencies not ready:\n- "
            + "\n- ".join(missing)
            + "\nStart backend services (celery's broker, nginx, celery) before running "
            "the simulation."
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

    logger.info(
        "Preflight OK: celery broker (%s), API gateway, OSRM and a live Celery worker are "
        "reachable; agent transport is %s.",
        _celery_broker_url().split("://")[0], _agent_transport_name(),
    )
