"""Publish terminal run_status (CANCELLED) when a simulation process is stopped."""

from __future__ import annotations

import signal
import sys
import threading
from typing import Callable, Optional


def install_terminal_status_publisher(
    run_id: str,
    *,
    terminal_status: str = "CANCELLED",
) -> Callable[[str], None]:
    """
    Publish a terminal run_status on SIGTERM/SIGINT so dashboards and the KPI sink
    see the run as stopped (not stuck on RUNNING forever after pkill/terminate).
    """
    from apps.utils import kafka_utils

    run_status_topic = kafka_utils.resolve_topic("run_status")
    published = threading.Event()

    def _publish_terminal(reason: str, *, status: Optional[str] = None) -> None:
        if published.is_set():
            return
        published.set()
        try:
            kafka_utils.push_run_status(
                run_status_topic,
                run_id,
                status or terminal_status,
                msg=reason,
            )
            kafka_utils.flush_producer(3)
        except Exception as exc:
            print(f"Failed to publish {status or terminal_status} run_status: {exc}", flush=True)

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        print(f"Simulation received {name}; publishing {terminal_status}.", flush=True)
        _publish_terminal(f"received {name}")
        if signum == signal.SIGINT:
            raise KeyboardInterrupt()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    return _publish_terminal
