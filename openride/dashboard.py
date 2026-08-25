"""Live terminal dashboard for a running headless simulation (rich.Live).

Progress (step/day/wall) is parsed from the sim's streamed stdout; live KPI tiles come from
the ``kpi_breakdown`` snapshots the analytics agent persists to Mongo during the run.
"""

from __future__ import annotations

import time

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

from . import config
from .mongo_reader import MongoReader, fleet_totals
from .runner import SimulationRunner


# Lines that flood the sim's stdout but say nothing useful in a tail view.
_NOISE = ("agent_stat[", "entering market", "exiting_market", "entering_market")


def _interesting(lines: list[str], n: int = 7) -> list[str]:
    kept = [ln for ln in lines if not any(tok in ln for tok in _NOISE)]
    return kept[-n:] if kept else lines[-n:]


def _fmt_dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def day_numbers(snap: dict) -> tuple[int, int]:
    """1-indexed current day and total day count. Day 1 is the first sim day (not day 0)."""
    sim_days = float(snap.get("sim_days") or 0.0)
    step = int(snap.get("step") or 0)
    total_steps = int(snap.get("total_steps") or 0)
    total_days = (sim_days * total_steps / step) if step > 0 else 0.0
    total_int = max(1, round(total_days)) if total_days > 0 else 0
    cur = int(sim_days) + 1  # 1-indexed
    if total_int:
        cur = min(cur, total_int)
    return cur, total_int


def _tiles(snap: dict, live_fleet: dict | None) -> Table:
    t = Table.grid(expand=True, padding=(0, 2))
    for _ in range(4):
        t.add_column(justify="center", ratio=1)

    def cell(label: str, value: str, color: str = "white") -> Panel:
        body = Align.center(f"[bold {color}]{value}[/]\n[dim]{label}[/]")
        return Panel(body, border_style="grey37", padding=(0, 1))

    f = live_fleet or {}

    def km(key):
        v = f.get(key)
        return f"{v:,.0f}" if v is not None else "—"

    def pct(key):
        v = f.get(key)
        return f"{v * 100:.1f}%" if v is not None else "—"

    def num(key, fmt="{:.2f}"):
        v = f.get(key)
        return fmt.format(v) if v is not None else "—"

    trucks = snap.get("trucks") or f.get("num_trucks") or 0
    cur_day, total_day = day_numbers(snap)
    day_str = f"{cur_day} / {total_day}" if total_day else str(cur_day)

    # Row 1 — counts (cyan/green)
    t.add_row(
        cell("trucks", f"{int(trucks):,}", "cyan"),
        cell("orders spawned", f"{int(snap.get('orders') or 0):,}", "cyan"),
        cell("facilities", f"{int(snap.get('facilities') or 0):,}", "cyan"),
        cell("hauls completed", num("num_orders_completed", "{:,.0f}"), "green"),
    )
    # Row 2 — distances (yellow)
    t.add_row(
        cell("deadhead (km)", km("empty_km"), "yellow"),
        cell("loaded (km)", km("loaded_km"), "yellow"),
        cell("total (km)", km("total_km"), "yellow"),
        cell("empty ratio", pct("empty_ratio"), "yellow"),
    )
    # Row 3 — efficiency + clock (yellow/magenta)
    t.add_row(
        cell("orders / truck", num("orders_per_truck"), "yellow"),
        cell("active hours", num("active_hours", "{:,.0f}"), "yellow"),
        cell("dual-cycle %", pct("dual_cycle_rate"), "yellow"),
        cell("sim day", day_str, "magenta"),
    )
    return t


def _company_table(hauliers: list[dict], limit: int = 8) -> Table:
    tbl = Table(title="Top companies (live)", header_style="bold magenta", expand=True)
    tbl.add_column("Company")
    tbl.add_column("Trucks", justify="right")
    tbl.add_column("Hauls", justify="right")
    tbl.add_column("Deadhead km", justify="right")
    tbl.add_column("Ord/day", justify="right")
    rows = sorted(hauliers, key=lambda e: e.get("num_orders_completed") or 0, reverse=True)
    for e in rows[:limit]:
        name = str(e.get("haulier_name") or e.get("id") or "—")
        tbl.add_row(
            name,
            f"{int(e.get('num_trucks') or 0):,}",
            f"{int(e.get('num_orders_completed') or 0):,}",
            f"{float(e.get('empty_km') or 0):,.0f}",
            f"{float(e.get('orders_per_day') or 0):.2f}",
        )
    if not rows:
        tbl.add_row("[dim]waiting for first snapshot…[/]", "", "", "", "")
    return tbl


