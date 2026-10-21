"""Single-tick bench: score dispatch policies on synthetic or captured fixtures.

Single responsibility: ``bench(names, fixtures, param_overrides) -> BenchReport`` and
``render_table(report)`` (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.5). Each
(fixture, policy) gets a fresh instance (``build_runner_for_policy``) with
``set_rng(Random(fixture.seed or 0))`` and ``set_tiebreak(fixture.tiebreak_seed)``,
the module RNG seeded the same way, and goes through ``runner.run``, so a plan the
invariants reject counts as a violation and commits nothing, exactly as live.

Deadhead is the HAVERSINE km from truck anchor to pickup (``objective.deadhead_km``),
a decision-time proxy: it is not the road-distance KPI (CLAUDE.md §6.3), and one tick
screens policies; only runs (n>=3, interleaved) certify them.

Testing side: reads the registry and the dispatch framework.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import random
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch import capture
from apps.container_logistics.policies.dispatch.context import DispatchContext
from apps.container_logistics.policies.dispatch.helpers import objective
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy

from .battery import seeded_module_random

logger = logging.getLogger(__name__)

__all__ = ["BENCH_HEADER", "BenchRow", "BenchReport", "CapturedFixture", "bench", "render_table", "load_fixtures"]

BENCH_HEADER = (
    "haversine deadhead (decision-time proxy, NOT the road-distance KPI); "
    "single-tick scores screen policies, runs certify them (n≥3 interleaved)."
)
# One radius for every policy, so dual-cycle counts are comparable across policies.
_DUAL_CYCLE_RADIUS_KM = assign_constraints._DEFAULT_DUAL_CYCLE_RADIUS_KM


@dataclass(frozen=True)
class CapturedFixture:
    """A fixture captured from a live run (``openride.dispatch_context/1``)."""

    id: str
    fx: Mapping[str, Any]
    seed: int = 0

    @property
    def tiebreak_seed(self) -> int | None:
        return self.fx.get("tiebreak_seed")

    def context(self, runner: Any, *, trucks: Any = None, orders: Any = None) -> DispatchContext:
        """``capture.context_from_fixture`` for ``runner``'s policy (order overrides unsupported)."""
        if trucks is not None or orders is not None:
            raise ValueError("a captured fixture replays its recorded truck/order order only")
        return capture.context_from_fixture(self.fx, runner, rng=runner.policy.rng)


@dataclass(frozen=True)
class BenchRow:
    """One policy's totals over every benched fixture."""

    policy: str
    fixtures: int
    assigned: int
    orders: int
    assign_rate: float
    deadhead_km_total: float
    deadhead_km_mean: float
    dual_cycle: int
    solve_ms_p50: float
    solve_ms_max: float
    violations: int


@dataclass(frozen=True)
class BenchReport:
    """The header (always :data:`BENCH_HEADER`), the fixture ids and one row per policy."""

    header: str
    fixture_ids: tuple[str, ...]
    rows: tuple[BenchRow, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (control-plane payload)."""
        return {"header": self.header, "fixture_ids": list(self.fixture_ids),
                "rows": [row.__dict__ for row in self.rows]}


def _bench_one(name: str, fixture: Any, params: Mapping[str, Any] | None) -> dict[str, Any]:
    runner = build_runner_for_policy(name, params)
    seed = fixture.seed or 0
    runner.policy.set_rng(random.Random(seed))
    runner.policy.set_tiebreak(fixture.tiebreak_seed)
    with seeded_module_random(seed):
        ctx = fixture.context(runner)
        pairs = runner.run(ctx)
    km = [objective.deadhead_km(t, o) for t, o in pairs]
    finite = [k for k in km if math.isfinite(k)]
    return {
        "assigned": len(pairs),
        "orders": len(ctx.raw_orders),
        "deadhead": finite,
        "dual": sum(1 for t, o in pairs if objective.is_dual_cycle(t, o, radius_km=_DUAL_CYCLE_RADIUS_KM)),
        "ms": runner.stats["total_ms"],
        "violations": runner.stats["violations"],
    }


def bench(
    names: Sequence[str],
    fixtures: Sequence[Any],
    param_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> BenchReport:
    """Run every policy in ``names`` on every fixture; one :class:`BenchRow` per policy.

    ``param_overrides`` maps a policy name to params (validated; unknown keys raise
    ``PolicyParamsError``). A name in it that is not benched raises ``ValueError``.
    """
    overrides = dict(param_overrides or {})
    stray = sorted(set(overrides) - set(names))
    if stray:
        raise ValueError(f"param overrides for {stray} name policies that are not benched ({list(names)})")
    rows = []
    for name in names:
        runs = [_bench_one(name, f, overrides.get(name)) for f in fixtures]
        assigned = sum(r["assigned"] for r in runs)
        orders = sum(r["orders"] for r in runs)
        deadhead = [k for r in runs for k in r["deadhead"]]
        ms = [r["ms"] for r in runs]
        rows.append(BenchRow(
            policy=name,
            fixtures=len(runs),
            assigned=assigned,
            orders=orders,
            assign_rate=round(assigned / orders, 4) if orders else 0.0,
            deadhead_km_total=round(sum(deadhead), 3),
            deadhead_km_mean=round(statistics.fmean(deadhead), 3) if deadhead else 0.0,
            dual_cycle=sum(r["dual"] for r in runs),
            solve_ms_p50=round(statistics.median(ms), 2) if ms else 0.0,
            solve_ms_max=round(max(ms), 2) if ms else 0.0,
            violations=sum(r["violations"] for r in runs),
        ))
    return BenchReport(header=BENCH_HEADER, fixture_ids=tuple(f.id for f in fixtures), rows=tuple(rows))


_COLUMNS = ("policy", "fixtures", "assigned", "orders", "assign_rate", "deadhead_km_total",
            "deadhead_km_mean", "dual_cycle", "solve_ms_p50", "solve_ms_max", "violations")


def render_table(report: BenchReport) -> str:
    """A fixed-width text table, the header line first."""
    cells = [list(_COLUMNS)] + [[str(getattr(row, c)) for c in _COLUMNS] for row in report.rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(_COLUMNS))]
    lines = [report.header, f"fixtures: {', '.join(report.fixture_ids) or '(none)'}"]
    for i, row in enumerate(cells):
        lines.append("  ".join(cell.ljust(w) if j == 0 else cell.rjust(w)
                               for j, (cell, w) in enumerate(zip(row, widths))))
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def load_fixtures(directory: str) -> list[CapturedFixture]:
    """Every ``step_*.json.gz`` capture in ``directory``, sorted by file name.

    Each is read with ``capture.read_fixture`` and replayed through
    ``capture.context_from_fixture``. Raises ``FileNotFoundError`` for a missing
    directory and ``PolicyError`` for a file in another format.
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"fixture directory {directory!r} does not exist")
    paths = sorted(glob.glob(os.path.join(directory, "step_*.json.gz")))
    return [CapturedFixture(id=os.path.basename(p)[: -len(".json.gz")], fx=capture.read_fixture(p))
            for p in paths]
