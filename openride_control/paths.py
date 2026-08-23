"""Workspace paths and interpreter resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# openride_apps/venv has simulation + Celery deps (eventlet, orsim, etc.).
# pyjupenv is only for the CLI (rich, questionary).
_PYTHON_CANDIDATES = (
    ROOT / "openride_apps" / "venv" / "bin" / "python",
    ROOT / "venv" / "bin" / "python",
    ROOT / "pyjupenv" / "bin" / "python",
)


def resolve_python() -> str:
    for candidate in _PYTHON_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


VENV_PYTHON = resolve_python()
# Eventlet greenlets are cheap; 64 keeps step-0 bootstrap of 500+ agents under
# ~45s wall while staying well within RabbitMQ's nofile=65536 budget. 128+ used
# to exhaust file descriptors before docker-compose raised the ulimit; override
# via CELERY_CONCURRENCY env if you need to push further.
DEFAULT_CELERY_CONCURRENCY = "128"
# We now run multiple Celery worker processes in parallel (each with its own
# eventlet pool). One eventlet worker is bottlenecked by the Python GIL — a
# single process can only execute Python bytecode on one core at a time. More
# processes spread CPU work across cores. Override with CELERY_WORKER_COUNT.
DEFAULT_CELERY_WORKER_COUNT = "16"
DEFAULT_OPENRIDE_SERVER_URL = "http://127.0.0.1:11654"
# Python module for the simulation process (ecosystem/domain is configured inside the runner).
SIMULATION_RUN_MODULE = os.environ.get(
    "OPENRIDE_SIMULATION_MODULE",
    "apps.simulation.run_container_logistics_simulation",
)
SIMULATION_LOG_FILE = ROOT / "simulation_log.txt"


def celery_concurrency() -> str:
    raw = os.environ.get("CELERY_CONCURRENCY", DEFAULT_CELERY_CONCURRENCY).strip()
    if raw.isdigit() and int(raw) > 0:
        return raw
    return DEFAULT_CELERY_CONCURRENCY


def celery_worker_count() -> int:
    raw = os.environ.get("CELERY_WORKER_COUNT", DEFAULT_CELERY_WORKER_COUNT).strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return int(DEFAULT_CELERY_WORKER_COUNT)


def openride_apps_env() -> dict[str, str]:
    env = os.environ.copy()
    base = str(ROOT / "openride_apps")
    env["PYTHONPATH"] = base + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("OPENRIDE_SERVER_URL", DEFAULT_OPENRIDE_SERVER_URL)
    env.setdefault("CELERY_BROKER_URL", "amqp://guest:guest@127.0.0.1:5672//")
    env.setdefault("CELERY_CONCURRENCY", celery_concurrency())
    env.setdefault("CELERY_WORKER_COUNT", str(celery_worker_count()))
    return env
