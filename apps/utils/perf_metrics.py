"""
Performance instrumentation helpers for simulation workers and Kafka perf_stream.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _proc_cpu_times_ticks() -> Optional[int]:
    """Linux /proc/self/stat utime+stime when psutil is unavailable."""
    try:
        with open("/proc/self/stat", "r", encoding="utf-8") as fh:
            parts = fh.read().split()
        if len(parts) < 15:
            return None
        return int(parts[13]) + int(parts[14])
    except OSError:
        return None


def _proc_rss_bytes() -> Optional[int]:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


class _ProcessCpuSampler:
    """Delta-based CPU % — psutil's first cpu_percent() call always returns 0."""

    def __init__(self) -> None:
        self._proc = psutil.Process(os.getpid()) if psutil else None
        self._last_wall = time.perf_counter()
        self._last_cpu = None
        self._last_ticks: Optional[int] = _proc_cpu_times_ticks()
        if self._proc is not None:
            self._last_cpu = self._proc.cpu_times()
            self._proc.cpu_percent(interval=None)  # prime

    def sample(self) -> Dict[str, Any]:
        now = time.perf_counter()
        wall = max(now - self._last_wall, 1e-6)
        cpu_pct = 0.0
        rss_mb: Optional[float] = None

        if self._proc is not None:
            cpu_times = self._proc.cpu_times()
            mem = self._proc.memory_info()
            rss_mb = mem.rss / (1024 * 1024)
            if self._last_cpu is not None:
                user_delta = (cpu_times.user - self._last_cpu.user) + (
                    cpu_times.system - self._last_cpu.system
                )
                cpu_pct = min(999.0, (user_delta / wall) * 100.0)
            else:
                cpu_pct = self._proc.cpu_percent(interval=None)
            self._last_cpu = cpu_times
        else:
            ticks = _proc_cpu_times_ticks()
            rss = _proc_rss_bytes()
            if rss is not None:
                rss_mb = rss / (1024 * 1024)
            if ticks is not None and self._last_ticks is not None:
                delta_ticks = ticks - self._last_ticks
                cpu_pct = min(999.0, (delta_ticks / _CLK_TCK / wall) * 100.0)
            self._last_ticks = ticks

        self._last_wall = now

        sys_cpu: Optional[float] = None
        if psutil is not None:
            try:
                sys_cpu = psutil.cpu_percent(interval=None)
            except Exception:
                pass

        out: Dict[str, Any] = {
            "cpu_percent": round(cpu_pct, 2),
            "rss_mb": round(rss_mb, 2) if rss_mb is not None else None,
            "pid": os.getpid(),
        }
        if sys_cpu is not None:
            out["system_cpu_percent"] = round(sys_cpu, 2)
        return {k: v for k, v in out.items() if v is not None}


SCHEDULER_LABELS: Dict[str, str] = {
    "agent": "Agent workers (Celery)",
    "service": "Service workers (assignment/analytics)",
    "agents": "Agent workers (Celery)",
    "services": "Service workers (assignment/analytics)",
}

_cpu_sampler: Optional[_ProcessCpuSampler] = None


def _get_cpu_sampler() -> _ProcessCpuSampler:
    global _cpu_sampler
    if _cpu_sampler is None:
        _cpu_sampler = _ProcessCpuSampler()
    return _cpu_sampler


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_metrics() -> Dict[str, Any]:
    """CPU and memory snapshot for the current process."""
    return _get_cpu_sampler().sample()


def build_bottlenecks(
    step_wall_ms: float,
    schedulers: Dict[str, Any],
    *,
    api_ms: float = 0.0,
) -> List[Dict[str, Any]]:
    """Rank where wall time was spent during a simulation step."""
    items: List[Dict[str, Any]] = []
    accounted = 0.0

    for key, data in schedulers.items():
        if not isinstance(data, dict):
            continue
        run_ms = float(data.get("run_ms") or 0)
        stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
        accounted += run_ms
        waiting = stat.get("waiting")
        booting = stat.get("booting")
        total = stat.get("total_agents")
        detail_parts: List[str] = []
        if total is not None:
            detail_parts.append(f"{total} agents")
        if booting is not None and int(booting) > 0:
            detail_parts.append(f"{booting} booting")
        if waiting is not None and int(waiting) > 0:
            detail_parts.append(f"{waiting} waiting")
        stepping = stat.get("stepping_agents")
        if stepping is not None:
            detail_parts.append(f"{stepping} stepped")
        items.append(
            {
                "component": key,
                "label": SCHEDULER_LABELS.get(key, key.replace("_", " ").title()),
                "ms": round(run_ms, 2),
                "pct": round(100.0 * run_ms / max(step_wall_ms, 1), 1),
                "detail": ", ".join(detail_parts) if detail_parts else None,
            }
        )

    if api_ms > 0:
        accounted += api_ms
        items.append(
            {
                "component": "api_mongo",
                "label": "MongoDB / API (run-config PATCH)",
                "ms": round(api_ms, 2),
                "pct": round(100.0 * api_ms / max(step_wall_ms, 1), 1),
                "detail": "Eve → MongoDB",
            }
        )

    other_ms = max(0.0, step_wall_ms - accounted)
    if other_ms >= 1.0:
        items.append(
            {
                "component": "other",
                "label": "Orchestration / other",
                "ms": round(other_ms, 2),
                "pct": round(100.0 * other_ms / max(step_wall_ms, 1), 1),
                "detail": "Sim loop overhead",
            }
        )

    items.sort(key=lambda x: x["ms"], reverse=True)
    return items


