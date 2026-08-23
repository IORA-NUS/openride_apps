"""Service registry and readiness probes."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from openride_control.paths import (
    ROOT,
    SIMULATION_LOG_FILE,
    SIMULATION_RUN_MODULE,
    VENV_PYTHON,
    celery_concurrency,
    celery_worker_count,
    openride_apps_env,
)

if TYPE_CHECKING:
    from openride_control.manager import ServiceManager

log = logging.getLogger(__name__)


@dataclass
class ServiceSpec:
    key: str
    label: str
    kind: str  # "compose" | "process"
    compose_dir: Path | None = None
    compose_service: str | None = None
    dependencies: tuple[str, ...] = ()
    dependents: tuple[str, ...] = ()
    log_file: Path | None = None
    description: str = ""
    readiness: Callable[["ServiceManager", "ServiceSpec"], None] | None = None
    start_command: Callable[["ServiceManager", "ServiceSpec"], subprocess.Popen] | None = None
    stop_extra: Callable[["ServiceManager", "ServiceSpec"], None] | None = None


def run_compose_ps(compose_dir: Path) -> set[str]:
    result = subprocess.run(
        ["docker", "compose", "ps", "--services", "--status", "running"],
        cwd=compose_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"docker compose ps failed in {compose_dir}: {stderr}")
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def wait_for_port(name: str, host: str, port: int, timeout: float = 120, interval: float = 2) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except OSError:
            log.info("Waiting for %s on %s:%s", name, host, port)
            time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {name} on {host}:{port}")


def wait_for_http(name: str, url: str, timeout: float = 120, interval: float = 2) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return
        except urllib.error.HTTPError as ex:
            if ex.code < 500:
                return
            log.info("%s HTTP %s at %s", name, ex.code, url)
        except Exception as ex:
            log.info("%s: %s", name, ex)
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {name} at {url}")


def wait_for_compose_service(
    compose_dir: Path, service: str, timeout: float = 180, interval: float = 3
) -> None:
    deadline = time.time() + timeout
    last_seen: set[str] = set()
    while time.time() < deadline:
        try:
            running = run_compose_ps(compose_dir)
            last_seen = running
            if service in running:
                return
        except Exception as ex:
            log.info("%s: %s", compose_dir.name, ex)
        if service not in last_seen:
            log.info("Waiting for compose service %r in %s", service, compose_dir.name)
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for compose service {service!r} in {compose_dir}")


def _inprocess_celery_ping(timeout: float) -> bool | None:
    """Fast, in-process AMQP liveness ping that early-returns on the FIRST worker reply.

    The CLI ``celery inspect ping`` always blocks for the whole ``--timeout`` window
    (it broadcasts and waits to collect replies from *every* worker) plus subprocess
    import + broker connect — ~10s here for a 32-worker pool. ``app.control.ping`` with
    ``limit=1`` returns the instant one worker answers (~80ms measured), so the common
    "workers are healthy" path costs milliseconds instead of seconds.

    Returns:
      * ``True``  — at least one worker replied (definitively alive).
      * ``None``  — inconclusive (import/broker unavailable, or no reply within the
        short window). The caller should NOT treat this as "dead": fall back to the
        authoritative subprocess ping before concluding anything. This is what keeps a
        momentarily-busy worker pool (e.g. tearing down a previous run's agents) from
        triggering a needless restart.
    """
    try:
        from apps.celery_worker import app  # type: ignore
    except Exception:
        return None
    try:
        # limit=1 → return as soon as one worker answers; cap the wait so a genuinely
        # unresponsive pool falls through to the patient path quickly.
        replies = app.control.ping(timeout=max(0.5, min(timeout, 2.0)), limit=1)
    except Exception:
        return None
    return True if replies else None


def _celery_ping_subprocess(timeout: float) -> bool:
    inspect_timeout = int(max(1, timeout))
    try:
        result = subprocess.run(
            [VENV_PYTHON, "-m", "celery", "-A", "apps.celery_worker", "inspect", "ping", f"--timeout={inspect_timeout}"],
            cwd=ROOT / "openride_apps",
            env=openride_apps_env(),
            capture_output=True,
            text=True,
            # Hard wall-clock cap. Celery's --timeout only bounds how long it waits for
            # worker *replies*; it does NOT bound broker connect / pidbox reply-queue
            # declaration. On an overloaded broker that setup can block indefinitely, which
            # otherwise hangs probe_state -> refresh_states -> every Services-tab command
            # (status / start-backend). Give a few seconds of headroom over --timeout, then
            # treat a blown cap as "not responding".
            timeout=inspect_timeout + 5,
        )
    except subprocess.TimeoutExpired:
        log.warning("celery inspect ping exceeded %ss hard cap; treating as unresponsive", inspect_timeout + 5)
        return False
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
    return result.returncode == 0 and "pong" in output


def celery_ping(timeout: float = 10.0) -> bool:
    """True when at least one Celery worker is responsive.

    Tries the fast in-process ping first (~80ms happy path); only when that is
    inconclusive does it pay for the authoritative — but slow — subprocess ping.
    """
    fast = _inprocess_celery_ping(timeout)
    if fast:
        return True
    return _celery_ping_subprocess(timeout)


def celery_python_worker_pids() -> list[int]:
    """Return PIDs of actual Python Celery workers (exclude bash launcher scripts)."""
    patterns = (
        "celery -A apps.celery_worker worker",
        "celery -A orsim.worker worker",
    )
    pids: set[int] = set()
    for pattern in patterns:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
            if comm.startswith("python"):
                pids.add(pid)
    return sorted(pids)


def celery_python_workers_running() -> bool:
    return bool(celery_python_worker_pids())


def celery_is_healthy(*, ping_timeout: float = 2.0) -> bool:
    """True when Python workers exist and respond to inspect ping."""
    if not celery_python_workers_running():
        return False
    return celery_ping(timeout=ping_timeout)


def wait_for_celery(timeout: float = 180, interval: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if celery_ping(timeout=5):
            return
        log.info("Waiting for Celery worker ping")
        time.sleep(interval)
    raise TimeoutError("Timed out waiting for Celery worker (inspect ping).")


DATAPLANE_UNIT = "openride-dataplane.service"
# Short on purpose. A hung dataplane (port bound, nothing answering — the window a
# `systemctl restart` opens, or a read landing while the store lock is held) must not
# block the Services page, which probes this on every refresh. Matches the 2 s
# `AbortSignal.timeout` the frontend already uses in `lib/dataplaneSource.ts`.
DATAPLANE_HEALTH_TIMEOUT = 2.0


def dataplane_health_url() -> str:
    """`/health` URL, honouring the same DATAPLANE_HTTP_PORT the unit reads."""
    port = (os.environ.get("DATAPLANE_HTTP_PORT") or "").strip() or "8620"
    return f"http://127.0.0.1:{port}/health"


def dataplane_health(
    *, url: str | None = None, timeout: float = DATAPLANE_HEALTH_TIMEOUT
) -> tuple[str, str]:
    """Three-state readiness probe for `apps/dataplane`.

    Unlike every other service here, "the process exists" is not the useful
    question: the 2026-07-01 outage (see docs/reference/features/18-dataplane.md)
    was five weeks of silent data loss behind a systemd unit that reported
    `active (running)` the whole time. `/health` reports per-task liveness, so we
    read it rather than the unit state.

    Returns ``(state, detail)`` where state is one of:
      * ``"down"``     — connection refused, timeout, non-200, or unparseable body.
      * ``"degraded"`` — reachable, but ``ok`` is false, ``degraded[]`` is non-empty,
        or some supervised task reports ``healthy: false``.
      * ``"healthy"``  — reachable and reporting nothing wrong.
    """
    target = url or dataplane_health_url()
    try:
        with urllib.request.urlopen(target, timeout=timeout) as response:
            if response.status != 200:
                return "down", f"/health returned HTTP {response.status}"
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        return "down", f"/health returned HTTP {ex.code}"
    except Exception as ex:
        return "down", f"/health unreachable: {type(ex).__name__}: {ex}"

    if not isinstance(payload, dict):
        return "down", "/health returned a non-object payload"

    reasons: list[str] = []
    degraded = payload.get("degraded")
    if isinstance(degraded, list) and degraded:
        reasons.append("degraded: " + ", ".join(str(item) for item in degraded))
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        sick = [
            str(task.get("name") or "?")
            for task in tasks
            if isinstance(task, dict) and not task.get("healthy", True)
        ]
        if sick:
            reasons.append("unhealthy tasks: " + ", ".join(sick))
    if payload.get("ok") is not True:
        reasons.append("reported ok=false")
    if reasons:
        return "degraded", "; ".join(reasons)
    return "healthy", ""


def wait_for_dataplane(timeout: float = 120, interval: float = 2) -> None:
    deadline = time.time() + timeout
    detail = "no probe attempted"
    while time.time() < deadline:
        state, detail = dataplane_health()
        if state == "healthy":
            return
        log.info("Waiting for dataplane (%s): %s", state, detail)
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for dataplane at {dataplane_health_url()} ({detail})")


def _ready_dataplane(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_dataplane()


def _start_dataplane(_mgr: ServiceManager, spec: ServiceSpec) -> subprocess.Popen:
    """Start the systemd user unit, then hand back a live journal follower.

    The dataplane binds :8620 and is supervised by systemd with `Restart=always`,
    so spawning a second copy directly (the way `_start_celery` does) would just
    lose a port race. `systemctl start` returns immediately though, and
    `ServiceManager.start` treats a start_command whose process has already
    exited as an immediate crash — so the Popen we return is a `journalctl -f`
    that both stays alive for the unit's lifetime and gives the log watchdog and
    `tail_log_lines` a real file to read.
    """
    log_path = spec.log_file or ROOT / "dataplane_log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["systemctl", "--user", "start", DATAPLANE_UNIT],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or f"systemctl --user start {DATAPLANE_UNIT} failed").strip()
        )
    log_file = open(log_path, "w")
    return subprocess.Popen(
        ["journalctl", "--user", "-u", DATAPLANE_UNIT, "-f", "-n", "200", "-o", "cat"],
        start_new_session=True,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )


def _stop_dataplane_extra(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    subprocess.run(["systemctl", "--user", "stop", DATAPLANE_UNIT], check=False)


def _ready_kafka(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "kafka_broker", "kafka")
    wait_for_port("Kafka broker", "127.0.0.1", 9092)


def _ready_mongodb(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_server", "mongodb")
    wait_for_port("MongoDB", "127.0.0.1", 27017)


def _ready_api(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_server", "api")


def _ready_analytics(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_server", "analytics")
    wait_for_port("Open Ride analytics", "127.0.0.1", 11655)


def _ready_nginx(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_server", "nginx")
    wait_for_port("Open Ride API gateway", "127.0.0.1", 11654)
    wait_for_http("Nginx status", "http://127.0.0.1:11654/nginx_status")


def _ready_osrm(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_server", "osrm-routed")
    wait_for_port("OSRM routed", "127.0.0.1", 10001)


def _ready_victoriametrics(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_server", "victoriametrics")
    wait_for_port("VictoriaMetrics", "127.0.0.1", 8428)
    wait_for_http("VictoriaMetrics", "http://127.0.0.1:8428/health")


def _ready_perf_vm_sink(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_server", "perf-vm-sink")


def _ready_rabbit(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_compose_service(ROOT / "openride_apps", "rabbit")
    wait_for_port("RabbitMQ AMQP", "127.0.0.1", 5672)
    wait_for_port("RabbitMQ management", "127.0.0.1", 15672)
    wait_for_http("RabbitMQ management API", "http://127.0.0.1:15672")


def _ready_celery(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    wait_for_celery()


def _start_celery(_mgr: ServiceManager, spec: ServiceSpec) -> subprocess.Popen:
    log_path = spec.log_file or ROOT / "celery_log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = openride_apps_env()
    concurrency = celery_concurrency()
    worker_count = celery_worker_count()
    # We fan out N independent Celery worker processes, each with its own
    # eventlet pool, so the Python GIL is no longer a single-core ceiling on
    # the agent_scheduler step. RabbitMQ round-robins tasks across them. The
    # parent bash process owns the process group, traps SIGTERM/SIGINT, and
    # kills all children before exiting so `pkill -f celery -A apps.celery_worker`
    # (in _stop_celery_extra) and Popen.terminate() both work cleanly.
    worker_lines = "\n".join(
        f'  "{VENV_PYTHON}" -m celery -A apps.celery_worker worker '
        f"--without-gossip --without-mingle --without-heartbeat "
        f"--pool eventlet --concurrency {concurrency} --loglevel WARNING "
        f'--hostname OpenRideAsyncService-{i}@$(date +%s)$$ &'
        f"\n  pids+=($!)"
        for i in range(1, worker_count + 1)
    )
    command = (
        f"cd {ROOT / 'openride_apps'} && ulimit -n 100000 && "
        "pids=();\n"
        + worker_lines
        + "\n"
        + "trap 'kill ${pids[@]} 2>/dev/null; wait' INT TERM\n"
        + "wait"
    )
    log_file = open(log_path, "w")
    return subprocess.Popen(
        ["bash", "-c", command],
        start_new_session=True,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _stop_celery_extra(_mgr: ServiceManager, _spec: ServiceSpec) -> None:
    subprocess.run(["pkill", "-f", "celery -A apps.celery_worker worker"], check=False)
    subprocess.run(["pkill", "-f", "celery -A orsim.worker worker"], check=False)
    subprocess.run(["systemctl", "--user", "stop", "openride-celery.service"], check=False)


def spawn_simulation_process(
    *,
    extra_env: dict[str, str] | None = None,
    background: bool = False,
) -> subprocess.Popen:
    """Launch the simulation subprocess (not a managed infrastructure service)."""
    SIMULATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = openride_apps_env()
    if extra_env:
        env.update(extra_env)

    cmd = [VENV_PYTHON, "-m", SIMULATION_RUN_MODULE]

    if background:
        # Write logs directly to file — avoid PIPE + shell, which caused Broken pipe crashes.
        log_handle = open(SIMULATION_LOG_FILE, "w", encoding="utf-8")
        log_handle.write(f"--- simulation start @ {datetime.now().isoformat()} ---\n")
        # Record the injected run id in the fresh log immediately so log-based discovery
        # resolves the new run before the runtime's own first log line — this is what the
        # dashboard's current-run probe reads while the process is still initialising.
        injected_run_id = env.get("ORSIM_RUN_ID", "").strip()
        if injected_run_id:
            log_handle.write(f"run_id={injected_run_id}\n")
        log_handle.flush()
        return subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=ROOT / "openride_apps",
            env=env,
        )

    return subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=ROOT / "openride_apps",
        text=True,
        bufsize=1,
        env=env,
    )


def simulation_process_pgrep_running() -> bool:
    """True when a detached simulation module process is still on the host."""
    result = subprocess.run(
        ["pgrep", "-f", f"[p]ython -m {SIMULATION_RUN_MODULE}"],
        capture_output=True,
    )
    return result.returncode == 0


def stop_simulation_process(*, wait_timeout: float = 20.0) -> bool:
    """SIGTERM simulation processes, wait, then SIGKILL. Returns True when none remain."""
    pattern = f"[p]ython -m {SIMULATION_RUN_MODULE}"
    subprocess.run(["pkill", "-f", pattern], check=False)
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if not simulation_process_pgrep_running():
            return True
        time.sleep(0.25)
    subprocess.run(["pkill", "-9", "-f", pattern], check=False)
    time.sleep(0.5)
    return not simulation_process_pgrep_running()


SERVICES: dict[str, ServiceSpec] = {
    "kafka": ServiceSpec(
        key="kafka",
        label="Kafka broker",
        kind="compose",
        compose_dir=ROOT / "kafka_broker",
        compose_service="kafka",
        log_file=ROOT / "kafka_log.txt",
        description="Message broker (port 9092)",
        readiness=_ready_kafka,
    ),
    "mongodb": ServiceSpec(
        key="mongodb",
        label="MongoDB",
        kind="compose",
        compose_dir=ROOT / "openride_server",
        compose_service="mongodb",
        log_file=ROOT / "server_log.txt",
        description="Primary database (port 27017)",
        readiness=_ready_mongodb,
        dependents=("api", "analytics", "nginx"),
    ),
    "api": ServiceSpec(
        key="api",
        label="OpenRide API",
        kind="compose",
        compose_dir=ROOT / "openride_server",
        compose_service="api",
        dependencies=("mongodb",),
        log_file=ROOT / "server_log.txt",
        description="Platform API (behind nginx)",
        readiness=_ready_api,
        dependents=("nginx",),
    ),
    "analytics": ServiceSpec(
        key="analytics",
        label="Analytics",
        kind="compose",
        compose_dir=ROOT / "openride_server",
        compose_service="analytics",
        dependencies=("mongodb",),
        log_file=ROOT / "server_log.txt",
        description="Analytics service (port 11655)",
        readiness=_ready_analytics,
    ),
    "dataplane": ServiceSpec(
        key="dataplane",
        label="Dataplane",
        kind="process",
        dependencies=("kafka", "mongodb"),
        log_file=ROOT / "dataplane_log.txt",
        description="Stream ingest + DuckDB store + read API (port 8620)",
        readiness=_ready_dataplane,
        start_command=_start_dataplane,
        stop_extra=_stop_dataplane_extra,
    ),
    "victoriametrics": ServiceSpec(
        key="victoriametrics",
        label="VictoriaMetrics",
        kind="compose",
        compose_dir=ROOT / "openride_server",
        compose_service="victoriametrics",
        dependencies=("kafka",),
        log_file=ROOT / "server_log.txt",
        description="Perf metrics TSDB (port 8428)",
        readiness=_ready_victoriametrics,
        dependents=("perf_vm_sink",),
    ),
    "perf_vm_sink": ServiceSpec(
        key="perf_vm_sink",
        label="Perf VM sink",
        kind="compose",
        compose_dir=ROOT / "openride_server",
        compose_service="perf-vm-sink",
        dependencies=("kafka", "victoriametrics"),
        log_file=ROOT / "server_log.txt",
        description="Kafka perf_stream → VictoriaMetrics",
        readiness=_ready_perf_vm_sink,
    ),
    "nginx": ServiceSpec(
        key="nginx",
        label="Nginx gateway",
        kind="compose",
        compose_dir=ROOT / "openride_server",
        compose_service="nginx",
        dependencies=("api",),
        log_file=ROOT / "server_log.txt",
        description="HTTP gateway (port 11654)",
        readiness=_ready_nginx,
        dependents=("celery",),
    ),
    "osrm": ServiceSpec(
        key="osrm",
        label="OSRM routing",
        kind="compose",
        compose_dir=ROOT / "openride_server",
        compose_service="osrm-routed",
        log_file=ROOT / "server_log.txt",
        description="Road routing engine (port 10001)",
        readiness=_ready_osrm,
    ),
    "rabbit": ServiceSpec(
        key="rabbit",
        label="RabbitMQ",
        kind="compose",
        compose_dir=ROOT / "openride_apps",
        compose_service="rabbit",
        log_file=ROOT / "rabbitmq_log.txt",
        description="Task queue (ports 5672, 15672)",
        readiness=_ready_rabbit,
        dependents=("celery",),
    ),
    "celery": ServiceSpec(
        key="celery",
        label="Celery workers",
        kind="process",
        dependencies=("rabbit", "nginx"),
        log_file=ROOT / "celery_log.txt",
        description="Async worker pool (eventlet)",
        readiness=_ready_celery,
        start_command=_start_celery,
        stop_extra=_stop_celery_extra,
    ),
    "simulation": ServiceSpec(
        key="simulation",
        label="Simulation",
        kind="process",
        dependencies=(
            "kafka",
            "mongodb",
            "api",
            "analytics",
            "nginx",
            "osrm",
            "rabbit",
            "celery",
        ),
        log_file=SIMULATION_LOG_FILE,
        description="Container logistics simulation runner",
    ),
}

BACKEND_START_ORDER: tuple[str, ...] = (
    "kafka",
    "mongodb",
    # After its two inputs, before anything that reads it (the dashboard reads only
    # from here) — mirrors the After=/Before= in openride-dataplane.service.
    "dataplane",
    "victoriametrics",
    "api",
    "analytics",
    "perf_vm_sink",
    "osrm",
    "nginx",
    "rabbit",
    "celery",
)

SERVICE_DISPLAY_ORDER: tuple[str, ...] = BACKEND_START_ORDER
