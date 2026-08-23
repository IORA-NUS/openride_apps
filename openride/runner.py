"""Spawn + supervise a headless simulation subprocess and parse its progress.

Reuses the existing launch machinery: ``ServiceManager._ensure_backend_for_simulation`` for
backend deps and ``registry.spawn_simulation_process`` to launch the same sim module the
dashboard uses — only difference is the injected ``ORSIM_HEADLESS`` env (suppresses the
visual geo/loc streaming) and that we keep stdout to stream a live view.
"""

from __future__ import annotations

import os
import re
import signal
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from openride_control.manager import ServiceManager
from openride_control.registry import (
    spawn_simulation_process,
    stop_simulation_process,
)

from . import config

_PROGRESS_RE = re.compile(r"step=(\d+)/(\d+)\s+sim_time=([\d.]+)d\s+wall=([\d.]+)s")
_AGENTS_RE = re.compile(r"Scenario agents:\s*trucks=(\d+)\s+orders=(\d+)\s+facilities=(\d+)")
_STEPS_RE = re.compile(r"Sim steps:\s*(\d+)")


def new_run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


class RunState:
    """Thread-safe snapshot of the running sim, updated from the stdout reader thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "starting"  # starting | running | completed | failed | stopped
        self.step = 0
        self.total_steps = 0
        self.sim_days = 0.0
        self.wall_seconds = 0.0
        self.trucks = 0
        self.orders = 0
        self.facilities = 0
        self.error: Optional[str] = None
        self.lines: deque[str] = deque(maxlen=400)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "step": self.step,
                "total_steps": self.total_steps,
                "sim_days": self.sim_days,
                "wall_seconds": self.wall_seconds,
                "trucks": self.trucks,
                "orders": self.orders,
                "facilities": self.facilities,
                "error": self.error,
                "lines": list(self.lines),
                "fraction": (self.step / self.total_steps) if self.total_steps else 0.0,
            }

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)


class SimulationRunner:
    def __init__(
        self,
        *,
        scenario: str,
        solver: Optional[str] = None,
        run_name: Optional[str] = None,
        run_id: Optional[str] = None,
        headless: bool = True,
        cooperation_structure: Optional[str] = None,
        sharing_policy: Optional[str] = None,
    ) -> None:
        self.scenario = scenario
        self.solver = solver
        self.cooperation_structure = cooperation_structure
        self.sharing_policy = sharing_policy
        self.run_name = run_name
        self.run_id = run_id or new_run_id()
        self.headless = headless
        self.state = RunState()
        self._proc = None
        self._reader: Optional[threading.Thread] = None
        self.report_dir = config.REPORT_BASE / self.run_id
        self.log_path = self.report_dir / "sim.log"

    # -- lifecycle --------------------------------------------------------
    @staticmethod
    def another_run_active() -> bool:
        """True if a sim is already running on this host (the engine allows only one)."""
        try:
            return ServiceManager().is_simulation_running()
        except Exception:
            return False

    def ensure_backend(self) -> Optional[str]:
        """Start backend deps if needed. Returns an error message, or None on success."""
        mgr = ServiceManager()
        if mgr.is_simulation_running():
            return "A simulation is already running on this host. Stop it first."
        failure = mgr._ensure_backend_for_simulation()
        if failure is not None:
            return failure.message
        return None

    def _build_env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "ORSIM_SCENARIO": self.scenario,
            "ORSIM_RUN_ID": self.run_id,
        }
        if self.run_name:
            env["ORSIM_RUN_NAME"] = self.run_name
        if self.solver:
            env["ORSIM_SOLVER"] = self.solver
        if self.cooperation_structure:
            env["ORSIM_COOP_STRUCTURE"] = self.cooperation_structure
        if self.sharing_policy:
            env["ORSIM_SHARING_POLICY"] = self.sharing_policy
        if self.headless:
            env["ORSIM_HEADLESS"] = "1"
        return env

    def start(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._proc = spawn_simulation_process(extra_env=self._build_env(), background=False)
        self.state._set(status="running")
        self._reader = threading.Thread(target=self._read_stdout, daemon=True, name="sim-stdout")
        self._reader.start()

    def _read_stdout(self) -> None:
        assert self._proc is not None
        log_file = open(self.log_path, "w", encoding="utf-8")
        try:
            for raw in self._proc.stdout:  # type: ignore[union-attr]
                line = raw.rstrip("\n")
                log_file.write(raw)
                log_file.flush()
                with self.state._lock:
                    self.state.lines.append(line)
                self._parse_line(line)
        finally:
            log_file.close()
            code = self._proc.wait()
            cur = self.state.snapshot()["status"]
            if cur not in ("completed", "failed", "stopped"):
                if code == 0:
                    self.state._set(status="completed")
                else:
                    self.state._set(status="failed", error=f"exit code {code}")

    def _parse_line(self, line: str) -> None:
        m = _PROGRESS_RE.search(line)
        if m:
            self.state._set(
                step=int(m.group(1)),
                total_steps=int(m.group(2)),
                sim_days=float(m.group(3)),
                wall_seconds=float(m.group(4)),
            )
            return
        m = _AGENTS_RE.search(line)
        if m:
            self.state._set(
                trucks=int(m.group(1)),
                orders=int(m.group(2)),
                facilities=int(m.group(3)),
            )
            return
        m = _STEPS_RE.search(line)
        if m:
            self.state._set(total_steps=int(m.group(1)))
            return
        if "Simulation completed!" in line:
            self.state._set(status="completed")
        elif "Simulation Error:" in line:
            self.state._set(status="failed", error=line.split("Simulation Error:", 1)[-1].strip())

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        self.state._set(status="stopped")
        # Prefer killing only *our* sim's process group (spawned with start_new_session=True,
        # so proc.pid is its group leader) — avoids nuking an unrelated sim a dashboard may
        # have launched. Fall back to the by-pattern stop if the targeted kill doesn't take.
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except Exception:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        stop_simulation_process()

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._reader is not None:
            self._reader.join(timeout)
