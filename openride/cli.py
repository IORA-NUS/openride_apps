"""Entry point for the unified OpenRide CLI.

A verb-based dispatcher over the whole stack; each verb group registers its own
subparser(s). Run with the pyjupenv interpreter (``python -m openride`` /
``scripts/openride.sh``); scenario/service primitives shell out to the apps venv via
``openride_control.command`` exactly like the dashboard does.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional

from . import analyze_cmd, run_group, scenario_cmd, services_cmd
from .control import ControlError
from .ui import console


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openride",
        description="OpenRide CLI — construct/maintain scenarios, run simulations, analyze runs.",
    )
    sub = parser.add_subparsers(dest="group")
    run_group.register(sub)
    scenario_cmd.register(sub)
    analyze_cmd.register(sub)
    services_cmd.register(sub)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    func = getattr(args, "func", None)
    if func is None:
        # Bare `openride` on a TTY → interactive front-door menu; otherwise print help
        # (keeps scripts/pipes that invoke `openride` with no args fully predictable).
        if sys.stdin.isatty() and sys.stdout.isatty():
            from . import tui

            try:
                return tui.run_menu()
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted.[/]")
                return 130
        parser.print_help()
        return 0

    try:
        return func(args)
    except ControlError as exc:
        # A failed control-plane call (bad spec, protected slug, timeout, …).
        console.print(f"[red]Error:[/] {exc}")
        # Surface the machine-readable code's remedy. Without this the CLI user is
        # told a save was refused and never told the override exists — a guard whose
        # override is invisible gets disabled wholesale (review finding 13).
        if getattr(exc, "code", None) == "COOP_RESET_REQUIRED":
            console.print(
                "[yellow]Hint:[/] re-run with [bold]--allow-cooperation-reset[/] to "
                "clear that structure's cooperation on purpose."
            )
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        return 130
    except (ValueError, FileNotFoundError) as exc:
        # Bad user input: unknown file, malformed JSON, bad --set/--haulier, etc.
        console.print(f"[red]Error:[/] {exc}")
        return 2
    except subprocess.CalledProcessError as exc:
        # e.g. $EDITOR exited non-zero during `scenario edit`.
        console.print(f"[red]Error:[/] command failed (exit {exc.returncode}): {exc.cmd}")
        return 1
    except Exception as exc:  # noqa: BLE001 — last-resort: a CLI should not dump a traceback.
        # Set OPENRIDE_DEBUG=1 to re-raise and see the full traceback while developing.
        if os.environ.get("OPENRIDE_DEBUG"):
            raise
        console.print(
            f"[red]Unexpected error:[/] {type(exc).__name__}: {exc}\n"
            "[dim]Re-run with OPENRIDE_DEBUG=1 for a full traceback.[/]"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