def _render(runner: SimulationRunner, snap: dict, live_fleet: dict, hauliers: list[dict],
            progress: Progress, task_id) -> Group:
    status = snap["status"]
    status_color = {
        "running": "green", "starting": "yellow", "completed": "cyan",
        "failed": "red", "stopped": "yellow",
    }.get(status, "white")
    header = (
        f"[bold cyan]OpenRide[/] headless  ·  run [bold]{runner.run_id}[/]\n"
        f"scenario [bold]{runner.scenario}[/]   solver [bold]{runner.solver or 'scenario default'}[/]"
        f"   headless [bold]{'on' if runner.headless else 'off'}[/]   "
        f"status [bold {status_color}]{status.upper()}[/]"
    )

    frac = snap["fraction"]
    progress.update(task_id, completed=min(100.0, frac * 100))
    wall = snap["wall_seconds"]
    eta = (wall / frac - wall) if frac > 0.01 else 0.0
    cur_day, total_day = day_numbers(snap)
    day_str = f"{cur_day}/{total_day}" if total_day else str(cur_day)
    prog_line = (
        f"step [bold]{snap['step']:,}[/]/{snap['total_steps']:,}   "
        f"day [bold]{day_str}[/]   "
        f"eta [bold]{_fmt_dur(eta)}[/]"
    )

    log_tail = "\n".join(_interesting(snap["lines"])) or "[dim](no output yet)[/]"

    return Group(
        Panel(header, border_style="cyan"),
        _tiles(snap, live_fleet),
        progress,
        Align.center(prog_line),
        _company_table(hauliers, limit=6),
        Panel(log_tail, title="sim log (tail)", border_style="grey23", height=6),
        Align.center("[dim]Ctrl+C to stop the run[/]"),
    )


def _frame_signature(snap: dict, live_fleet: dict, log_tail: list[str]) -> tuple:
    """Cheap fingerprint of what's visible — repaint only when this changes (anti-flicker)."""
    return (
        snap["step"],
        snap["status"],
        round(snap["sim_days"], 2),
        snap["trucks"],
        live_fleet.get("num_orders_completed"),
        round(float(live_fleet.get("empty_ratio") or 0.0), 4),
        len(live_fleet) and round(float(live_fleet.get("orders_per_truck") or 0.0), 3),
        tuple(log_tail),
    )


def run_live(runner: SimulationRunner, console: Console) -> str:
    """Drive the live dashboard until the run finishes. Returns the final status string."""
    reader = MongoReader()
    progress = Progress(
        TextColumn("[bold]progress"),
        BarColumn(bar_width=None),
        TextColumn("{task.percentage:>5.1f}%"),
        expand=True,
    )
    task_id = progress.add_task("run", total=100.0)

    live_fleet: dict = {}
    hauliers: list[dict] = []
    last_poll = 0.0
    last_sig: tuple | None = None
    last_paint = 0.0
    # Gentle, fixed repaint floor — avoids the high-frequency repaints that read as flicker.
    min_paint_gap = max(0.3, float(config.LIVE_PAINT_MIN_GAP_SECONDS))

    try:
        # screen=True → render on the alternate buffer (atomic in-place repaint, no
        # clear-then-redraw flash). auto_refresh=False → we repaint only on real change.
        # vertical_overflow="crop" → tall content is clipped, never scroll-flickers.
        with Live(
            console=console,
            screen=True,
            auto_refresh=False,
            vertical_overflow="crop",
        ) as live:
            while True:
                snap = runner.state.snapshot()
                now = time.time()
                if now - last_poll >= config.LIVE_POLL_SECONDS:
                    last_poll = now
                    breakdown = reader.latest_breakdown(runner.run_id, "haulier")
                    if breakdown.entities:
                        hauliers = breakdown.entities
                        live_fleet = fleet_totals(hauliers)

                terminal = snap["status"] in ("completed", "failed", "stopped") and not runner.is_alive()
                sig = _frame_signature(snap, live_fleet, _interesting(snap["lines"]))
                # Repaint only when the frame changed AND the paint floor has elapsed (or we're
                # finishing) — identical frames are never redrawn, so the view sits still.
                if terminal or (sig != last_sig and now - last_paint >= min_paint_gap):
                    last_sig = sig
                    last_paint = now
                    live.update(
                        _render(runner, snap, live_fleet, hauliers, progress, task_id),
                        refresh=True,
                    )
                if terminal:
                    break
                time.sleep(0.2)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping simulation…[/]")
        runner.stop()
        runner.wait(timeout=25)
    finally:
        reader.close()

    return runner.state.snapshot()["status"]
