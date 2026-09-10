"""Opt-in, per-process phase timer for agent-side hot paths.

Agents run inside long-lived Celery workers, so `py-spy` cannot reach them on
this box (`/proc/sys/kernel/yama/ptrace_scope == 1`, no sudo) and the scheduler
only sees an agent's total `run_time`. This gives the missing middle: named
spans accumulated per worker process and dumped to `celery_log.txt`.

Disabled unless ``OPENRIDE_STEP_PROFILE=1``, in which case ``span()`` costs two
``perf_counter()`` calls and a dict update. When disabled it returns a shared
no-op context manager, so leaving the call sites in place is free.

    from apps.utils.step_profile import span, tick

    with span("facility.tick"):
        ...
    tick("facility.tick")          # dumps the table every DUMP_EVERY ticks
"""

import logging
import os
import time
from collections import defaultdict
from contextlib import contextmanager

ENABLED = os.environ.get("OPENRIDE_STEP_PROFILE", "") == "1"
DUMP_EVERY = int(os.environ.get("OPENRIDE_STEP_PROFILE_EVERY", "200"))

_total: dict = defaultdict(float)
_count: dict = defaultdict(int)
_ticks: dict = defaultdict(int)


class _Noop:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NOOP = _Noop()


@contextmanager
def _timed(name: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _total[name] += time.perf_counter() - t0
        _count[name] += 1


def span(name: str):
    """Time a named phase (no-op unless OPENRIDE_STEP_PROFILE=1)."""
    if not ENABLED:
        return _NOOP
    return _timed(name)


def bump(name: str, seconds: float = 0.0) -> None:
    """Record a phase whose duration was measured by the caller."""
    if not ENABLED:
        return
    _total[name] += seconds
    _count[name] += 1


def tick(name: str) -> None:
    """Count one unit of work; dump the accumulated table every DUMP_EVERY."""
    if not ENABLED:
        return
    _ticks[name] += 1
    if _ticks[name] % DUMP_EVERY:
        return
    rows = sorted(_total.items(), key=lambda kv: -kv[1])
    parts = [
        f"{k}={_total[k] * 1000:.0f}ms/n{_count[k]}({_total[k] / max(1, _count[k]) * 1000:.2f}ms)"
        for k, _ in rows
    ]
    # celery runs at --loglevel WARNING, so an info() dump would never be emitted.
    logging.warning(
        "step_profile pid=%s after %d %s: %s",
        os.getpid(),
        _ticks[name],
        name,
        " ".join(parts),
    )
