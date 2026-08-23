"""Interactive front-door menu for the OpenRide CLI.

Launched when ``openride`` is run with no subcommand on a TTY (see ``cli.py``). It's a thin
orchestration layer over the existing verb handlers — every action routes into the same
``scenario_cmd`` / ``run_group`` / ``analyze`` / ``services`` code the flag CLI uses, so the
menu and the flags can never diverge. Non-interactive callers never reach here.
"""

from __future__ import annotations

import argparse
from typing import Optional

from . import analyze_cmd, config, control, run_group, scenario_cmd, services_cmd
from .control import ControlError
from .mongo_reader import MongoReader
from .ui import console, emit_report, render_scenarios


def _run_namespace(domain: Optional[str]) -> argparse.Namespace:
    """A defaulted `run` args object → the interactive run flow (pick scenario/solver + TUI)."""
    return argparse.Namespace(
        scenario_pos=None, scenario=None, solver=None, run_name=None,
        no_headless=False, no_tui=False, no_backend_check=False,
        list=False, report_only=None, domain=domain, json=False,
        max_wall_seconds=None, require_mongo=False,
    )


def _safe(action) -> None:
    """Run a menu action, surfacing control/input errors without exiting the menu."""
    try:
        action()
    except (ControlError, ValueError, FileNotFoundError) as exc:
        console.print(f"[red]Error:[/] {exc}")


def _scenarios_menu(domain: Optional[str]) -> None:
    import questionary

    while True:
        choice = questionary.select(
            "Scenarios —",
            choices=[
                questionary.Choice("New (guided wizard)", "new"),
                questionary.Choice("List", "list"),
                questionary.Choice("Show", "show"),
                questionary.Choice("Edit", "edit"),
                questionary.Choice("Regenerate (recompile data)", "compile"),
                questionary.Choice("Delete", "delete"),
                questionary.Choice("Rebuild index", "reindex"),
                questionary.Choice("← Back", "back"),
            ],
        ).ask()
        if choice in (None, "back"):
            return
        if choice == "new":
            _safe(lambda: scenario_cmd.interactive_new(domain))
        elif choice == "list":
            _safe(lambda: render_scenarios(control.list_scenarios(domain)))
        elif choice == "show":
            _safe(lambda: scenario_cmd.interactive_show(domain))
        elif choice == "edit":
            _safe(lambda: scenario_cmd.interactive_edit(domain))
        elif choice == "compile":
            _safe(lambda: _compile_one(domain))
        elif choice == "delete":
            _safe(lambda: scenario_cmd.interactive_delete(domain))
        elif choice == "reindex":
            _safe(lambda: console.print(
                f"[green]Rebuilt browse-index.[/] {control.rebuild_index(domain).get('message', '')}".rstrip()
            ))


def _compile_one(domain: Optional[str]) -> None:
    import questionary

    slug = scenario_cmd.pick_slug(domain, "Regenerate which scenario?")
    if not slug:
        return
    # Generation is seeded, so a plain regenerate reproduces the same data; reseed for new data.
    reseed = bool(questionary.confirm(
        "Draw a NEW seed for fresh data? (No = reproduce the same data)", default=False
    ).ask())
    label = "Regenerating (new data)" if reseed else "Regenerating"
    with console.status(f"[cyan]{label} [bold]{slug}[/]…", spinner="dots"):
        control.compile_scenario(slug, domain, reseed=reseed)
    console.print(f"[green]{'Regenerated (new data)' if reseed else 'Regenerated'}[/] [bold]{slug}[/].")


def _analyze_menu(domain: Optional[str]) -> None:
    import questionary

    reader = MongoReader()
    if not reader.ping():
        console.print("[red]Mongo unreachable — cannot list runs.[/]")
        return
    runs = reader.list_runs(limit=25)
    reader.close()
    if not runs:
        console.print("[yellow]No runs found.[/]")
        return
    choices = [
        questionary.Choice(
            f"{r.get('run_id')}  [{r.get('scenario_slug') or '?'}]  {r.get('status') or ''}",
            value=r.get("run_id"),
        )
        for r in runs
    ]
    run_id = questionary.select("Analyze which run?", choices=choices).ask()
    if not run_id:
        return
    emit_report(run_id, config.REPORT_BASE / run_id)


def _services_menu(domain: Optional[str]) -> None:
    import questionary

    while True:
        choice = questionary.select(
            "Services —",
            choices=[
                questionary.Choice("Status", "status"),
                questionary.Choice("Start a service", "start"),
                questionary.Choice("Stop a service", "stop"),
                questionary.Choice("← Back", "back"),
            ],
        ).ask()
        if choice in (None, "back"):
            return
        if choice == "status":
            _safe(lambda: services_cmd._cmd_services_status(argparse.Namespace(service=None, json=False)))
        elif choice in ("start", "stop"):
            _safe(lambda: _service_action(choice))


def _service_action(action: str) -> None:
    import questionary

    services = control.service_status()
    keys = [s.get("key") for s in services if s.get("key")]
    if not keys:
        console.print("[yellow]No services reported.[/]")
        return
    key = questionary.select(f"{action.capitalize()} which service?", choices=keys).ask()
    if not key:
        return
    result = control.start_service(key) if action == "start" else control.stop_service(key)
    style = "green" if result.get("ok") else "red"
    console.print(f"[{style}]{result.get('message', 'done')}[/]")


def run_menu(domain: Optional[str] = None) -> int:
    """Top-level interactive loop. Returns a process exit code (0)."""
    import questionary

    console.print("[bold cyan]OpenRide[/] · interactive console  [dim](Ctrl-C to quit)[/]")
    while True:
        try:
            choice = questionary.select(
                "What would you like to do?",
                choices=[
                    questionary.Choice("Run a simulation", "run"),
                    questionary.Choice("Scenarios — construct & maintain", "scenarios"),
                    questionary.Choice("Analyze a past run", "analyze"),
                    questionary.Choice("Services", "services"),
                    questionary.Choice("Quit", "quit"),
                ],
            ).ask()
        except KeyboardInterrupt:
            choice = "quit"

        if choice in (None, "quit"):
            console.print("[dim]Bye.[/]")
            return 0
        if choice == "run":
            _safe(lambda: run_group._cmd_run(_run_namespace(domain)))
        elif choice == "scenarios":
            _scenarios_menu(domain)
        elif choice == "analyze":
            _safe(lambda: _analyze_menu(domain))
        elif choice == "services":
            _services_menu(domain)
