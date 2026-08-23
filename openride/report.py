"""Final analysis: build a KPI summary from Mongo, render rich tables, export report files."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config
from .mongo_reader import BreakdownSnapshot, MongoReader, fleet_totals

# Per-haulier columns shown in the *terminal* table, in order: (entity key, header, formatter).
# Kept compact so it fits an 80-col terminal; the CSV / JSON / markdown carry the full set.
_HAULIER_COLUMNS = [
    ("name", "Company", str),
    ("num_trucks", "Trucks", lambda v: f"{int(v or 0):,}"),
    ("num_orders_completed", "Orders", lambda v: f"{int(v or 0):,}"),
    ("empty_km", "Deadhead km", lambda v: f"{float(v or 0):,.0f}"),
    ("total_km", "Total km", lambda v: f"{float(v or 0):,.0f}"),
    ("orders_per_day", "Ord/day", lambda v: f"{float(v or 0):.2f}"),
    ("dual_cycle_rate", "DualCyc", lambda v: f"{float(v or 0) * 100:.1f}%"),
]


# Scalar (point-in-time) KPIs: metric key -> (friendly label, formatter). Rendered in this
# order; any scalar metric not listed here is appended with its raw key.
_SCALAR_KPI_SPEC: list[tuple[str, str, Any]] = [
    ("empty_distance_ratio", "Fleet deadhead ratio", lambda v: f"{v * 100:.1f}%"),
    ("avg_empty_distance_km", "Avg empty distance / trip", lambda v: f"{v:.2f} km"),
    ("dual_cycle_rate", "Dual-cycle rate", lambda v: f"{v * 100:.1f}%"),
    ("orders_per_truck", "Orders per truck", lambda v: f"{v:.2f}"),
    ("orders_per_truck_day", "Orders per truck-day", lambda v: f"{v:.2f}"),
    ("avg_truck_active_hours", "Avg truck active hours", lambda v: f"{v:.2f} h"),
    ("avg_queue_wait_seconds", "Avg gate queue wait", lambda v: f"{v / 60:.1f} min"),
    ("avg_idle_time_seconds", "Avg truck idle time", lambda v: f"{v / 60:.1f} min"),
    ("peak_queue_length", "Peak gate queue length", lambda v: f"{v:,.0f}"),
    ("active_haul_trips", "Active haul trips (at run end)", lambda v: f"{v:,.0f}"),
    ("active_orders", "Active orders (at run end)", lambda v: f"{v:,.0f}"),
    ("num_hauls_completed", "Hauls completed (window)", lambda v: f"{v:,.0f}"),
    ("num_orders_completed", "Orders completed (window)", lambda v: f"{v:,.0f}"),
]


def _scalar_rows(scalar: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered (label, formatted-value) rows for the point-in-time scalar KPIs."""
    rows: list[tuple[str, str]] = []
    seen = set()
    for key, label, fmt in _SCALAR_KPI_SPEC:
        if key in scalar and scalar[key] is not None:
            seen.add(key)
            try:
                rows.append((label, fmt(scalar[key])))
            except (TypeError, ValueError):
                rows.append((label, str(scalar[key])))
    # Any scalar metric we didn't have a spec for — show raw so nothing is silently dropped.
    for key in sorted(scalar):
        if key not in seen and scalar[key] is not None:
            rows.append((key, f"{scalar[key]:,.4g}"))
    return rows


def _haulier_name(e: dict[str, Any]) -> str:
    return str(e.get("haulier_name") or e.get("name") or e.get("id") or "—")


def build_summary(reader: MongoReader, run_id: str) -> dict[str, Any]:
    haulier = reader.latest_breakdown(run_id, "haulier")
    truck = reader.latest_breakdown(run_id, "truck")
    fleet = fleet_totals(haulier.entities)
    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "final": haulier.final,
        "sim_clock": _iso(haulier.sim_clock),
        "fleet": fleet,
        "hauliers": _sorted_hauliers(haulier),
        "trucks": truck.entities,
        "num_trucks_reported": len(truck.entities),
        "scalar_kpis": reader.latest_scalar_kpis(run_id),
        "run_config": _clean(reader.run_config(run_id)),
    }


def _sorted_hauliers(snap: BreakdownSnapshot) -> list[dict[str, Any]]:
    rows = []
    for e in snap.entities:
        row = dict(e)
        row["name"] = _haulier_name(e)
        rows.append(row)
    rows.sort(key=lambda r: r.get("num_orders_completed") or 0, reverse=True)
    return rows


def _iso(v: Any) -> str | None:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v is not None else None


