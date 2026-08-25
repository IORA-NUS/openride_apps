"""``openride services ...`` and ``openride solver list`` — host services + solver discovery.

Thin, friendly wrappers over the same control-plane the dashboard's Services page uses.
"""

from __future__ import annotations

import json
import sys

from rich.table import Table

from . import control
from .ui import console


def _cmd_services_status(args) -> int:
    services = control.service_status(args.service)
    if args.json:
        json.dump(services, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if not services:
        console.print("[yellow]No services reported.[/]")
        return 0
    tbl = Table(title="OpenRide services", header_style="bold cyan")
    tbl.add_column("Service")
    tbl.add_column("State")
    tbl.add_column("Detail")
    for s in services:
        state = str(s.get("state") or s.get("status") or "")
        color = {"running": "green", "active": "green", "stopped": "red", "inactive": "red"}.get(
            state, "yellow"
        )
        tbl.add_row(
            str(s.get("key") or s.get("service") or ""),
            f"[{color}]{state}[/]",
            str(s.get("message") or s.get("detail") or ""),
        )
    console.print(tbl)
    return 0


def _cmd_services_start(args) -> int:
    result = control.start_service(args.service, with_dependencies=not args.no_deps)
    ok = bool(result.get("ok"))
    style = "green" if ok else "red"
    console.print(f"[{style}]{result.get('message', 'done')}[/]")
    return 0 if ok else 1


def _cmd_services_stop(args) -> int:
    result = control.stop_service(args.service, force=args.force)
    ok = bool(result.get("ok"))
    style = "green" if ok else "red"
    console.print(f"[{style}]{result.get('message', 'done')}[/]")
    return 0 if ok else 1


def _cmd_solver_list(args) -> int:
    info = control.known_solvers()
    if args.json:
        json.dump(info, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    tbl = Table(title="Assignment solvers", header_style="bold cyan")
    tbl.add_column("Strategy")
    tbl.add_column("Default", justify="center")
    for s in info.get("solvers", []):
        tbl.add_row(s, "✓" if s == info.get("default") else "")
    console.print(tbl)
    console.print(
        "[dim]Select one per run with[/] [bold]openride run <slug> --solver <strategy>[/] "
        "[dim](same scenario, different matcher).[/]"
    )
    return 0


def register(sub) -> None:
    svc = sub.add_parser("services", help="Inspect / start / stop host services")
    ssub = svc.add_subparsers(dest="services_cmd", required=True)

    st = ssub.add_parser("status", help="Show service states")
    st.add_argument("--service", default=None, help="Only this service key (faster)")
    st.add_argument("--json", action="store_true", help="Emit raw JSON")
    st.set_defaults(func=_cmd_services_status)

    start = ssub.add_parser("start", help="Start a service")
    start.add_argument("service")
    start.add_argument("--no-deps", action="store_true", help="Do not start dependencies first")
    start.set_defaults(func=_cmd_services_start)

    stop = ssub.add_parser("stop", help="Stop a service")
    stop.add_argument("service")
    stop.add_argument("--force", action="store_true")
    stop.set_defaults(func=_cmd_services_stop)

    sol = sub.add_parser("solver", help="Assignment solver strategies")
    solsub = sol.add_subparsers(dest="solver_cmd", required=True)
    sl = solsub.add_parser("list", help="List runtime-selectable solver strategies")
    sl.add_argument("--json", action="store_true", help="Emit raw JSON")
    sl.set_defaults(func=_cmd_solver_list)
