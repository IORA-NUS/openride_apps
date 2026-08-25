"""``openride run <slug>`` — launch a simulation (the original headless-CLI flow).

Interactive by default (pick scenario/solver, live TUI dashboard); ``--json`` or a missing
TTY takes the clean scriptable path (``run_cmd.run_vanilla`` — one JSON object to stdout).
This is the same machinery the dashboard uses; ``--headless`` just suppresses the per-tick
geo/truck-location streaming that only feeds the map, so analysis-only runs go faster.
"""

from __future__ import annotations

import sys
import time
from typing import Optional

from rich.panel import Panel

from . import config, control
from .runner import SimulationRunner
from .ui import console, emit_report, render_scenarios

_BANNER = (
    "[bold cyan]OpenRide[/] · run\n"
    "[dim]Same sim as the dashboard; --headless drops geo streaming for faster analysis.[/]"
)


def _scenario_label(s: dict) -> str:
    agents = s.get("agents") or {}
    return (
        f"{s.get('name', s.get('slug'))}  —  {agents.get('truck', '?')} trucks · "
        f"{agents.get('order', '?')} orders · {s.get('simulationDays', '?')}d   [{s.get('slug')}]"
    )


def _pick_scenario(scenarios: list[dict]) -> Optional[str]:
    import questionary

    if not scenarios:
        console.print("[red]No scenarios on disk.[/] Create one: [bold]openride scenario new[/].")
        return None
    choices = [questionary.Choice(_scenario_label(s), value=s.get("slug")) for s in scenarios]
    return questionary.select("Pick a scenario to run:", choices=choices).ask()


def _pick_solver() -> Optional[str]:
    import questionary

    choices = [questionary.Choice("Scenario default", value="__default__")]
    choices += [questionary.Choice(s, value=s) for s in config.SOLVERS]
    answer = questionary.select("Assignment solver:", choices=choices).ask()
    if answer in (None, "__default__"):
        return None
    return answer


def _quiet_wait(runner: SimulationRunner) -> str:
    """Non-TUI interactive mode: print progress lines occasionally, block until the run ends."""
    last_step = -1
    try:
        while runner.is_alive() or runner.state.snapshot()["status"] == "starting":
            snap = runner.state.snapshot()
            if snap["step"] != last_step and snap["total_steps"]:
                last_step = snap["step"]
                console.print(
                    f"[dim]step {snap['step']:,}/{snap['total_steps']:,} "
                    f"day {snap['sim_days']:.2f} ({snap['fraction'] * 100:.0f}%)[/]"
                )
            time.sleep(1.0)
            if snap["status"] in ("completed", "failed", "stopped"):
                break
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping simulation…[/]")
        runner.stop()
    runner.wait(timeout=25)
    return runner.state.snapshot()["status"]


def _cmd_run(args) -> int:
    # Reconcile positional slug with the legacy --scenario flag.
    if getattr(args, "scenario_pos", None) and not args.scenario:
        args.scenario = args.scenario_pos

    if not args.json:
        console.print(Panel(_BANNER, border_style="cyan"))

    if args.report_only:
        ok = emit_report(args.report_only, config.REPORT_BASE / args.report_only)
        return 0 if ok else 1

    try:
        scenarios = control.list_scenarios(args.domain)
    except control.ControlError as exc:
        console.print(f"[red]Could not list scenarios:[/] {exc}")
        return 1

    if args.list:
        render_scenarios(scenarios)
        return 0

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    # Vanilla scriptable path (no TTY, or --json from a terminal).
    if args.json or not interactive:
        from . import run_cmd

        valid_slugs = {s.get("slug") for s in scenarios}
        return run_cmd.run_vanilla(args, valid_slugs)

    # -- interactive resolve ---------------------------------------------
    scenario = args.scenario
    if not scenario:
        scenario = _pick_scenario(scenarios)
        if not scenario:
            return 1

    valid_slugs = {s.get("slug") for s in scenarios}
    if scenario not in valid_slugs:
        console.print(f"[red]Unknown scenario slug:[/] {scenario}")
        render_scenarios(scenarios)
        return 2

    solver = args.solver
    if solver is None and not args.scenario:
        solver = _pick_solver()

    headless = not args.no_headless
    runner = SimulationRunner(
        scenario=scenario, solver=solver, run_name=args.run_name, headless=headless,
        cooperation_structure=getattr(args, "structure", None),
        sharing_policy=getattr(args, "sharing_policy", None),
    )

    if runner.another_run_active():
        console.print("[red]A simulation is already running on this host. Stop it first.[/]")
        return 1

    if not args.no_backend_check:
        import questionary

        with console.status("[cyan]Ensuring backend services are up…", spinner="dots"):
            err = runner.ensure_backend()
        if err:
            console.print(f"[red]Backend not ready:[/] {err}")
            if not questionary.confirm("Try to run anyway?", default=False).ask():
                return 1

    console.print(
        f"Launching [bold]{scenario}[/]  ·  solver [bold]{solver or 'scenario default'}[/]  ·  "
        f"headless [bold]{'on' if headless else 'off'}[/]  ·  run id [bold]{runner.run_id}[/]"
    )
    runner.start()

    if args.no_tui:
        status = _quiet_wait(runner)
    else:
        from .dashboard import run_live

        status = run_live(runner, console)

    if status == "failed":
        console.print(f"[red]Run failed:[/] {runner.state.snapshot().get('error') or 'unknown error'}")
        console.print(f"[dim]See {runner.log_path}[/]")
    elif status == "stopped":
        console.print("[yellow]Run stopped.[/]")
    else:
        console.print("[green]Run completed.[/]")

    if status == "completed":
        time.sleep(2.0)
    emit_report(runner.run_id, runner.report_dir)
    return 0 if status in ("completed", "stopped") else 1


def register(sub) -> None:
    p = sub.add_parser("run", help="Run a simulation (interactive TUI, or --json scriptable)")
    p.add_argument("scenario_pos", nargs="?", metavar="SCENARIO", help="Scenario slug to run")
    p.add_argument("--scenario", help="Scenario slug (alternative to the positional)")
    p.add_argument("--solver", choices=config.SOLVERS, help="Assignment solver override")
    p.add_argument("--structure", help="Cooperation structure id override (declared in the scenario's spec.json)")
    p.add_argument("--sharing-policy", dest="sharing_policy",
                   help="Sharing algorithm override for two-stage planners")
    p.add_argument("--run-name", help="Human-readable run name")
    p.add_argument("--no-headless", action="store_true",
                   help="Keep geo/truck-location streaming on (slower; same as a dashboard run)")
    p.add_argument("--no-tui", action="store_true", help="No live dashboard; just progress lines")
    p.add_argument("--no-backend-check", action="store_true", help="Skip ensuring backend services are up")
    p.add_argument("--list", action="store_true", help="List scenarios and exit")
    p.add_argument("--report-only", metavar="RUN_ID", help="Skip running; report on an existing run")
    p.add_argument("--domain", default=None, help="Scenario domain dir override")
    p.add_argument("--json", action="store_true",
                   help="Vanilla non-interactive run: one JSON result on stdout (progress to stderr)")
    p.add_argument("--max-wall-seconds", type=float, default=None,
                   help="Watchdog: stop the run after this many wall seconds (exit 5)")
    p.add_argument("--require-mongo", action="store_true",
                   help="Fail preflight (exit 4) if Mongo is unreachable instead of warning")
    p.set_defaults(func=_cmd_run)
