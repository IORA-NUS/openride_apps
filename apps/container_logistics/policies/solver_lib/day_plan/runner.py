"""Run one day-plan solve in a CHILD process (plan §4 ``runner.py``; addendum B2 / M2 / minor 8).

Single responsibility: hand :class:`instance.PlanInputs` to
``python -m apps.container_logistics.policies.solver_lib.day_plan.child`` (the agent's own
interpreter, ``sys.executable``, addendum M2), bound it in wall time and memory, and turn
every way it can go wrong into a typed error. Never in-process: SCIP holds the GIL, so a
thread would freeze every agent of the celery worker; and N0 measured a MIP child at
11.6 GB RSS.

- **spawn** -- ``eventlet.green.subprocess`` when eventlet is importable (the agent runs on
  an eventlet pool: waiting must yield to the other greenlets), else ``subprocess``. The
  request and the answer go through temporary FILES bound to the child's stdin / stdout
  (no pipe can fill up and deadlock, whatever the answer's size); stderr likewise.
- **limits** -- ``preexec_fn`` = ``coop_dispatch.limits.memory_cap_preexec`` (RLIMIT_AS,
  default 4 GiB), applied in the forked child only; ``SCIP_MEMORY_LIMIT_MB`` is set below
  that cap so SCIP stops itself first. The child gets its own session, and on timeout the
  whole process group is SIGKILLed.
- **errors** -- :class:`SolveTimeout` (killed at ``timeout_s``), :class:`SolveCrashed`
  (non-zero exit, out of memory, unreadable answer), :class:`SolveRejected` (the child
  refused, e.g. ``poor_incumbent``; or its answer breaks the plan's invariants:
  ``invalid_output``). All are :class:`SolveError`. The caller keeps its previous plan
  version on any of them.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping



def _limits():
    """``coop_dispatch.limits`` (stdlib only) even without OR-Tools: the package ``__init__``
    imports the MIP eagerly, so on ImportError the module is loaded from its file."""
    try:
        from ..coop_dispatch import limits
        return limits
    except ImportError:
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "coop_dispatch" / "limits.py"
        spec = importlib.util.spec_from_file_location("_day_plan_coop_limits", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod


_LIMITS = _limits()
DEFAULT_MEMORY_CAP_BYTES = _LIMITS.DEFAULT_MEMORY_CAP_BYTES
memory_cap_preexec = _LIMITS.memory_cap_preexec

try:  # the agent's pool is eventlet: a green wait yields to the other greenlets
    from eventlet.green import subprocess as _subprocess
    GREEN = True
except ImportError:  # pragma: no cover - eventlet is in requirements
    import subprocess as _subprocess
    GREEN = False

__all__ = [
    "CHILD_MODULE",
    "SolveCrashed",
    "SolveError",
    "SolveRejected",
    "SolveTimeout",
    "solve",
    "validate_output",
]

CHILD_MODULE = "apps.container_logistics.policies.solver_lib.day_plan.child"
#: repo root (``apps/`` lives here), put first on the child's PYTHONPATH.
_REPO_ROOT = str(Path(__file__).resolve().parents[5])
_EXIT_OOM = 3
_STDERR_TAIL = 4000


class SolveError(RuntimeError):
    """Any failed solve; ``reason`` is the short tag the wrapper notes (``replan_failed``)."""

    reason = "error"

    def __init__(self, message: str, *, stats: Mapping[str, Any] | None = None,
                 stderr: str = "", elapsed_s: float | None = None):
        super().__init__(message)
        self.stats = dict(stats or {})
        self.stderr = stderr
        self.elapsed_s = elapsed_s


class SolveTimeout(SolveError):
    reason = "timeout"


class SolveCrashed(SolveError):
    reason = "crashed"

    def __init__(self, message: str, *, returncode: int | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.returncode = returncode
        if returncode == _EXIT_OOM or "MemoryError" in self.stderr:
            self.reason = "out_of_memory"


class SolveRejected(SolveError):
    def __init__(self, reason: str, message: str, **kw: Any):
        super().__init__(message, **kw)
        self.reason = reason


def validate_output(inputs: Mapping[str, Any], out: Mapping[str, Any]) -> list[str]:
    """Problems with a child answer against its inputs (empty: valid).

    Every itinerary belongs to an input truck; every job is an input job and is planned
    at most once; ``unplanned`` is exactly the input jobs no itinerary holds."""
    problems: list[str] = []
    trucks = {t["id"] for t in inputs["trucks"]}
    jobs = {j["id"] for j in inputs["jobs"]}
    seen: dict[str, str] = {}
    itins = out.get("itineraries")
    if not isinstance(itins, Mapping):
        return ["itineraries is not a mapping"]
    for t, chain in itins.items():
        if t not in trucks:
            problems.append(f"itinerary for unknown truck {t!r}")
        if not isinstance(chain, list):
            problems.append(f"itinerary of {t!r} is not a list")
            continue
        for j in chain:
            if j not in jobs:
                problems.append(f"truck {t!r}: unknown job {j!r}")
            elif j in seen:
                problems.append(f"job {j!r} on trucks {seen[j]!r} and {t!r}")
            else:
                seen[j] = t
    expected = sorted(jobs - set(seen))
    if sorted(out.get("unplanned") or []) != expected:
        problems.append("unplanned is not exactly the input jobs no itinerary holds")
    return problems


def _child_env(memory_cap_bytes: int) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(x for x in (_REPO_ROOT, env.get("PYTHONPATH")) if x)
    # SCIP checks its own memory limit; keep it under the address-space cap.
    env["SCIP_MEMORY_LIMIT_MB"] = str(max(256, int(memory_cap_bytes * 0.6) // (1024 * 1024)))
    env.setdefault("PYTHONHASHSEED", "0")
    return env


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 - best effort reap
        pass


def solve(inputs: Mapping[str, Any], *, backend: str = "auto", time_limit_s: float | None = 60.0,
          timeout_s: float | None = None, memory_cap_bytes: int = DEFAULT_MEMORY_CAP_BYTES,
          python: str | None = None, child_args: list[str] | None = None,
          deadline_s: float | None = None, accept_poor: bool = False) -> dict[str, Any]:
    """Solve ``inputs`` in a child; returns ``{"itineraries", "unplanned", "stats"}``.

    ``backend``: ``auto`` (exact min-cost flow where it applies, MIP + guard elsewhere),
    ``mincostflow`` (same), ``mip`` (MIP everywhere), ``greedy`` (no OR-Tools).
    ``time_limit_s`` bounds each MIP component; ``timeout_s`` (default
    ``time_limit_s + 15``, or 120 s) is the hard wall-time kill of the whole child.
    ``deadline_s`` (seconds from now, default ``timeout_s - 1``) is the child's ONE solve
    deadline across all components (M2): MIP components share what is left and the rest
    goes greedy, so the child answers before it is killed. ``accept_poor``: keep a greedy
    fallback under the quality guard instead of refusing (the caller has no plan to keep).
    ``python`` / ``child_args`` replace the interpreter / ``-m <child>`` (tests only).
    ``stats`` gains ``wall_ms`` (spawn to answer, parent side) and ``green``.
    Raises :class:`SolveTimeout`, :class:`SolveCrashed` or :class:`SolveRejected`.
    """
    if timeout_s is None:
        timeout_s = (float(time_limit_s) + 15.0) if time_limit_s is not None else 120.0
    if deadline_s is None:
        deadline_s = max(0.5, float(timeout_s) - 1.0)
    request = {"inputs": inputs, "backend": backend, "time_limit_s": time_limit_s,
               "deadline_epoch": time.time() + float(deadline_s), "accept_poor": bool(accept_poor)}
    argv = [python or sys.executable] + (child_args if child_args is not None else ["-m", CHILD_MODULE])
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="day_plan_") as tmp:
        p_in, p_out, p_err = (os.path.join(tmp, n) for n in ("in.json", "out.json", "err.txt"))
        with open(p_in, "w") as f:
            json.dump(request, f, separators=(",", ":"), allow_nan=False)
        with open(p_in, "rb") as fin, open(p_out, "wb") as fout, open(p_err, "wb") as ferr:
            proc = _subprocess.Popen(argv, stdin=fin, stdout=fout, stderr=ferr, cwd=_REPO_ROOT,
                                     env=_child_env(memory_cap_bytes),
                                     preexec_fn=memory_cap_preexec(memory_cap_bytes),
                                     start_new_session=True, close_fds=True)
            try:
                rc = proc.wait(timeout=timeout_s)
            except _subprocess.TimeoutExpired:
                _kill(proc)
                elapsed = time.monotonic() - t0
                raise SolveTimeout(f"day-plan solve killed after {elapsed:.1f} s (timeout {timeout_s} s)",
                                   stderr=_tail(p_err), elapsed_s=elapsed) from None
            except BaseException:
                _kill(proc)
                raise
        elapsed = time.monotonic() - t0
        err = _tail(p_err)
        if rc != 0:
            raise SolveCrashed(f"day-plan child exited {rc}: {err[-500:]}", returncode=rc,
                               stderr=err, elapsed_s=elapsed)
        try:
            with open(p_out) as f:
                out = json.load(f)
        except (OSError, ValueError) as exc:
            raise SolveCrashed(f"day-plan child answer unreadable ({exc})", returncode=rc,
                               stderr=err, elapsed_s=elapsed) from None
    if not isinstance(out, Mapping) or "ok" not in out:
        raise SolveCrashed("day-plan child answer is not {ok: ...}", returncode=0, stderr=err,
                           elapsed_s=elapsed)
    stats = dict(out.get("stats") or {})
    stats.update(wall_ms=round(elapsed * 1000.0, 1), green=GREEN)
    if not out["ok"]:
        reason = str(out.get("reason") or "rejected")
        raise SolveRejected(reason, f"day-plan solve refused: {reason}", stats=stats, stderr=err,
                            elapsed_s=elapsed)
    problems = validate_output(inputs, out)
    if problems:
        raise SolveRejected("invalid_output", "day-plan child answer is invalid: " + "; ".join(problems[:5]),
                            stats=stats, stderr=err, elapsed_s=elapsed)
    return {"itineraries": out["itineraries"], "unplanned": out["unplanned"], "stats": stats}


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            n = f.tell()
            f.seek(max(0, n - _STDERR_TAIL))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""
