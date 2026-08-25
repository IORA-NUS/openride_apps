"""Shared terminal rendering for the OpenRide CLI (one Console, common tables)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config, report
from .mongo_reader import MongoReader

console = Console()


def render_scenarios(scenarios: list[dict[str, Any]]) -> None:
    if not scenarios:
        console.print("[yellow]No scenarios on disk.[/] Create one with [bold]openride scenario new[/].")
        return
    tbl = Table(title="Scenarios on disk", header_style="bold cyan")
    tbl.add_column("Slug")
    tbl.add_column("Name")
    tbl.add_column("Days", justify="right")
    tbl.add_column("Trucks", justify="right")
    tbl.add_column("Orders", justify="right")
    tbl.add_column("Facilities", justify="right")
    for s in scenarios:
        agents = s.get("agents") or {}
        tbl.add_row(
            str(s.get("slug", "?")),
            str(s.get("name", "")),
            str(s.get("simulationDays", "")),
            f"{agents.get('truck', '') or ''}",
            f"{agents.get('order', '') or ''}",
            f"{agents.get('facility', '') or ''}",
        )
    console.print(tbl)


def render_scenario_detail(detail: dict[str, Any]) -> None:
    # The recipe fields (solver, hauliers, …) live under editForm; the list/header fields
    # (counts, name) live at the top level. Read each from where it actually is.
    form = detail.get("editForm") if isinstance(detail.get("editForm"), dict) else {}
    agents = detail.get("agents") or {}
    hauliers = detail.get("hauliers") or form.get("hauliers") or []
    solver = detail.get("solver") or form.get("solver")
    lines = [
        f"[bold]{detail.get('name', detail.get('slug'))}[/]  [dim]({detail.get('slug')})[/]",
        f"days={detail.get('simulationDays', '?')}  "
        f"trucks={agents.get('truck', '?')}  orders={agents.get('order', '?')}  "
        f"facilities={agents.get('facility', '?')}",
        f"solver={solver or 'scenario default'}",
    ]
    console.print(Panel("\n".join(lines), border_style="cyan", title="scenario"))
    if hauliers:
        tbl = Table(title="Hauliers", header_style="bold magenta")
        tbl.add_column("Id")
        tbl.add_column("Name")
        tbl.add_column("Fleet %", justify="right")
        tbl.add_column("Order %", justify="right")
        for h in hauliers:
            tbl.add_row(
                str(h.get("id", "")),
                str(h.get("name", "")),
                f"{h.get('fleet_share', '')}",
                f"{h.get('order_share', '')}",
            )
        console.print(tbl)


def emit_report(run_id: str, report_dir: Path) -> bool:
    """Build, render, and persist the KPI report for a run. Returns True if data was found."""
    reader = MongoReader()
    if not reader.ping():
        console.print("[red]Mongo unreachable — cannot read KPI results.[/]")
        return False
    summary = report.build_summary(reader, run_id)
    reader.close()

    has_data = bool(summary["hauliers"]) or bool(summary["scalar_kpis"]) or bool(summary["run_config"])
    if not has_data:
        console.print(
            f"[yellow]No KPI data found for run [bold]{run_id}[/] — nothing to report.[/]\n"
            "[dim]Either the run id is unknown, or the run was too small/short to complete a haul.[/]"
        )
        return False

    console.rule("[bold]Analysis")
    report.render_summary(console, summary)
    paths = report.write_report(summary, report_dir)
    files = "\n".join(f"  • {k}: {v}" for k, v in paths.items())
    console.print(Panel(f"Report written:\n{files}", border_style="green"))
    return True
