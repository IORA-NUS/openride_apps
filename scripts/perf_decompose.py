#!/usr/bin/env python3
"""perf_decompose.py — step-level wall-time decomposition for completed OpenRide runs.

Read-only analysis tool. Given one or more run_ids under apps/output/<run_id>/, it
parses summary.json + sim.log and prints:

  - a per-run header (wall_seconds, steps, orders completed, empty_ratio, sleeping
    agent stats)
  - a bucketed step-wall-time table (idle / light / heavy, by stepping_agents count)
  - a cross-run comparison (wall deltas; min/median/max when >=3 runs of the same
    scenario are given, per CLAUDE.md section 17)

Log format this script depends on (verified against apps/output/run_20260906_164919/
sim.log, a completed 2592-step run):

  1. Step markers, one pair (agent_scheduler + service_scheduler) per step:
       "... INFO root agent_scheduler Step: N"
       "... INFO root service_scheduler Step: N"
     Only the agent_scheduler line is used for step-boundary timing.

  2. Agent-stat dicts, e.g.:
       "... INFO root self.agent_stat[self.time] = {'completed': 1060, ...,
        'stepping_agents': 0, 'total_agents': 1060}"
     Both schedulers emit one of these per step. Empirically the service_scheduler's
     total_agents is always a small constant (3 on every run inspected: assignment_main,
     analytics_000, order_lifecycle_main) while the agent_scheduler's total_agents is in
     the hundreds/thousands (trucks + facilities). We disambiguate with a threshold
     (AGENT_STAT_MIN_TOTAL) rather than position in the log, because the two agent_stat
     lines for a given step are NOT in a fixed order relative to each other (verified:
     step 0 emits the large-total stat first, step 1 emits the small-total stat first).

Usage:
    venv/bin/python scripts/perf_decompose.py <run_id> [<run_id> ...]
"""

from __future__ import annotations

import ast
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "apps" / "output"

# Agent-stat dicts with total_agents below this are the service_scheduler's own
# bookkeeping (observed constant at 3: assignment_main/analytics_000/order_lifecycle_main),
# not the fleet-wide agent_scheduler stat we want. Observed agent_scheduler totals ranged
# 839-1060 on the reference runs; 50 leaves comfortable headroom on both sides.
AGENT_STAT_MIN_TOTAL = 50

STEP_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) INFO root (agent_scheduler|service_scheduler) Step: (\d+)\s*$"
)
AGENT_STAT_RE = re.compile(r"self\.agent_stat\[self\.time\] = (\{.*\})\s*$")

TS_FMT = "%Y-%m-%d %H:%M:%S,%f"

BUCKETS = [
    ("idle (0)", 0, 0),
    ("light (1-200)", 1, 200),
    ("heavy (>200)", 201, float("inf")),
]


@dataclass
class StepRecord:
    step: int
    ts: datetime
    stepping_agents: Optional[int] = None
    total_agents: Optional[int] = None
    sleeping: Optional[int] = None


@dataclass
class RunData:
    run_id: str
    run_dir: Path
    summary: Optional[dict] = None
    summary_error: Optional[str] = None
    steps: list = field(default_factory=list)  # list[StepRecord]
    log_error: Optional[str] = None
    unmatched_stat_steps: int = 0


