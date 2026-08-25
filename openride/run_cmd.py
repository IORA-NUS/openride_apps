"""Vanilla (non-interactive) run command.

A clean, dependency-light path with no prompts and no live TUI: validate inputs, run
preflight checks, start the headless sim, block until it ends, build the report, and emit a
machine-readable JSON summary. This is the entry a scheduler / cron / CI would call.

Human-readable progress goes to **stderr** so **stdout stays clean** for the JSON summary
(``--json``). Exit codes are stable so a caller can branch on them:

    0  completed
    1  run failed
    2  bad arguments / unknown scenario   (also enforced by argparse in cli.py)
    3  another sim already running on this host
    4  backend or Mongo not ready
    5  run exceeded --max-wall-seconds (watchdog stopped it)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, report
from .mongo_reader import MongoReader
from .runner import SimulationRunner

# Exit codes (see module docstring).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2
EXIT_HOST_BUSY = 3
EXIT_NOT_READY = 4
EXIT_TIMED_OUT = 5

# Scalar KPIs surfaced in the compact JSON summary (the headline analysis numbers). The full
# set still lands in report.json / report.md via report.write_report().
_SUMMARY_SCALAR_KEYS = (
    "empty_distance_ratio",
    "avg_empty_distance_km",
    "dual_cycle_rate",
    "orders_per_truck",
    "orders_per_truck_day",
    "avg_truck_active_hours",
    "avg_queue_wait_seconds",
    "num_hauls_completed",
    "num_orders_completed",
)


def _log(msg: str) -> None:
    """Human-readable progress to stderr (keeps stdout clean for JSON)."""
    print(msg, file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_vanilla(args, valid_slugs: set[str]) -> int:
    """Non-interactive run. Returns a process exit code."""
    json_only = bool(getattr(args, "json", False))

    # -- validate inputs --------------------------------------------------
    if not args.scenario:
        _log("error: --scenario is required.")
        return EXIT_BAD_ARGS
    if args.scenario not in valid_slugs:
        _log(f"error: unknown scenario slug: {args.scenario}")
        return EXIT_BAD_ARGS
    # Solver choice is already constrained by argparse; None = scenario default.
    solver = args.solver
    headless = not args.no_headless

    runner = SimulationRunner(
        scenario=args.scenario,
        solver=solver,
        run_name=args.run_name,
        headless=headless,
        cooperation_structure=getattr(args, "structure", None),
        sharing_policy=getattr(args, "sharing_policy", None),
    )

    started_at = _now_iso()

    # -- preflight checks -------------------------------------------------
    # The engine allows only one sim per host — refuse to stack a second one even when
    # --no-backend-check is set.
    if runner.another_run_active():
        _log("error: a simulation is already running on this host. Stop it first.")
        return EXIT_HOST_BUSY

    if not args.no_backend_check:
        _log("preflight: ensuring backend services are up…")
        err = runner.ensure_backend()
        if err:
            _log(f"error: backend not ready: {err}")
            return EXIT_NOT_READY

    # Preflight Mongo *before* the run so we don't burn a long run and then fail to report.
    mongo_ok = MongoReader().ping()
    if not mongo_ok:
        if getattr(args, "require_mongo", False):
            _log("error: Mongo unreachable (--require-mongo set).")
            return EXIT_NOT_READY
        _log("warning: Mongo unreachable — the run will proceed but the report may be empty.")

    _log(
        f"launching scenario={args.scenario} solver={solver or 'scenario-default'} "
        f"headless={'on' if headless else 'off'} run_id={runner.run_id}"
    )

    # -- run + wait -------------------------------------------------------
    runner.start()
    status = _wait(runner, max_wall_seconds=getattr(args, "max_wall_seconds", None))

    snap = runner.state.snapshot()
    if status == "completed":
        _log("run completed.")
        # Let the run-finalize path write the authoritative final breakdown.
        time.sleep(2.0)
    elif status == "timed_out":
        _log(f"run exceeded watchdog limit ({args.max_wall_seconds}s) — stopped.")
    elif status == "failed":
        _log(f"run failed: {snap.get('error') or 'unknown error'}  (see {runner.log_path})")
    elif status == "stopped":
        _log("run stopped.")

    # -- report + JSON summary -------------------------------------------
    summary = _build_and_write_report(runner.run_id, runner.report_dir)
    result = _result_doc(
        runner=runner,
        scenario=args.scenario,
        solver=solver,
        headless=headless,
        status=status,
        started_at=started_at,
        snap=snap,
        summary=summary,
    )

    # Always persist the compact summary next to the run's other artifacts.
    summary_path = runner.report_dir / "summary.json"
    try:
        runner.report_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        result["summary_path"] = str(summary_path)
    except OSError as exc:
        _log(f"warning: could not write summary.json: {exc}")

    # Machine-readable result to stdout (the one thing on stdout in --json mode).
    print(json.dumps(result, default=str), flush=True)
    if not json_only:
        _log(f"summary written: {summary_path}")

    return _exit_code_for(status)


# -- helpers --------------------------------------------------------------
def _wait(runner: SimulationRunner, max_wall_seconds: Optional[float]) -> str:
    """Block until the run ends, printing occasional progress lines to stderr.

    Returns the final status string ('completed' | 'failed' | 'stopped' | 'timed_out').
    """
    last_step = -1
    start = time.monotonic()
    try:
        while True:
            snap = runner.state.snapshot()
            if snap["status"] in ("completed", "failed", "stopped"):
                break
            if not runner.is_alive() and snap["status"] != "starting":
                break
            if snap["step"] != last_step and snap["total_steps"]:
                last_step = snap["step"]
                _log(
                    f"  step {snap['step']:,}/{snap['total_steps']:,} "
                    f"day {snap['sim_days']:.2f} ({snap['fraction'] * 100:.0f}%)"
                )
            if max_wall_seconds and (time.monotonic() - start) > float(max_wall_seconds):
                runner.stop()
                runner.wait(timeout=25)
                return "timed_out"
            time.sleep(1.0)
    except KeyboardInterrupt:
        _log("interrupted — stopping simulation…")
        runner.stop()

    # Join the stdout reader so EOF-driven completed/failed is settled before we read status.
    runner.wait(timeout=25)
    return runner.state.snapshot()["status"]


def _build_and_write_report(run_id: str, report_dir) -> Optional[dict[str, Any]]:
    reader = MongoReader()
    if not reader.ping():
        _log("warning: Mongo unreachable — no KPI report produced.")
        return None
    try:
        summary = report.build_summary(reader, run_id)
    finally:
        reader.close()
    has_data = bool(summary["hauliers"]) or bool(summary["scalar_kpis"]) or bool(summary["run_config"])
    if not has_data:
        _log(f"warning: no KPI data found for run {run_id} — report will be empty.")
        return summary
    report.write_report(summary, report_dir)
    return summary


def _result_doc(
    *,
    runner: SimulationRunner,
    scenario: str,
    solver: Optional[str],
    headless: bool,
    status: str,
    started_at: str,
    snap: dict[str, Any],
    summary: Optional[dict[str, Any]],
) -> dict[str, Any]:
    fleet = (summary or {}).get("fleet") or {}
    scalar = (summary or {}).get("scalar_kpis") or {}
    kpis = {k: scalar[k] for k in _SUMMARY_SCALAR_KEYS if k in scalar}
    return {
        "run_id": runner.run_id,
        "run_name": runner.run_name,
        "scenario": scenario,
        "solver": solver or "scenario-default",
        "headless": headless,
        "status": status,
        "ok": status == "completed",
        "exit_code": _exit_code_for(status),
        "started_at": started_at,
        "ended_at": _now_iso(),
        "wall_seconds": snap.get("wall_seconds"),
        "sim_days": snap.get("sim_days"),
        "steps": snap.get("step"),
        "total_steps": snap.get("total_steps"),
        "error": snap.get("error"),
        "fleet": {
            "num_trucks": fleet.get("num_trucks"),
            "num_hauliers": fleet.get("num_hauliers"),
            "num_orders_completed": fleet.get("num_orders_completed"),
            "empty_ratio": fleet.get("empty_ratio"),
            "empty_km": fleet.get("empty_km"),
            "total_km": fleet.get("total_km"),
            "dual_cycle_rate": fleet.get("dual_cycle_rate"),
        },
        "kpis": kpis,
        "report_dir": str(runner.report_dir),
        "log_path": str(runner.log_path),
        "report_final": bool((summary or {}).get("final")),
    }


def _exit_code_for(status: str) -> int:
    if status == "completed":
        return EXIT_OK
    if status == "timed_out":
        return EXIT_TIMED_OUT
    if status == "stopped":
        return EXIT_OK  # a deliberate stop is not a failure
    return EXIT_FAILED
