"""Workspace paths and interpreter resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The WORKSPACE root -- the directory that CONTAINS openride_apps, openride_server,
# kafka_broker and friends -- not this repo.
#
# This read `parent.parent` until 2026-08-24, which was right while this package
# lived at /home/user/openride_control. Moving it to
# /home/user/openride_apps/openride_control silently shifted ROOT down one level
# to /home/user/openride_apps, and every derived path gained a doubled segment:
# compose dirs became /home/user/openride_apps/openride_server (absent), so
# `docker compose ps` raised FileNotFoundError, the exception was swallowed, and
# the /services page reported the entire running stack as "stopped"; and the
# simulation spawn cwd became /home/user/openride_apps/openride_apps, so launching
# a run failed before exec. Mirrors OPENRIDE_WORKSPACE_ROOT in openride/config.py.
ROOT = Path(
    os.environ.get("OPENRIDE_WORKSPACE_ROOT", str(Path(__file__).resolve().parents[2]))
)

# THIS repo, anchored to this file rather than rebuilt as `ROOT / "openride_apps"`.
# That reconstruction only resolves when the checkout is literally named
# openride_apps: in a clone named anything else the CLI shelled out with a
# PYTHONPATH pointing at a directory that does not exist, and every
# `scenario compile` died with "No module named 'openride_control'" -- 0 of 16.
REPO_ROOT = Path(__file__).resolve().parents[1]

# openride_apps/venv has simulation + Celery deps (eventlet, orsim, etc.).
# pyjupenv is only for the CLI (rich, questionary).
_PYTHON_CANDIDATES = (
    REPO_ROOT / "venv" / "bin" / "python",
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
    base = str(REPO_ROOT)
    env["PYTHONPATH"] = base + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("OPENRIDE_SERVER_URL", DEFAULT_OPENRIDE_SERVER_URL)
    env.setdefault("CELERY_BROKER_URL", "amqp://guest:guest@127.0.0.1:5672//")
    env.setdefault("CELERY_CONCURRENCY", celery_concurrency())
    env.setdefault("CELERY_WORKER_COUNT", str(celery_worker_count()))
    return env