def build_perf_payload(
    run_id: str,
    perf_type: str,
    metrics: Dict[str, Any],
    *,
    sim_step: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "run_id": run_id,
        "ts": utc_now_iso(),
        "type": perf_type,
        "metrics": metrics,
    }
    if sim_step is not None:
        payload["sim_step"] = sim_step
    if extra:
        payload.update(extra)
    return payload


def _normalize_agent_run_time_ms(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Agents typically report seconds; values above 500 are treated as ms.
    if value <= 500:
        return round(value * 1000.0, 2)
    return round(value, 2)


def collect_slow_agents(
    schedulers: Dict[str, Any],
    step_index: int,
    *,
    top_n: int = 10,
) -> List[Dict[str, Any]]:
    """Rank agents by reported run_time for a completed simulation step."""
    ranked: List[Dict[str, Any]] = []
    for scheduler_key, scheduler in schedulers.items():
        collection = getattr(scheduler, "agent_collection", None)
        if not isinstance(collection, dict):
            continue
        stat_step = getattr(scheduler, "time", step_index + 1) - 1
        for agent_id, item in collection.items():
            if not isinstance(item, dict):
                continue
            responses = item.get("step_response")
            if not isinstance(responses, dict):
                continue
            resp = responses.get(stat_step) or responses.get(step_index)
            if not isinstance(resp, dict):
                continue
            run_time_ms = _normalize_agent_run_time_ms(resp.get("run_time"))
            if run_time_ms is None:
                continue
            ranked.append(
                {
                    "scheduler": scheduler_key,
                    "agent_id": str(agent_id),
                    "run_time_ms": run_time_ms,
                }
            )
    ranked.sort(key=lambda row: row["run_time_ms"], reverse=True)
    return ranked[: max(1, top_n)]


def build_step_spans(
    scheduler_ms: Dict[str, Any],
    *,
    api_ms: float = 0.0,
    spawn_ms: float = 0.0,
) -> List[Dict[str, Any]]:
    """Named timing spans for a simulation step (orchestrator-local view)."""
    spans: List[Dict[str, Any]] = []
    if spawn_ms > 0:
        spans.append({"name": "spawn_agents", "ms": round(spawn_ms, 2)})
    for key, raw_ms in scheduler_ms.items():
        if raw_ms is None:
            continue
        spans.append({"name": f"scheduler.{key}", "ms": round(float(raw_ms), 2)})
    if api_ms > 0:
        spans.append({"name": "mongo_patch", "ms": round(api_ms, 2)})
    spans.sort(key=lambda row: row["ms"], reverse=True)
    return spans


def publish_perf(
    run_id: str,
    perf_type: str,
    metrics: Dict[str, Any],
    *,
    sim_step: Optional[int] = None,
    include_process: bool = False,
    flush: bool = False,
) -> None:
    """Publish a perf event to Kafka perf_stream (no-op if Kafka unavailable)."""
    if include_process:
        proc = process_metrics()
        metrics = {**metrics, **{k: v for k, v in proc.items() if v is not None}}
    payload = build_perf_payload(run_id, perf_type, metrics, sim_step=sim_step)
    try:
        from apps.utils import kafka_utils

        kafka_utils.push_perf_to_topic(run_id, payload)
        if flush:
            kafka_utils.flush_perf_producer(1)
    except Exception as exc:
        logger.warning("perf_stream publish failed run_id=%s type=%s: %s", run_id, perf_type, exc)


def maybe_flush_perf_producer(
    step_index: int,
    *,
    interval: int = 10,
    force: bool = False,
) -> None:
    """Batch Kafka flushes during high-frequency step_tick publishing."""
    if not force and interval > 1 and step_index % interval != 0:
        return
    try:
        from apps.utils import kafka_utils

        kafka_utils.flush_perf_producer(1)
    except Exception as exc:
        logger.debug("perf_stream flush skipped: %s", exc)


class timed_block:
    """Context manager that records wall time in milliseconds."""

    def __init__(self) -> None:
        self.start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "timed_block":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self.start) * 1000.0
