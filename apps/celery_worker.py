"""Celery worker entry with resilient RabbitMQ connection settings.

Tasks are registered on ``orsim.worker.app``; we re-export that instance after
applying stronger broker reconnect settings.
"""

from __future__ import annotations

import os

from orsim.celery_config import CeleryConfig
from orsim.worker import app

_DEFAULT_BROKER = "amqp://guest:guest@127.0.0.1:5672//"


class ResilientCeleryConfig(CeleryConfig):
    broker_url = os.environ.get("CELERY_BROKER_URL", _DEFAULT_BROKER)
    broker_heartbeat = 30
    broker_connection_timeout = 30
    broker_connection_retry = True
    broker_connection_retry_on_startup = True
    broker_connection_max_retries = None
    # Keep a small pool so eventlet concurrency does not open hundreds of AMQP sockets.
    broker_pool_limit = 4
    worker_cancel_long_running_tasks_on_connection_loss = True
    worker_prefetch_multiplier = 1
    task_acks_late = True
    task_reject_on_worker_lost = True


app.config_from_object(ResilientCeleryConfig)

import orsim.tasks  # noqa: F401, E402