def _clean(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop Mongo's ObjectId/_etag noise so the report serializes cleanly."""
    out = {}
    for k, v in (doc or {}).items():
        if k.startswith("_"):
            continue
        try:
            json.dumps(v)
            out[k] = v
        except TypeError:
            out[k] = str(v)
    return out


# -- rendering ------------------------------------------------------------
def render_summary(console: Console, summary: dict[str, Any]) -> None:
    fleet = summary["fleet"]
    final_tag = "[green]FINAL[/]" if summary["final"] else "[yellow]live snapshot (not final)[/]"
    head = (
        f"[bold]{summary['run_id']}[/]   {final_tag}\n"
        f"Hauliers: {fleet.get('num_hauliers', 0)}   "
        f"Trucks: {fleet.get('num_trucks', 0):,}   "
        f"Orders completed: {fleet.get('num_orders_completed', 0):,}"
    )
    console.print(Panel(head, title="OpenRide run summary", border_style="cyan"))

    fleet_tbl = Table(title="Fleet KPIs", header_style="bold cyan", expand=False)
    fleet_tbl.add_column("Metric")
    fleet_tbl.add_column("Value", justify="right")
    fleet_tbl.add_row("Trucks", f"{fleet.get('num_trucks', 0):,}")
    fleet_tbl.add_row("Hauliers", f"{fleet.get('num_hauliers', 0):,}")
    fleet_tbl.add_row("Orders completed", f"{fleet.get('num_orders_completed', 0):,}")
    fleet_tbl.add_row("Deadhead / empty distance (km)", f"{fleet.get('empty_km', 0):,.0f}")
    fleet_tbl.add_row("Loaded distance (km)", f"{fleet.get('loaded_km', 0):,.0f}")
    fleet_tbl.add_row("Total distance (km)", f"{fleet.get('total_km', 0):,.0f}")
    fleet_tbl.add_row("Empty ratio (deadhead share)", f"{fleet.get('empty_ratio', 0) * 100:.1f}%")
    fleet_tbl.add_row("Orders / truck", f"{fleet.get('orders_per_truck', 0):.2f}")
    fleet_tbl.add_row("Total active hours", f"{fleet.get('active_hours', 0):,.0f}")
    fleet_tbl.add_row("Dual-cycle rate", f"{fleet.get('dual_cycle_rate', 0) * 100:.1f}%")
    fleet_tbl.add_row(
        "Dual cycles / chain opportunities",
        f"{fleet.get('dual_cycle_count', 0):,} / {fleet.get('chain_opportunities', 0):,}",
    )
    console.print(fleet_tbl)

    if summary["hauliers"]:
        htbl = Table(title="Per-company breakdown", header_style="bold magenta")
        for _key, header, _fmt in _HAULIER_COLUMNS:
            htbl.add_column(header, justify="left" if header == "Company" else "right")
        for e in summary["hauliers"]:
            htbl.add_row(*[fmt(e.get(key)) for key, _h, fmt in _HAULIER_COLUMNS])
        console.print(htbl)
    else:
        console.print("[yellow]No per-company breakdown found yet for this run.[/]")

    rows = _scalar_rows(summary["scalar_kpis"])
    if rows:
        ktbl = Table(
            title="Operational KPIs (point-in-time snapshot at run end)",
            header_style="bold green",
        )
        ktbl.add_column("Metric")
        ktbl.add_column("Value", justify="right")
        for label, value in rows:
            ktbl.add_row(label, value)
        console.print(ktbl)


# -- export ---------------------------------------------------------------
def write_report(summary: dict[str, Any], report_dir: Path) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    json_path = report_dir / "report.json"
    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    paths["json"] = json_path

    # Per-haulier CSV (the most diff/spreadsheet-friendly artifact).
    csv_path = report_dir / "report_hauliers.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        cols = [
            "name", "haulier_id", "num_trucks", "num_orders_completed", "empty_ratio",
            "empty_km", "loaded_km", "total_km", "orders_per_day", "active_hours",
            "dual_cycle_rate", "dual_cycle_count", "chain_opportunities",
        ]
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for e in summary["hauliers"]:
            writer.writerow(e)
    paths["csv"] = csv_path

    paths["markdown"] = _write_markdown(summary, report_dir / "report.md")
    return paths


def _write_markdown(summary: dict[str, Any], path: Path) -> Path:
    f = summary["fleet"]
    lines = [
        f"# OpenRide run report — `{summary['run_id']}`",
        "",
        f"- Generated: {summary['generated_at']}",
        f"- Source: {'final recompute' if summary['final'] else 'live snapshot (not final)'}",
        f"- Sim clock: {summary['sim_clock']}",
        "",
        "## Fleet KPIs",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Trucks | {f.get('num_trucks', 0):,} |",
        f"| Hauliers | {f.get('num_hauliers', 0):,} |",
        f"| Orders completed | {f.get('num_orders_completed', 0):,} |",
        f"| Deadhead / empty distance (km) | {f.get('empty_km', 0):,.0f} |",
        f"| Loaded km | {f.get('loaded_km', 0):,.0f} |",
        f"| Total km | {f.get('total_km', 0):,.0f} |",
        f"| Empty ratio (deadhead share) | {f.get('empty_ratio', 0) * 100:.1f}% |",
        f"| Orders / truck | {f.get('orders_per_truck', 0):.2f} |",
        f"| Total active hours | {f.get('active_hours', 0):,.0f} |",
        f"| Dual-cycle rate | {f.get('dual_cycle_rate', 0) * 100:.1f}% |",
        f"| Dual cycles / chain opportunities | {f.get('dual_cycle_count', 0):,} / {f.get('chain_opportunities', 0):,} |",
        "",
        "## Per-company breakdown",
        "",
        "| Company | Trucks | Orders | Deadhead km | Total km | Ord/day | DualCyc |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for e in summary["hauliers"]:
        lines.append(
            f"| {e.get('name')} | {int(e.get('num_trucks') or 0):,} | "
            f"{int(e.get('num_orders_completed') or 0):,} | "
            f"{float(e.get('empty_km') or 0):,.0f} | "
            f"{float(e.get('total_km') or 0):,.0f} | "
            f"{float(e.get('orders_per_day') or 0):.2f} | "
            f"{float(e.get('dual_cycle_rate') or 0) * 100:.1f}% |"
        )

    scalar_rows = _scalar_rows(summary["scalar_kpis"])
    if scalar_rows:
        lines += [
            "",
            "## Operational KPIs",
            "",
            "_Point-in-time snapshot at run end (windowed), not run totals._",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
        lines += [f"| {label} | {value} |" for label, value in scalar_rows]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