def parse_timestamp(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


def load_summary(run_dir: Path) -> tuple[Optional[dict], Optional[str]]:
    path = run_dir / "summary.json"
    if not path.exists():
        return None, f"summary.json not found at {path}"
    try:
        with path.open() as f:
            return json.load(f), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read/parse summary.json: {exc}"


def load_steps(run_dir: Path) -> tuple[list, Optional[str]]:
    """Parse sim.log into one StepRecord per agent_scheduler 'Step: N' marker,
    with the fleet-wide agent_stat dict (if found before the next marker) attached.
    """
    path = run_dir / "sim.log"
    if not path.exists():
        return [], f"sim.log not found at {path}"

    records: list[StepRecord] = []
    current: Optional[StepRecord] = None

    try:
        with path.open(errors="replace") as f:
            for line in f:
                m = STEP_LINE_RE.match(line)
                if m:
                    ts_str, scheduler, step_str = m.groups()
                    if scheduler != "agent_scheduler":
                        continue
                    try:
                        ts = parse_timestamp(ts_str)
                    except ValueError:
                        continue
                    current = StepRecord(step=int(step_str), ts=ts)
                    records.append(current)
                    continue

                m = AGENT_STAT_RE.search(line)
                if m and current is not None and current.stepping_agents is None:
                    try:
                        d = ast.literal_eval(m.group(1))
                    except (ValueError, SyntaxError):
                        continue
                    total = d.get("total_agents")
                    if isinstance(total, int) and total >= AGENT_STAT_MIN_TOTAL:
                        current.stepping_agents = d.get("stepping_agents")
                        current.total_agents = total
                        current.sleeping = d.get("sleeping")
    except OSError as exc:
        return [], f"could not read sim.log: {exc}"

    if not records:
        return [], "no 'agent_scheduler Step: N' lines found in sim.log"

    return records, None


def load_run(run_id: str) -> RunData:
    run_dir = OUTPUT_DIR / run_id
    rd = RunData(run_id=run_id, run_dir=run_dir)
    if not run_dir.exists():
        rd.summary_error = f"run directory not found: {run_dir}"
        rd.log_error = rd.summary_error
        return rd
    rd.summary, rd.summary_error = load_summary(run_dir)
    rd.steps, rd.log_error = load_steps(run_dir)
    rd.unmatched_stat_steps = sum(1 for s in rd.steps if s.stepping_agents is None)
    return rd


def paired_deltas(steps: list) -> list[tuple[StepRecord, StepRecord, float]]:
    """Consecutive (N, N+1) pairs only, delta in seconds."""
    by_step = {s.step: s for s in steps}
    pairs = []
    for s in steps:
        nxt = by_step.get(s.step + 1)
        if nxt is None:
            continue
        delta = (nxt.ts - s.ts).total_seconds()
        pairs.append((s, nxt, delta))
    return pairs


def pctl(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def fmt(v, digits=1):
    if v is None or (isinstance(v, float) and (v != v)):
        return "n/a"
    return f"{v:.{digits}f}"


def print_run_report(rd: RunData) -> None:
    print("=" * 78)
    print(f"run_id: {rd.run_id}")
    print("=" * 78)

    if rd.summary_error:
        print(f"  [summary.json] {rd.summary_error}")
    else:
        s = rd.summary
        fleet = s.get("fleet", {}) or {}
        print(f"  scenario:           {s.get('scenario')}")
        print(f"  status / ok:        {s.get('status')} / {s.get('ok')}")
        print(f"  wall_seconds:       {fmt(s.get('wall_seconds'))}")
        print(f"  steps / total_steps:{s.get('steps')} / {s.get('total_steps')}")
        print(f"  sim_days:           {s.get('sim_days')}")
        print(f"  orders_completed:   {fleet.get('num_orders_completed')}")
        print(f"  empty_ratio:        {fmt(fleet.get('empty_ratio'), 4)}")
        print(f"  dual_cycle_rate:    {fmt(fleet.get('dual_cycle_rate'), 4)}")

    if rd.log_error:
        print(f"  [sim.log] {rd.log_error}")
        return

    n_markers = len(rd.steps)
    sample_ok = n_markers - rd.unmatched_stat_steps
    print(f"  agent_scheduler step markers found: {n_markers}")
    if rd.unmatched_stat_steps:
        print(
            f"  WARNING: {rd.unmatched_stat_steps} step(s) had no matching "
            f"fleet-wide agent_stat line (total_agents >= {AGENT_STAT_MIN_TOTAL}) "
            f"before the next step marker — those steps are excluded from the "
            f"bucket table below."
        )
    print(f"  usable agent_stat samples: {sample_ok} (project convention: assert == 2611 before quoting a mean)")

    sleeping_vals = [s.sleeping for s in rd.steps if s.sleeping is not None]
    if sleeping_vals:
        print(
            f"  sleeping agents: mean={fmt(statistics.mean(sleeping_vals), 2)} "
            f"max={max(sleeping_vals)}"
        )
    else:
        print("  sleeping agents: n/a (no matched agent_stat samples)")

    pairs = paired_deltas(rd.steps)
    if not pairs:
        print("  [step timing] no consecutive (N, N+1) step-marker pairs found — cannot build wall-time table")
        return

    total_paired_wall = sum(delta for _, _, delta in pairs)
    print(f"\n  paired steps: {len(pairs)}   total paired wall: {fmt(total_paired_wall, 1)} s", end="")
    if rd.summary and rd.summary.get("wall_seconds"):
        print(f"   (summary.wall_seconds: {fmt(rd.summary['wall_seconds'], 1)} s)")
    else:
        print()

    print(
        f"\n  {'bucket':<16}{'n':>7}{'mean ms':>10}{'p50 ms':>9}{'p90 ms':>9}"
        f"{'total s':>10}{'share':>8}"
    )
    print("  " + "-" * 68)
    unbucketed = 0
    for label, lo, hi in BUCKETS:
        durs_ms = []
        for s, _nxt, delta in pairs:
            sa = s.stepping_agents
            if sa is None:
                continue
            if lo <= sa <= hi:
                durs_ms.append(delta * 1000.0)
        if not durs_ms:
            print(f"  {label:<16}{0:>7}{'n/a':>10}{'n/a':>9}{'n/a':>9}{0.0:>10.1f}{0.0:>7.1f}%")
            continue
        durs_sorted = sorted(durs_ms)
        n = len(durs_sorted)
        mean_ms = statistics.mean(durs_sorted)
        p50 = pctl(durs_sorted, 0.50)
        p90 = pctl(durs_sorted, 0.90)
        total_s = sum(durs_sorted) / 1000.0
        share = (total_s / total_paired_wall * 100.0) if total_paired_wall else float("nan")
        print(
            f"  {label:<16}{n:>7}{mean_ms:>10.1f}{p50:>9.1f}{p90:>9.1f}"
            f"{total_s:>10.1f}{share:>7.1f}%"
        )
    for s, _nxt, _delta in pairs:
        if s.stepping_agents is None:
            unbucketed += 1
    if unbucketed:
        print(f"  ({unbucketed} paired step(s) excluded from buckets: no stepping_agents sample)")


def compare_runs(runs: list) -> None:
    usable = [rd for rd in runs if rd.summary and rd.summary.get("wall_seconds") is not None]
    if len(runs) < 2:
        return

    print("\n" + "=" * 78)
    print("COMPARISON")
    print("=" * 78)

    if len(usable) < len(runs):
        skipped = [rd.run_id for rd in runs if rd not in usable]
        print(f"  (skipping wall-time comparison for runs with no usable summary: {skipped})")

    if len(usable) < 2:
        print("  fewer than 2 runs with usable summary.json — nothing to compare.")
        return

    print(f"\n  {'run_id':<28}{'scenario':<28}{'wall_seconds':>14}")
    for rd in usable:
        scenario = rd.summary.get("scenario", "?")
        wall = rd.summary.get("wall_seconds")
        print(f"  {rd.run_id:<28}{str(scenario):<28}{fmt(wall, 1):>14}")

    base = usable[0]
    base_wall = base.summary.get("wall_seconds")
    print(f"\n  deltas vs first run ({base.run_id}, {fmt(base_wall, 1)} s):")
    for rd in usable[1:]:
        wall = rd.summary.get("wall_seconds")
        if wall is None or base_wall is None:
            print(f"    {rd.run_id}: n/a")
            continue
        delta = wall - base_wall
        pct = (delta / base_wall * 100.0) if base_wall else float("nan")
        sign = "+" if delta >= 0 else ""
        print(f"    {rd.run_id}: {sign}{fmt(delta, 1)} s ({sign}{fmt(pct, 1)}%)")

    # Group by scenario for the n>=3 min/median/max rule (CLAUDE.md section 17).
    by_scenario: dict = {}
    for rd in usable:
        scenario = rd.summary.get("scenario", "?")
        by_scenario.setdefault(scenario, []).append(rd)

    print("\n  per-scenario wall-time spread (project rule: n>=3 required before claiming a wall-time delta):")
    for scenario, group in by_scenario.items():
        walls = [rd.summary.get("wall_seconds") for rd in group if rd.summary.get("wall_seconds") is not None]
        n = len(walls)
        if n == 0:
            continue
        lo, med, hi = min(walls), statistics.median(walls), max(walls)
        print(f"    {scenario}: n={n}  min={fmt(lo,1)}  median={fmt(med,1)}  max={fmt(hi,1)}")
        if n < 3:
            print(
                f"      WARNING: only {n} run(s) of this scenario supplied — "
                f"CLAUDE.md section 17 requires n>=3 on a quiet box before making "
                f"any wall-time claim. Treat this as a data point, not a result."
            )


def main(argv: list) -> int:
    if not argv:
        print("usage: perf_decompose.py <run_id> [<run_id> ...]", file=sys.stderr)
        return 2

    runs = [load_run(run_id) for run_id in argv]
    for rd in runs:
        print_run_report(rd)
        print()

    compare_runs(runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
