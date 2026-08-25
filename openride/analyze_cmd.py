"""``openride analyze <run_id>`` and ``openride runs list`` — inspect finished runs.

``analyze`` is the old ``--report-only`` path: read a run's KPIs straight from Mongo,
render the fleet + per-company tables, and (re)write the report under ``output/<run_id>/``.
``runs list`` is run discovery — so you don't have to already know a run id.
"""

from __future__ import annotations

import json
import sys

from rich.table import Table

from . import config
from .mongo_reader import MongoReader
from .ui import console, emit_report


def _cmd_analyze(args) -> int:
    report_dir = config.REPORT_BASE / args.run_id
    ok = emit_report(args.run_id, report_dir)
    return 0 if ok else 1


def _cmd_runs_list(args) -> int:
    reader = MongoReader()
    if not reader.ping():
        console.print("[red]Mongo unreachable — cannot list runs.[/]")
        return 1
    runs = reader.list_runs(limit=args.limit, scenario=args.scenario)
    reader.close()

    if args.json:
        json.dump(runs, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not runs:
        console.print("[yellow]No runs found.[/]")
        return 0

    tbl = Table(title="Recent runs", header_style="bold cyan")
    tbl.add_column("Run id")
    tbl.add_column("Scenario")
    tbl.add_column("Name")
    tbl.add_column("Status")
    tbl.add_column("Trucks", justify="right")
    tbl.add_column("Updated")
    for r in runs:
        status = str(r.get("status") or "")
        color = {"success": "green", "In Progress": "yellow", "failed": "red"}.get(status, "white")
        tbl.add_row(
            str(r.get("run_id", "")),
            str(r.get("scenario_slug") or ""),
            str(r.get("name") or ""),
            f"[{color}]{status}[/]",
            f"{r.get('trucks') or ''}",
            str(r.get("updated") or "")[:19],
        )
    console.print(tbl)
    console.print("[dim]Analyze one with[/] [bold]openride analyze <run id>[/].")
    return 0


def register(sub) -> None:
    ana = sub.add_parser("analyze", help="Build/print the KPI report for a finished run")
    ana.add_argument("run_id")
    ana.set_defaults(func=_cmd_analyze)

    runs = sub.add_parser("runs", help="List past runs")
    rsub = runs.add_subparsers(dest="runs_cmd", required=True)
    lst = rsub.add_parser("list", help="List recent runs (newest first)")
    lst.add_argument("--limit", type=int, default=25, help="How many runs to show")
    lst.add_argument("--scenario", default=None, help="Filter by scenario slug")
    lst.add_argument("--json", action="store_true", help="Emit raw JSON")
    lst.set_defaults(func=_cmd_runs_list)
