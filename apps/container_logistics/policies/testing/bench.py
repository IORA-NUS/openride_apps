"""Whole-tick bench: score dispatch selections on synthetic or captured ticks.

Single responsibility: ``bench(entries, fixtures, param_overrides) -> BenchReport`` and
``render_table(report)`` (round 2, ``docs/plans/policy_slots_round2_whole_tick.md``
§5 C5; plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.5). Each (fixture, entry) gets
a fresh instance of the entry's whole selection tree (``build_runner_for_policy``),
decides the WHOLE tick through ``runner.run`` (a plan the invariants reject counts as
a violation and commits nothing, exactly as live) and is accounted by the
framework's ``assignment.accounting.account_shares`` -- the same function for every
policy, so ``cross_company`` / ``benefit_km_total`` compare across solvers. Nothing
is injected: the tick context is the randomness input, and the ``random`` module is
seeded per decide (the fixture's ``seed``, or a capture's recorded state).

Entries may be aliased, so one policy can be benched in several configurations:
``["pm=PooledMarket", "pr=PooledMarket"]`` with ``{"pr": {"inner": "RandomAssignment"}}``.
An override is the nested ``--policy-param`` shape (a string on a ref sets its type
and resets its params; ``{"inner": {"params": {...}}}`` merges into the current ref),
merged over the policy's defaults (``framework.resolve.merge_params``) and validated.

Columns: ``policy`` (the alias), ``label`` (the selection, e.g.
``PooledMarket/RandomAssignment``), ``badge`` (the top node's stamped provenance badge,
e.g. ``per company``; empty when the solver declares none), the counts, haversine
``deadhead_km_*``, ``dual_cycle``, ``cross_company`` (committed cross-company shares)
and ``benefit_km_total`` (sum of ``owner_reserve_km - awarded_cost_km`` over the
shares whose both values are finite), solve times and ``violations``.

Deadhead is the HAVERSINE km from truck anchor to pickup (``tick.measure.deadhead_km``),
a decision-time proxy: it is not the road-distance KPI (CLAUDE.md §6.3), and one tick
screens policies; only runs (n>=3, interleaved) certify them.

Testing side: reads the registry and the dispatch framework.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import statistics
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from apps.container_logistics.assignment import accounting
from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch import capture
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import Tick
from apps.container_logistics.policies.framework.errors import PolicyError
from apps.container_logistics.policies.framework.registry import get_policy_spec
from apps.container_logistics.policies.framework.resolve import merge_params

logger = logging.getLogger(__name__)

__all__ = [
    "BENCH_HEADER",
    "BENCH_COLUMNS",
    "BenchEntry",
    "BenchRow",
    "BenchReport",
    "CapturedFixture",
    "bench",
    "parse_bench_entries",
    "render_table",
    "load_fixtures",
]

BENCH_HEADER = (
    "haversine deadhead (decision-time proxy, NOT the road-distance KPI); "
    "single-tick scores screen policies, runs certify them (n≥3 interleaved)."
)
# One radius for every policy, so dual-cycle counts are comparable across policies.
_DUAL_CYCLE_RADIUS_KM = assign_constraints._DEFAULT_DUAL_CYCLE_RADIUS_KM


@dataclass(frozen=True)
class CapturedFixture:
    """A tick captured from a live run (``openride.dispatch_context/3``, or a converted ``/2``)."""

    id: str
    fx: Mapping[str, Any]

    @property
    def seed(self) -> int:
        return 0

    @contextmanager
    def module_random(self) -> Iterator[None]:
        """The module RNG at its captured decide-time state (``capture.restored_module_rng``)."""
        with capture.restored_module_rng(self.fx):
            yield

    def context(self, runner: Any, *, trucks: Any = None, orders: Any = None,
                scope_log: Any = None) -> Tick:
        """``capture.context_from_fixture`` for ``runner``'s selection (order overrides unsupported)."""
        if trucks is not None or orders is not None:
            raise ValueError("a captured fixture replays its recorded truck/order order only")
        return capture.context_from_fixture(self.fx, runner, scope_log=scope_log)


