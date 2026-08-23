"""Service lifecycle orchestration (docker compose + local processes)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openride_control.models import ActionResult, ServiceSnapshot, ServiceState
from openride_control.paths import ROOT, SIMULATION_LOG_FILE
from openride_control.registry import (
    BACKEND_START_ORDER,
    SERVICE_DISPLAY_ORDER,
    SERVICES,
    ServiceSpec,
    run_compose_ps,
    spawn_simulation_process,
    stop_simulation_process,
)

SIMULATION_PROCESS_KEY = "simulation"
DATAPLANE_KEY = "dataplane"

log = logging.getLogger(__name__)


@dataclass
class ServiceManager:
    processes: dict[str, subprocess.Popen] = field(default_factory=dict)
    states: dict[str, ServiceState] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    _watchdog_started: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        for key in SERVICES:
            self.states.setdefault(key, ServiceState.STOPPED)
            self.messages.setdefault(key, "")

    def is_compose_running(self, spec: ServiceSpec, cache: dict | None = None) -> bool:
        if spec.kind != "compose" or not spec.compose_dir or not spec.compose_service:
            return False
        try:
            if cache is not None and spec.compose_dir in cache:
                running = cache[spec.compose_dir]
            else:
                running = run_compose_ps(spec.compose_dir)
                if cache is not None:
                    cache[spec.compose_dir] = running
            return spec.compose_service in running
        except Exception:
            return False

    def _build_compose_cache(self) -> dict:
        """Probe each distinct compose project once, in parallel.

        ``all_snapshots`` probes 11 services and calls ``probe_state`` twice each;
        without this, that fans out to ~16+ serial ``docker compose ps`` calls (~1s
        apiece, ~12s total) even though most services share the ``openride_server``
        project. One ``docker compose ps`` per project returns every running service
        in it, so we batch them — collapsing the status probe to ~1–2s.
        """
        dirs = {
            spec.compose_dir
            for spec in SERVICES.values()
            if spec.kind == "compose" and spec.compose_dir
        }
        cache: dict = {}
        if not dirs:
            return cache

        def probe(compose_dir):
            try:
                return compose_dir, run_compose_ps(compose_dir)
            except Exception:
                return compose_dir, set()

        with ThreadPoolExecutor(max_workers=len(dirs)) as ex:
            for compose_dir, running in ex.map(probe, dirs):
                cache[compose_dir] = running
        return cache

    def is_process_running(self, key: str) -> bool:
        proc = self.processes.get(key)
        if proc is not None and proc.poll() is None:
            return True
        if key == "celery":
            from openride_control.registry import celery_is_healthy

            return celery_is_healthy(ping_timeout=2.0)
        if key == DATAPLANE_KEY:
            # Systemd-supervised: our Popen is only a journal follower, so the
            # authoritative liveness answer is /health, not the child process.
            return self._dataplane_health()[0] != "down"
        return False

    def is_simulation_running(self) -> bool:
        proc = self.processes.get(SIMULATION_PROCESS_KEY)
        if proc is not None and proc.poll() is None:
            return True
        from openride_control.registry import simulation_process_pgrep_running

        return simulation_process_pgrep_running()

    def probe_state(self, key: str, cache: dict | None = None) -> ServiceState:
        if key == SIMULATION_PROCESS_KEY:
            return ServiceState.RUNNING if self.is_simulation_running() else ServiceState.STOPPED
        if key == "celery":
            return ServiceState.RUNNING if self._celery_healthy(cache) else ServiceState.STOPPED
        if key == DATAPLANE_KEY:
            # "degraded" is still RUNNING here — it is up, just unwell. The nuance
            # rides on ServiceSnapshot.health so the start/stop gating (which keys
            # off `state`) keeps working: you can still stop a degraded dataplane.
            health, _ = self._dataplane_health(cache)
            return ServiceState.STOPPED if health == "down" else ServiceState.RUNNING
        spec = SERVICES[key]
        if spec.kind == "compose":
            return ServiceState.RUNNING if self.is_compose_running(spec, cache) else ServiceState.STOPPED
        return ServiceState.RUNNING if self.is_process_running(key) else ServiceState.STOPPED

    def _celery_healthy(self, cache: dict | None = None) -> bool:
        """Celery liveness ping, memoized per status probe (it's hit twice per snapshot)."""
        if cache is not None and "__celery__" in cache:
            return cache["__celery__"]
        from openride_control.registry import celery_is_healthy

        healthy = celery_is_healthy(ping_timeout=2.0)
        if cache is not None:
            cache["__celery__"] = healthy
        return healthy

    def _dataplane_health(self, cache: dict | None = None) -> tuple[str, str]:
        """`(state, detail)` from the dataplane's /health, memoized per status probe.

        Same shape as `_celery_healthy`: `probe_state` and `snapshot` both need the
        answer, and a hung dataplane costs the full 2 s timeout each time.
        """
        if cache is not None and "__dataplane__" in cache:
            return cache["__dataplane__"]
        from openride_control.registry import dataplane_health

        result = dataplane_health()
        if cache is not None:
            cache["__dataplane__"] = result
        return result

    def refresh_states(self, cache: dict | None = None) -> None:
        with self._lock:
            for key in SERVICES:
                if self.states.get(key) in (ServiceState.STARTING, ServiceState.STOPPING):
                    continue
                self.states[key] = self.probe_state(key, cache)

    def running_dependents(self, key: str) -> list[str]:
        spec = SERVICES[key]
        return [dep for dep in spec.dependents if self.probe_state(dep) == ServiceState.RUNNING]

    def snapshot(self, key: str, cache: dict | None = None) -> ServiceSnapshot:
        spec = SERVICES[key]
        transitional = self.states.get(key)
        probed = self.probe_state(key, cache)

        # Do not leave STARTING/STOPPING stuck when docker/process probe already matches.
        if transitional == ServiceState.STARTING and probed == ServiceState.RUNNING:
            transitional = None
            self.states[key] = ServiceState.RUNNING
            self.messages[key] = "Running"
        elif transitional == ServiceState.STOPPING and probed == ServiceState.STOPPED:
            transitional = None
            self.states[key] = ServiceState.STOPPED
            self.messages[key] = ""

        if transitional in (ServiceState.STARTING, ServiceState.STOPPING, ServiceState.FAILED):
            state = transitional
        else:
            state = probed
            self.states[key] = state

        message = self.messages.get(key, "")
        health = "ok"
        if key == DATAPLANE_KEY and state == ServiceState.RUNNING:
            dp_state, detail = self._dataplane_health(cache)
            if dp_state == "degraded":
                health = "degraded"
                message = detail or "reporting degraded"

        return ServiceSnapshot(
            key=key,
            label=spec.label,
            description=spec.description,
            kind=spec.kind,
            state=state,
            dependencies=spec.dependencies,
            dependents=spec.dependents,
            message=message,
            health=health,
        )

    def all_snapshots(self) -> list[ServiceSnapshot]:
        # One batched compose probe shared across refresh_states + every snapshot below,
        # so all 11 services resolve from a single `docker compose ps` per project.
        cache = self._build_compose_cache()
        self.refresh_states(cache)
        keys = list(SERVICE_DISPLAY_ORDER)
        if SIMULATION_PROCESS_KEY in SERVICES and SIMULATION_PROCESS_KEY not in keys:
            keys.append(SIMULATION_PROCESS_KEY)
        return [self.snapshot(key, cache) for key in keys if key in SERVICES]

    def _start_compose(self, spec: ServiceSpec) -> None:
        assert spec.compose_dir and spec.compose_service
        log_path = spec.log_file or ROOT / f"{spec.key}_log.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- start {spec.key} @ {datetime.now().isoformat()} ---\n")
            result = subprocess.run(
                ["docker", "compose", "up", "-d", spec.compose_service],
                cwd=spec.compose_dir,
                capture_output=True,
                text=True,
            )
            log_file.write(result.stdout or "")
            log_file.write(result.stderr or "")
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "docker compose up failed").strip())

    def _stop_compose_project(self, compose_dir: Path) -> None:
        result = subprocess.run(
            ["docker", "compose", "down", "--remove-orphans"],
            cwd=compose_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "docker compose down failed").strip())

    def _stop_compose(self, spec: ServiceSpec) -> None:
        assert spec.compose_dir and spec.compose_service
        stop_result = subprocess.run(
            ["docker", "compose", "stop", "-t", "30", spec.compose_service],
            cwd=spec.compose_dir,
            capture_output=True,
            text=True,
        )
        if stop_result.returncode != 0:
            raise RuntimeError((stop_result.stderr or stop_result.stdout or "docker compose stop failed").strip())
        rm_result = subprocess.run(
            ["docker", "compose", "rm", "-f", spec.compose_service],
            cwd=spec.compose_dir,
            capture_output=True,
            text=True,
        )
        if rm_result.returncode != 0:
            raise RuntimeError((rm_result.stderr or rm_result.stdout or "docker compose rm failed").strip())
        if self.is_compose_running(spec):
            raise RuntimeError(f"{spec.label} is still running after docker compose stop")

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                proc.kill()
            proc.wait(timeout=5)

    def _stop_process(self, key: str, spec: ServiceSpec) -> None:
        proc = self.processes.pop(key, None)
        if proc:
            self._terminate_process(proc)
        if spec.stop_extra:
            spec.stop_extra(self, spec)
        if self.is_process_running(key):
            raise RuntimeError(f"{spec.label} is still running after stop")

    def start(self, key: str, *, with_dependencies: bool = True) -> ActionResult:
        if key == SIMULATION_PROCESS_KEY:
            return self.run_simulation_background()
        with self._lock:
            spec = SERVICES[key]
            self.refresh_states()

            if self.probe_state(key) == ServiceState.RUNNING:
                msg = f"{spec.label} is already running."
                self.messages[key] = msg
                return ActionResult(key=key, ok=True, state=ServiceState.RUNNING, message=msg)

            if with_dependencies:
                for dep in spec.dependencies:
                    if self.probe_state(dep) != ServiceState.RUNNING:
                        log.info("Starting dependency %s for %s", dep, key)
                        dep_result = self.start(dep, with_dependencies=True)
                        if not dep_result.ok:
                            return dep_result

            self.states[key] = ServiceState.STARTING
            self.messages[key] = "Starting…"
            log.info("Starting %s", spec.label)

            try:
                if spec.kind == "compose":
                    self._start_compose(spec)
                else:
                    if not spec.start_command:
                        raise RuntimeError(f"No start command for {key}")
                    proc = spec.start_command(self, spec)
                    self.processes[key] = proc
                    time.sleep(2)
                    if proc.poll() is not None:
                        raise RuntimeError(f"Process exited immediately (code {proc.returncode})")

                if spec.readiness:
                    spec.readiness(self, spec)

                self.states[key] = ServiceState.RUNNING
                self.messages[key] = "Running"
                self._ensure_watchdog()
                log.info("%s is ready", spec.label)
                return ActionResult(key=key, ok=True, state=ServiceState.RUNNING, message="Started")
            except Exception as ex:
                self.states[key] = ServiceState.FAILED
                self.messages[key] = str(ex)
                log.exception("Failed to start %s", spec.label)
                return ActionResult(key=key, ok=False, state=ServiceState.FAILED, message=str(ex))

    def stop(self, key: str, *, force: bool = False) -> ActionResult:
        if key == SIMULATION_PROCESS_KEY:
            return self.stop_simulation_command()
        with self._lock:
            return self._stop_one_locked(key, force=force)

    def _ensure_backend_for_simulation(self) -> ActionResult | None:
        """Start backend stack services required before a simulation run."""
        from openride_control.registry import celery_ping, _stop_celery_extra

        for key in BACKEND_START_ORDER:
            if self.probe_state(key) != ServiceState.RUNNING:
                dep_result = self.start(key, with_dependencies=False)
                if not dep_result.ok:
                    return dep_result

        # Celery may appear running (pgrep) but be dead after a RabbitMQ restart.
        # `celery_ping` now takes the fast in-process path first (~80ms when healthy) and
        # only pays the slow subprocess ping when that is inconclusive — so the common case
        # no longer costs ~8s here. Restarting the whole 32-worker pool blocks up to 180s
        # (`wait_for_celery`) and re-imports orsim+apps in every worker, so it must NOT fire
        # on a single transient miss (e.g. the pool momentarily busy tearing down the
        # previous run's agents). Confirm with a second, patient ping before restarting.
        if not celery_ping(timeout=8) and not celery_ping(timeout=10):
            log.warning("Celery not responding to two pings — restarting worker before simulation")
            spec = SERVICES["celery"]
            _stop_celery_extra(self, spec)
            self.processes.pop("celery", None)
            self.states["celery"] = ServiceState.STOPPED
            celery_result = self.start("celery", with_dependencies=True)
            if not celery_result.ok:
                return celery_result
        return None

    def stop_simulation(self) -> None:
        proc = self.processes.pop(SIMULATION_PROCESS_KEY, None)
        if proc:
            self._terminate_process(proc)
        if not stop_simulation_process():
            raise RuntimeError("Simulation is still running after stop")

    def stop_simulation_command(self) -> ActionResult:
        """Stop a background simulation run."""
        with self._lock:
            if not self.is_simulation_running():
                self.states[SIMULATION_PROCESS_KEY] = ServiceState.STOPPED
                self.messages[SIMULATION_PROCESS_KEY] = ""
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=True,
                    state=ServiceState.STOPPED,
                    message="Simulation is not running.",
                )
            self.states[SIMULATION_PROCESS_KEY] = ServiceState.STOPPING
            self.messages[SIMULATION_PROCESS_KEY] = "Stopping…"
            try:
                self.stop_simulation()
                self.states[SIMULATION_PROCESS_KEY] = ServiceState.STOPPED
                self.messages[SIMULATION_PROCESS_KEY] = ""
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=True,
                    state=ServiceState.STOPPED,
                    message="Simulation stopped.",
                )
            except Exception as ex:
                self.states[SIMULATION_PROCESS_KEY] = ServiceState.FAILED
                self.messages[SIMULATION_PROCESS_KEY] = str(ex)
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=False,
                    state=ServiceState.FAILED,
                    message=str(ex),
                )

    def _watch_simulation_process(self, proc: subprocess.Popen) -> None:
        exit_code = proc.wait()
        with self._lock:
            self.processes.pop(SIMULATION_PROCESS_KEY, None)
            if exit_code == 0:
                self.states[SIMULATION_PROCESS_KEY] = ServiceState.STOPPED
                self.messages[SIMULATION_PROCESS_KEY] = "Completed."
            else:
                self.states[SIMULATION_PROCESS_KEY] = ServiceState.FAILED
                self.messages[SIMULATION_PROCESS_KEY] = f"Exited with code {exit_code}."

    def _drain_simulation_log(self, proc: subprocess.Popen) -> None:
        exit_code = 1
        try:
            if not proc.stdout:
                exit_code = proc.wait()
                return
            SIMULATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(SIMULATION_LOG_FILE, "w", encoding="utf-8") as log_file:
                for line in proc.stdout:
                    stamped = (
                        f"[SIMULATION_LOG] [{datetime.now().isoformat(timespec='seconds')}] => {line}"
                    )
                    log_file.write(stamped)
                    log_file.flush()
            exit_code = proc.wait()
        except Exception:
            log.exception("Simulation log drain failed")
        finally:
            with self._lock:
                self.processes.pop(SIMULATION_PROCESS_KEY, None)
                if exit_code == 0:
                    self.states[SIMULATION_PROCESS_KEY] = ServiceState.STOPPED
                    self.messages[SIMULATION_PROCESS_KEY] = "Completed."
                else:
                    self.states[SIMULATION_PROCESS_KEY] = ServiceState.FAILED
                    self.messages[SIMULATION_PROCESS_KEY] = f"Exited with code {exit_code}."

    def run_simulation_background(
        self,
        *,
        scenario: str | None = None,
        run_name: str | None = None,
        run_id: str | None = None,
        solver: str | None = None,
        cooperation_structure: str | None = None,
        sharing_policy: str | None = None,
        order_lifecycle: str | None = None,
    ) -> ActionResult:
        """Start backend deps + simulation detached; logs to simulation_log.txt."""
        with self._lock:
            if self.is_simulation_running():
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=False,
                    state=ServiceState.RUNNING,
                    message="Simulation is already running.",
                )

            self.states[SIMULATION_PROCESS_KEY] = ServiceState.STARTING
            self.messages[SIMULATION_PROCESS_KEY] = "Starting…"

            try:
                dep_failure = self._ensure_backend_for_simulation()
                if dep_failure is not None:
                    self.states[SIMULATION_PROCESS_KEY] = ServiceState.FAILED
                    self.messages[SIMULATION_PROCESS_KEY] = dep_failure.message
                    return dep_failure

                extra_env: dict[str, str] = {}
                if scenario:
                    extra_env["ORSIM_SCENARIO"] = scenario
                if run_name and run_name.strip():
                    extra_env["ORSIM_RUN_NAME"] = run_name.strip()
                if run_id and run_id.strip():
                    extra_env["ORSIM_RUN_ID"] = run_id.strip()
                if solver and solver.strip():
                    extra_env["ORSIM_SOLVER"] = solver.strip()
                if cooperation_structure and cooperation_structure.strip():
                    extra_env["ORSIM_COOP_STRUCTURE"] = cooperation_structure.strip()
                if sharing_policy and sharing_policy.strip():
                    extra_env["ORSIM_SHARING_POLICY"] = sharing_policy.strip()
                if order_lifecycle and order_lifecycle.strip():
                    extra_env["ORSIM_ORDER_LIFECYCLE"] = order_lifecycle.strip()
                proc = spawn_simulation_process(extra_env=extra_env or None, background=True)
                self.processes[SIMULATION_PROCESS_KEY] = proc
                time.sleep(2)
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"Simulation exited immediately (code {proc.returncode})"
                    )

                self.states[SIMULATION_PROCESS_KEY] = ServiceState.RUNNING
                self.messages[SIMULATION_PROCESS_KEY] = "Running"
                threading.Thread(
                    target=self._watch_simulation_process,
                    args=(proc,),
                    daemon=True,
                    name="simulation-watch",
                ).start()
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=True,
                    state=ServiceState.RUNNING,
                    message="Simulation started in background.",
                )
            except Exception as ex:
                log.exception("Failed to start simulation")
                self.states[SIMULATION_PROCESS_KEY] = ServiceState.FAILED
                self.messages[SIMULATION_PROCESS_KEY] = str(ex)
                try:
                    self.stop_simulation()
                except Exception:
                    pass
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=False,
                    state=ServiceState.FAILED,
                    message=str(ex),
                )

    def run_simulation_foreground(
        self,
        emit_line: Callable[[str], None],
    ) -> ActionResult:
        """Start backend deps + simulation, stream stdout until exit or Ctrl+C."""
        with self._lock:
            if self.is_simulation_running():
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=False,
                    state=ServiceState.RUNNING,
                    message="Simulation is already running.",
                )

            try:
                dep_failure = self._ensure_backend_for_simulation()
                if dep_failure is not None:
                    return dep_failure

                proc = spawn_simulation_process()
                self.processes[SIMULATION_PROCESS_KEY] = proc
                time.sleep(2)
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"Simulation exited immediately (code {proc.returncode})"
                    )

                if not proc.stdout:
                    raise RuntimeError("Simulation process has no stdout stream.")

                SIMULATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(SIMULATION_LOG_FILE, "w", encoding="utf-8") as log_file:
                    for line in proc.stdout:
                        stamped = (
                            f"[SIMULATION_LOG] [{datetime.now().isoformat(timespec='seconds')}] => {line}"
                        )
                        log_file.write(stamped)
                        log_file.flush()
                        emit_line(line.rstrip("\n"))
                exit_code = proc.wait()
            except KeyboardInterrupt:
                log.info("Simulation interrupted by user")
                self.stop_simulation()
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=False,
                    state=ServiceState.STOPPED,
                    message="Simulation stopped (Ctrl+C).",
                )
            except Exception as ex:
                log.exception("Simulation failed to start or run")
                self.stop_simulation()
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=False,
                    state=ServiceState.FAILED,
                    message=str(ex),
                )

            self.processes.pop(SIMULATION_PROCESS_KEY, None)
            if exit_code == 0:
                return ActionResult(
                    key=SIMULATION_PROCESS_KEY,
                    ok=True,
                    state=ServiceState.STOPPED,
                    message="Simulation completed.",
                )
            return ActionResult(
                key=SIMULATION_PROCESS_KEY,
                ok=False,
                state=ServiceState.FAILED,
                message=f"Simulation failed (exit code {exit_code}).",
            )

    def start_backend(self) -> list[ActionResult]:
        results: list[ActionResult] = []
        for key in BACKEND_START_ORDER:
            results.append(self.start(key, with_dependencies=False))
        return results

    def stop_all(self, *, force: bool = True) -> list[ActionResult]:
        results: list[ActionResult] = []
        with self._lock:
            if self.is_simulation_running():
                try:
                    self.stop_simulation()
                    results.append(
                        ActionResult(
                            key=SIMULATION_PROCESS_KEY,
                            ok=True,
                            state=ServiceState.STOPPED,
                            message="Simulation stopped.",
                        )
                    )
                except Exception as ex:
                    results.append(
                        ActionResult(
                            key=SIMULATION_PROCESS_KEY,
                            ok=False,
                            state=ServiceState.FAILED,
                            message=str(ex),
                        )
                    )

            for key in reversed(BACKEND_START_ORDER):
                spec = SERVICES[key]
                if spec.kind != "process":
                    continue
                results.append(self._stop_one_locked(key, force=force))

            handled_projects: set[str] = set()
            for key in reversed(BACKEND_START_ORDER):
                spec = SERVICES[key]
                if spec.kind != "compose" or not spec.compose_dir:
                    continue
                project_key = str(spec.compose_dir.resolve())
                if project_key in handled_projects:
                    continue
                handled_projects.add(project_key)

                project_keys = [
                    service_key
                    for service_key, service_spec in SERVICES.items()
                    if service_spec.compose_dir
                    and str(service_spec.compose_dir.resolve()) == project_key
                ]
                running_before = {
                    service_key
                    for service_key in project_keys
                    if self.probe_state(service_key) == ServiceState.RUNNING
                }
                try:
                    running_services = run_compose_ps(spec.compose_dir)
                except Exception as ex:
                    for service_key in reversed(
                        [service_key for service_key in BACKEND_START_ORDER if service_key in project_keys]
                    ):
                        if self.probe_state(service_key) == ServiceState.RUNNING:
                            self.states[service_key] = ServiceState.FAILED
                            self.messages[service_key] = str(ex)
                            results.append(
                                ActionResult(
                                    key=service_key,
                                    ok=False,
                                    state=ServiceState.FAILED,
                                    message=str(ex),
                                )
                            )
                        else:
                            self.states[service_key] = ServiceState.STOPPED
                            self.messages[service_key] = ""
                            results.append(
                                ActionResult(
                                    key=service_key,
                                    ok=True,
                                    state=ServiceState.STOPPED,
                                    message=f"{SERVICES[service_key].label} is not running.",
                                )
                            )
                    continue

                if running_services:
                    try:
                        self._stop_compose_project(spec.compose_dir)
                    except Exception as ex:
                        for service_key in reversed(
                            [service_key for service_key in BACKEND_START_ORDER if service_key in project_keys]
                        ):
                            if self.probe_state(service_key) == ServiceState.RUNNING:
                                self.states[service_key] = ServiceState.FAILED
                                self.messages[service_key] = str(ex)
                                results.append(
                                    ActionResult(
                                        key=service_key,
                                        ok=False,
                                        state=ServiceState.FAILED,
                                        message=str(ex),
                                    )
                                )
                            else:
                                self.states[service_key] = ServiceState.STOPPED
                                self.messages[service_key] = ""
                                results.append(
                                    ActionResult(
                                        key=service_key,
                                        ok=True,
                                        state=ServiceState.STOPPED,
                                        message=f"{SERVICES[service_key].label} is not running.",
                                    )
                                )
                        continue

                for service_key in reversed(
                    [service_key for service_key in BACKEND_START_ORDER if service_key in project_keys]
                ):
                    label = SERVICES[service_key].label
                    if self.probe_state(service_key) == ServiceState.RUNNING:
                        msg = f"{label} is still running."
                        self.states[service_key] = ServiceState.FAILED
                        self.messages[service_key] = msg
                        results.append(
                            ActionResult(
                                key=service_key,
                                ok=False,
                                state=ServiceState.FAILED,
                                message=msg,
                            )
                        )
                    else:
                        self.states[service_key] = ServiceState.STOPPED
                        self.messages[service_key] = ""
                        msg = "Stopped" if service_key in running_before else f"{label} is not running."
                        results.append(
                            ActionResult(
                                key=service_key,
                                ok=True,
                                state=ServiceState.STOPPED,
                                message=msg,
                            )
                        )
        return results

    def _stop_one_locked(self, key: str, *, force: bool = False) -> ActionResult:
        spec = SERVICES[key]
        self.refresh_states()

        if self.probe_state(key) != ServiceState.RUNNING:
            self.states[key] = ServiceState.STOPPED
            self.messages[key] = ""
            return ActionResult(
                key=key,
                ok=True,
                state=ServiceState.STOPPED,
                message=f"{spec.label} is not running.",
            )

        dependents = self.running_dependents(key)
        if dependents and not force:
            names = ", ".join(SERVICES[d].label for d in dependents)
            msg = f"Stop dependents first: {names}"
            self.messages[key] = msg
            return ActionResult(
                key=key,
                ok=False,
                state=ServiceState.RUNNING,
                message=msg,
                blocked_by=dependents,
            )

        self.states[key] = ServiceState.STOPPING
        self.messages[key] = "Stopping…"
        log.info("Stopping %s", spec.label)

        try:
            if spec.kind == "compose":
                self._stop_compose(spec)
            else:
                self._stop_process(key, spec)

            self.states[key] = ServiceState.STOPPED
            self.messages[key] = ""
            log.info("%s stopped", spec.label)
            return ActionResult(key=key, ok=True, state=ServiceState.STOPPED, message="Stopped")
        except Exception as ex:
            self.states[key] = ServiceState.FAILED
            self.messages[key] = str(ex)
            log.exception("Failed to stop %s", spec.label)
            return ActionResult(key=key, ok=False, state=ServiceState.FAILED, message=str(ex))

    def _ensure_watchdog(self) -> None:
        if self._watchdog_started:
            return
        threading.Thread(target=self._log_watchdog_loop, daemon=True).start()
        self._watchdog_started = True

    def _log_watchdog_loop(self) -> None:
        error_keywords = ("Error", "Exception", "Traceback", "ConnectionRefused", "Killed")
        ignore_if_contains = (
            "Cannot connect to amqp://",
            "Trying again in",
            "Connection to broker lost",
            "INTERNAL_ERROR",
            "ConsumerCancelled",
        )
        last_fingerprints: dict[str, str] = {}
        while True:
            for key, spec in SERVICES.items():
                if not spec.log_file or not spec.log_file.is_file():
                    continue
                try:
                    lines = spec.log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
                    content = "\n".join(lines)
                    if not content or not any(k in content for k in error_keywords):
                        continue
                    if any(skip in content for skip in ignore_if_contains):
                        continue
                    if content == last_fingerprints.get(key):
                        continue
                    last_fingerprints[key] = content
                    log.warning("Watchdog · %s:\n%s", spec.label, content)
                except Exception:
                    pass
            time.sleep(5)

    def tail_log_lines(self, key: str, lines: int = 20) -> str:
        spec = SERVICES[key]
        if not spec.log_file or not spec.log_file.is_file():
            return ""
        content = spec.log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        return "\n".join(content)