@dataclass(frozen=True)
class BenchEntry:
    """One benched selection: ``alias`` (the row name), the registered top ``name``."""

    alias: str
    name: str


@dataclass(frozen=True)
class BenchRow:
    """One entry's totals over every benched fixture."""

    policy: str
    label: str
    badge: str | None
    fixtures: int
    assigned: int
    orders: int
    assign_rate: float
    deadhead_km_total: float
    deadhead_km_mean: float
    dual_cycle: int
    cross_company: int
    benefit_km_total: float
    solve_ms_p50: float
    solve_ms_max: float
    violations: int


BENCH_COLUMNS = ("policy", "label", "badge", "fixtures", "assigned", "orders", "assign_rate",
                 "deadhead_km_total", "deadhead_km_mean", "dual_cycle", "cross_company", "benefit_km_total",
                 "solve_ms_p50", "solve_ms_max", "violations")


@dataclass(frozen=True)
class BenchReport:
    """The header (always :data:`BENCH_HEADER`), the fixture ids and one row per entry."""

    header: str
    fixture_ids: tuple[str, ...]
    rows: tuple[BenchRow, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (control-plane payload)."""
        return {"header": self.header, "fixture_ids": list(self.fixture_ids),
                "rows": [row.__dict__ for row in self.rows]}


def parse_bench_entries(items: Sequence[str]) -> list[BenchEntry]:
    """``["pm=PooledMarket", "GreedyNearest"]`` -> entries; a bare name is its own alias.

    Raises ``PolicyError`` for an empty item, an empty alias/name, or a repeated alias.
    Names are not resolved here (:func:`bench` does).
    """
    out: list[BenchEntry] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        alias, sep, name = text.partition("=")
        alias, name = alias.strip(), name.strip()
        if not sep:
            alias = name = text
        if not alias or not name:
            raise PolicyError(f"bench entry {item!r} must be NAME or ALIAS=NAME, e.g. b=<a registered policy name>.")
        if alias in seen:
            raise PolicyError(f"bench alias {alias!r} is used twice; give each benched selection its own alias.")
        seen.add(alias)
        out.append(BenchEntry(alias=alias, name=name))
    return out


def _entries(items: Sequence[str | BenchEntry]) -> list[BenchEntry]:
    plain = [i for i in items if not isinstance(i, BenchEntry)]
    parsed = iter(parse_bench_entries(plain))
    return [i if isinstance(i, BenchEntry) else next(parsed) for i in items]


def _validated_params(entry: BenchEntry, override: Mapping[str, Any] | None) -> dict[str, Any]:
    """The entry's override merged over its policy's defaults, validated (refs resolved)."""
    spec = get_policy_spec("planner", entry.name)
    where = f"bench params for {entry.alias} ({spec.name})"
    defaults = spec.params_cls.from_mapping(None, where=where).to_dict()
    merged = merge_params(spec, defaults, dict(override or {}), path=f"{entry.alias}")
    return spec.params_cls.from_mapping(merged, where=where).to_dict()


def _bench_one(entry: BenchEntry, params: Mapping[str, Any], fixture: Any) -> dict[str, Any]:
    runner = build_runner_for_policy(entry.name, params)
    with fixture.module_random():
        tick = fixture.context(runner)
        pairs = runner.run(tick).pairs
    mtt = tick.world.unread().get("max_travel_time_pickup")

    def own_allowed(t: Any, o: Any) -> bool:
        # The owner reserve's rule (as AssignmentApp): strict own-haulier feasibility.
        return assign_constraints.pair_allowed(t, o, max_travel_time_pickup=mtt)

    measure = tick.measure
    shares = accounting.account_shares(pairs, candidates=list(tick.raw_trucks),
                                       cost=measure.cost, own_allowed=own_allowed)
    benefit = 0.0
    for acc in shares:
        reserve, awarded = acc.owner_reserve_km, acc.awarded_cost_km
        if reserve is not None and math.isfinite(reserve) and math.isfinite(awarded):
            benefit += reserve - awarded
    km = [measure.deadhead_km(t, o) for t, o in pairs]
    return {
        "label": runner.resolved.label,
        "assigned": len(pairs),
        "orders": len(tick.raw_orders),
        "deadhead": [k for k in km if math.isfinite(k)],
        "dual": sum(1 for t, o in pairs if measure.is_dual_cycle(t, o, radius_km=_DUAL_CYCLE_RADIUS_KM)),
        "cross": len(shares),
        "benefit": benefit,
        "ms": runner.stats["total_ms"],
        "violations": runner.stats["violations"],
    }


def bench(
    names: Sequence[str | BenchEntry],
    fixtures: Sequence[Any],
    param_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> BenchReport:
    """Run every entry of ``names`` (``NAME`` or ``ALIAS=NAME``) on every fixture; one row per entry.

    ``param_overrides`` maps an ALIAS to a nested override (validated; unknown keys
    and unknown ref targets raise ``PolicyError``). An alias in it that is not benched
    raises ``ValueError``.
    """
    entries = _entries(names)
    overrides = dict(param_overrides or {})
    stray = sorted(set(overrides) - {e.alias for e in entries})
    if stray:
        raise ValueError(f"param overrides for {stray} name entries that are not benched "
                         f"({[e.alias for e in entries]})")
    rows = []
    for entry in entries:
        params = _validated_params(entry, overrides.get(entry.alias))
        runs = [_bench_one(entry, params, f) for f in fixtures]
        assigned = sum(r["assigned"] for r in runs)
        orders = sum(r["orders"] for r in runs)
        deadhead = [k for r in runs for k in r["deadhead"]]
        ms = [r["ms"] for r in runs]
        probe = build_runner_for_policy(entry.name, params).tree
        rows.append(BenchRow(
            policy=entry.alias,
            label=probe.resolved.label,
            badge=probe.declaration.provenance.get("badge"),
            fixtures=len(runs),
            assigned=assigned,
            orders=orders,
            assign_rate=round(assigned / orders, 4) if orders else 0.0,
            deadhead_km_total=round(sum(deadhead), 3),
            deadhead_km_mean=round(statistics.fmean(deadhead), 3) if deadhead else 0.0,
            dual_cycle=sum(r["dual"] for r in runs),
            cross_company=sum(r["cross"] for r in runs),
            benefit_km_total=round(sum(r["benefit"] for r in runs), 3),
            solve_ms_p50=round(statistics.median(ms), 2) if ms else 0.0,
            solve_ms_max=round(max(ms), 2) if ms else 0.0,
            violations=sum(r["violations"] for r in runs),
        ))
    return BenchReport(header=BENCH_HEADER, fixture_ids=tuple(f.id for f in fixtures), rows=tuple(rows))


def render_table(report: BenchReport) -> str:
    """A fixed-width text table, the header line first."""
    cells = [list(BENCH_COLUMNS)] + [[str(getattr(row, c)) for c in BENCH_COLUMNS] for row in report.rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(BENCH_COLUMNS))]
    lines = [report.header, f"fixtures: {', '.join(report.fixture_ids) or '(none)'}"]
    for i, row in enumerate(cells):
        lines.append("  ".join(cell.ljust(w) if j < 3 else cell.rjust(w)
                               for j, (cell, w) in enumerate(zip(row, widths))))
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def load_fixtures(directory: str) -> list[CapturedFixture]:
    """Every ``step_*.json.gz`` capture in ``directory``, sorted by time step.

    Each is read with ``capture.read_fixture`` (format ``/3``, a ``/2`` converted) and replayed through
    ``capture.context_from_fixture``. Raises ``FileNotFoundError`` for a missing
    directory and ``PolicyError`` for a file in another format (message 22 for ``/1``).
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"fixture directory {directory!r} does not exist")
    paths = glob.glob(os.path.join(directory, "step_*.json.gz"))

    def _step(path: str) -> tuple[int, str]:
        stem = os.path.basename(path)[len("step_"): -len(".json.gz")]
        return (int(stem), path) if stem.isdigit() else (-1, path)

    return [CapturedFixture(id=os.path.basename(p)[: -len(".json.gz")], fx=capture.read_fixture(p))
            for p in sorted(paths, key=_step)]
