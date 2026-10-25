#!/usr/bin/env python
"""Fit the day-plan time model from ONE baseline run and write ``time_model_data.py``.

Usage::

    venv/bin/python scripts/fit_day_plan_time_model.py <baseline_run_id> \\
        [--mongo-uri mongodb://127.0.0.1:27017] [--db OpenRoadDB] \\
        [--out apps/.../solver_lib/day_plan/time_model_data.py] [--fit-date YYYY-MM-DD] \\
        [--note "why this run"] [--dry-run]

MongoDB is read READ-ONLY (``find`` only): ``run_config`` (settings + scenario slug),
``container_logistics_order`` (``meta.request_time_step``, profile) and
``container_logistics_haul_trip`` (completed trips: ``meta.award_time_step``,
``stats.*`` stamps, ``sim_clock`` = completion, locations, ``routes.planned.*``).

What is fitted (see ``time_model.py`` for the semantics):

- ``job_seconds``: per-cell median of (haul completion - award) over completed trips,
  one table per lookup level, most specific first: ``facility`` (pair x queuing
  facility, ``time_model.facility_key``) -> ``pair_hour`` (pair x dispatch hour bucket
  on the elapsed axis) -> ``pair`` -> ``global`` (``--levels`` changes the chain;
  ``cell`` = pair x hour x km bucket is available);
- ``deadhead``: least squares ``award -> pickup-queue arrival`` on great-circle km
  (reposition origin -> pickup), over trips that drove a repositioning leg.

Accuracy is reported by HOLD-OUT BY DAY (leave one release day out, refit on the other
days, predict the held-out day) as MAE and P90 -- both of |residual| and of the signed
residual ``actual - predicted`` -- overall and per feature group. The recommended
``nf_late_slack_min`` is the P90 of the signed hold-out residual, rounded up to 5 min
(critique minor 14: a fixed 15 min would be wrong under a right-skewed queue); the
same rule per pickup/dropoff type pair goes into ``MODEL["late_slack"]``.

A refit is a ``version`` bump of the wrapper (plan §3); the module's ``DIGEST`` covers
everything that moves a prediction.
"""

from __future__ import annotations

import argparse
import math
import pprint
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from apps.container_logistics.policies.solver_lib.day_plan.time_model import (  # noqa: E402
    SCHEMA_VERSION,
    JobFeatures,
    TimeModel,
    facility_key,
    haversine_km,
    hour_bucket,
    km_bucket,
    level_key,
    location_type,
    model_digest,
    validate_model,
)

DEFAULT_OUT = REPO / "apps/container_logistics/policies/solver_lib/day_plan/time_model_data.py"
DEFAULT_HOUR_BUCKET_HOURS = 4
DEFAULT_KM_EDGES = (5.0, 10.0, 15.0, 20.0, 30.0)
DEFAULT_MIN_CELL_N = 30
DEFAULT_LEVELS = ("facility", "pair_hour", "pair")
STEPS_PER_DAY_SECONDS = 86400.0
SLACK_ROUND_MIN = 5

TRIP_PROJECTION = {
    "order": 1,
    "state": 1,
    "sim_clock": 1,
    "pickup_loc": 1,
    "dropoff_loc": 1,
    "meta.award_time_step": 1,
    "meta.reposition_origin_loc": 1,
    "meta.order_profile.pickup_facility_name": 1,
    "meta.order_profile.dropoff_facility_name": 1,
    "stats": 1,
    "routes.planned.repositioning_to_pickup.geometry_source": 1,
    "routes.planned.loaded_to_dropoff.geometry_source": 1,
}
ORDER_PROJECTION = {
    "meta.request_time_step": 1,
    "profile.pickup_facility_name": 1,
    "profile.dropoff_facility_name": 1,
    "profile.pickup_location_type": 1,
    "profile.dropoff_location_type": 1,
}


@dataclass(frozen=True)
class Row:
    """One completed baseline haul, reduced to model inputs and targets."""

    order: str
    day: int  # release day on the elapsed axis
    pickup: str  # CT / CU / MT / ??
    dropoff: str
    dispatch_s: float  # award, seconds since REFERENCE_TIME
    km: float  # pickup -> dropoff great-circle km
    job_s: float  # completion - award
    latency_s: float  # award - release (baseline dispatch latency; report only)
    deadhead_km: Optional[float]  # reposition origin -> pickup, if a leg was driven
    deadhead_s: Optional[float]  # award -> pickup-queue arrival
    pickup_name: Optional[str] = None  # facility names (per-facility level)
    dropoff_name: Optional[str] = None


# ----------------------------------------------------------------------------- helpers


def _reference_epoch(reference_time: str) -> datetime:
    """``REFERENCE_TIME`` is zone-less (``"2020-01-01 08:00:00"``): read it as UTC."""
    return datetime.strptime(reference_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _elapsed(stamp: Any, ref: datetime) -> Optional[float]:
    """Seconds since ``ref`` for an RFC-1123 string or a (naive = UTC) datetime."""
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        moment = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    else:
        try:
            moment = parsedate_to_datetime(str(stamp))
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    return (moment - ref).total_seconds()


def _coords(loc: Any) -> Optional[tuple[float, float]]:
    if isinstance(loc, Mapping):
        loc = loc.get("coordinates")
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        return float(loc[0]), float(loc[1])
    return None


def _get(doc: Mapping, dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


def quantile(values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile (deterministic, no interpolation); 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return float(ordered[rank - 1])


def _round(x: float, nd: int = 1) -> float:
    return float(round(float(x), nd))


# --------------------------------------------------------------------------- docs->rows


def rows_from_docs(
    orders: Mapping[str, Mapping],
    trips: Iterable[Mapping],
    *,
    reference_time: str,
    step_seconds: float,
) -> tuple[list[Row], Counter]:
    """Join completed haul trips with their orders. Pure; returns ``(rows, skipped)``."""
    ref = _reference_epoch(reference_time)
    rows: list[Row] = []
    skipped: Counter = Counter()
    for trip in trips:
        if trip.get("state") != "completed":
            skipped["not_completed"] += 1
            continue
        order = orders.get(str(trip.get("order")))
        if order is None:
            skipped["no_order"] += 1
            continue
        req = _get(order, "meta.request_time_step")
        award = _get(trip, "meta.award_time_step")
        if req is None or award is None:
            skipped["no_step_stamp"] += 1
            continue
        release_s = float(req) * step_seconds
        dispatch_s = float(award) * step_seconds
        done_s = _elapsed(trip.get("sim_clock"), ref)
        if done_s is None or done_s < dispatch_s or dispatch_s < release_s:
            skipped["bad_times"] += 1
            continue
        p, d = _coords(trip.get("pickup_loc")), _coords(trip.get("dropoff_loc"))
        if p is None or d is None:
            skipped["no_location"] += 1
            continue
        pname = _get(order, "profile.pickup_facility_name") or _get(
            trip, "meta.order_profile.pickup_facility_name"
        )
        dname = _get(order, "profile.dropoff_facility_name") or _get(
            trip, "meta.order_profile.dropoff_facility_name"
        )
        ptype = location_type(pname)
        if ptype == "??":
            ptype = location_type(_get(order, "profile.pickup_location_type"))
        dtype = location_type(dname)
        if dtype == "??":
            dtype = location_type(_get(order, "profile.dropoff_location_type"))
        stats = trip.get("stats") or {}
        dh_km = dh_s = None
        origin = _coords(_get(trip, "meta.reposition_origin_loc"))
        arrival = _elapsed(stats.get("pickup_queue_arrival_time"), ref)
        if origin is not None and stats.get("reposition_started_at") and arrival is not None:
            km_leg = haversine_km(origin[0], origin[1], p[0], p[1])
            if km_leg > 0.05 and arrival >= dispatch_s:
                dh_km, dh_s = km_leg, arrival - dispatch_s
        rows.append(
            Row(
                order=str(trip.get("order")),
                day=int(release_s // STEPS_PER_DAY_SECONDS),
                pickup=ptype,
                dropoff=dtype,
                dispatch_s=dispatch_s,
                km=haversine_km(p[0], p[1], d[0], d[1]),
                job_s=done_s - dispatch_s,
                latency_s=dispatch_s - release_s,
                deadhead_km=dh_km,
                deadhead_s=dh_s,
                pickup_name=str(pname) if pname else None,
                dropoff_name=str(dname) if dname else None,
            )
        )
    rows.sort(key=lambda r: (r.dispatch_s, r.order))  # input order never matters
    return rows, skipped


# --------------------------------------------------------------------------------- fit


def _features(row: Row, hb_hours: int, edges: Sequence[float]) -> JobFeatures:
    return JobFeatures(
        row.pickup,
        row.dropoff,
        hour_bucket(row.dispatch_s, hb_hours),
        km_bucket(row.km, edges),
        facility_key(row.pickup_name, row.dropoff_name),
    )


def _row_model_features(model: TimeModel, row: Row) -> JobFeatures:
    """What the planner would pass: facility NAMES when known, else the type codes."""
    return model.features(
        row.pickup_name or row.pickup, row.dropoff_name or row.dropoff, row.dispatch_s, row.km
    )


def _deadhead_fit(rows: Sequence[Row]) -> dict:
    pts = [(r.deadhead_km, r.deadhead_s) for r in rows if r.deadhead_km is not None]
    if len(pts) < 2:
        return {"intercept_s": 0.0, "per_km_s": 0.0, "n": len(pts), "km": "haversine"}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
    if slope < 0:
        slope = 0.0
    intercept = my - slope * mx
    if intercept < 0:  # through the origin instead
        intercept = 0.0
        sx2 = sum(x * x for x in xs)
        slope = max(0.0, sum(x * y for x, y in zip(xs, ys)) / sx2) if sx2 > 0 else 0.0
    return {
        "intercept_s": _round(intercept),
        "per_km_s": _round(slope, 3),
        "n": len(pts),
        "km": "haversine",
    }


def fit(
    rows: Sequence[Row],
    *,
    hour_bucket_hours: int = DEFAULT_HOUR_BUCKET_HOURS,
    km_edges: Sequence[float] = DEFAULT_KM_EDGES,
    min_cell_n: int = DEFAULT_MIN_CELL_N,
    levels: Sequence[str] = DEFAULT_LEVELS,
) -> dict:
    """The model mapping (schema 2, no late_slack) from ``rows``. Deterministic in the row SET."""
    if not rows:
        raise ValueError("cannot fit a time model on zero rows")
    groups: dict[str, dict[str, list[float]]] = {lv: defaultdict(list) for lv in levels}
    for row in rows:
        f = _features(row, hour_bucket_hours, km_edges)
        for level in levels:
            key = level_key(level, f)
            if key is not None:
                groups[level][key].append(row.job_s)

    def table(g: Mapping[str, list[float]]) -> dict:
        return {k: [_round(statistics.median(v)), len(v)] for k, v in sorted(g.items())}

    model = {
        "schema_version": SCHEMA_VERSION,
        "features": {
            "hour_bucket_hours": int(hour_bucket_hours),
            "km_edges": [float(e) for e in km_edges],
            "hour_axis": "elapsed",
            "hour_of": "dispatch",
            "km": "haversine pickup->dropoff",
            "location_type": "facility-name prefix: port->CT, customer->CU, depot/yard->MT",
            "facility": "depot id(s) when the pair touches a depot, else the pickup facility id",
        },
        "job_seconds": {
            "target": "haul completion (sim_clock) - award (meta.award_time_step)",
            "estimator": "median",
            "min_cell_n": int(min_cell_n),
            "levels": list(levels),
            "global": _round(statistics.median([r.job_s for r in rows])),
            **{level: table(groups[level]) for level in levels},
        },
        "deadhead": _deadhead_fit(rows),
    }
    validate_model(model)
    return model


# ---------------------------------------------------------------------------- evaluate


def _summary(residuals: Sequence[float]) -> dict:
    if not residuals:
        return {"n": 0, "mae_s": 0.0, "p90_abs_s": 0.0, "p90_signed_s": 0.0, "bias_s": 0.0}
    return {
        "n": len(residuals),
        "mae_s": _round(statistics.fmean(abs(x) for x in residuals)),
        "p90_abs_s": _round(quantile([abs(x) for x in residuals], 0.9)),
        "p90_signed_s": _round(quantile(residuals, 0.9)),
        "bias_s": _round(statistics.fmean(residuals)),
    }


def _target_shape(values: Sequence[float]) -> dict:
    """Right-skew of the TARGET itself (queue tail): mean vs median, P90/median."""
    if not values:
        return {"n": 0}
    med = statistics.median(values)
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    return {
        "n": len(values),
        "median_s": _round(med),
        "mean_s": _round(mean),
        "p90_s": _round(quantile(values, 0.9)),
        "p90_over_median": _round(quantile(values, 0.9) / med, 2) if med > 0 else None,
        "pearson_skew": _round(3.0 * (mean - med) / sd, 3) if sd > 0 else 0.0,
    }


def holdout_by_day(rows: Sequence[Row], **fit_kw: Any) -> dict:
    """Leave-one-release-day-out. Residual = actual - predicted (positive = late)."""
    days = sorted({r.day for r in rows})
    by_group: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    pooled: list[float] = []
    per_day: dict[str, dict] = {}
    for day in days:
        train = [r for r in rows if r.day != day]
        test = [r for r in rows if r.day == day]
        if not train:
            continue
        model = TimeModel(fit(train, **fit_kw))
        res_day: list[float] = []
        for r in test:
            f = _row_model_features(model, r)
            pred, level = model.lookup(f)
            res = r.job_s - pred
            res_day.append(res)
            by_group["pair"][f"{f.pickup_type}->{f.dropoff_type}"].append(res)
            by_group["hour_bucket"][str(f.hour_bucket)].append(res)
            by_group["km_bucket"][str(f.km_bucket)].append(res)
            by_group["level"][level].append(res)
            touches_depot = "MT" in (f.pickup_type, f.dropoff_type)
            by_group["depot_leg"]["touches_MT" if touches_depot else "no_MT"].append(res)
        pooled.extend(res_day)
        per_day[str(day)] = _summary(res_day)
    return {
        "overall": _summary(pooled),
        "per_day": per_day,
        "by_group": {
            g: {k: _summary(v) for k, v in sorted(groups.items())} for g, groups in sorted(by_group.items())
        },
    }


def target_shapes(rows: Sequence[Row]) -> dict:
    by_pair: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_pair[f"{r.pickup}->{r.dropoff}"].append(r.job_s)
    return {
        "all": _target_shape([r.job_s for r in rows]),
        "by_pair": {k: _target_shape(v) for k, v in sorted(by_pair.items())},
        "baseline_dispatch_latency": _target_shape([r.latency_s for r in rows]),
    }


def recommend_late_slack_min(p90_signed_s: float, round_to: int = SLACK_ROUND_MIN) -> int:
    """P90 of the signed residual, in minutes, rounded UP to ``round_to``; never < 0."""
    minutes = max(0.0, float(p90_signed_s)) / 60.0
    return int(math.ceil(minutes / round_to) * round_to) if minutes > 0 else 0


def late_slack_table(holdout: Mapping) -> dict:
    """``MODEL["late_slack"]`` from a hold-out report: per pair and overall, seconds."""
    by_pair = {
        pair.replace("->", "|"): float(60 * recommend_late_slack_min(summary["p90_signed_s"]))
        for pair, summary in sorted(holdout["by_group"].get("pair", {}).items())
    }
    return {
        "by_pair_s": by_pair,
        "default_s": float(60 * recommend_late_slack_min(holdout["overall"]["p90_signed_s"])),
        "rule": "P90 of signed hold-out residual (actual - predicted), rounded up to 5 min",
    }


# ------------------------------------------------------------------------------ render


def render_module(
    model: Mapping,
    report: Mapping,
    *,
    run_id: str,
    scenario: str,
    fit_date: str,
    baseline: Mapping,
) -> str:
    """Python source of ``time_model_data.py``. Deterministic in its arguments."""
    validate_model(model)
    overall = report["holdout"]["overall"]
    slack = recommend_late_slack_min(overall["p90_signed_s"])
    by_pair_min = {
        k: int(v // 60) for k, v in sorted((model.get("late_slack") or {}).get("by_pair_s", {}).items())
    }
    digest = model_digest(model)
    pp = lambda obj: pprint.pformat(obj, width=100, sort_dicts=True)  # noqa: E731
    return (
        '"""GENERATED by scripts/fit_day_plan_time_model.py -- do not edit by hand.\n'
        "\n"
        "Day-plan time model coefficients (see ``time_model.py``). Pure data: importing\n"
        "this module performs no I/O. ``DIGEST`` covers everything that moves a\n"
        "prediction (``model_digest(MODEL)``); a refit is a wrapper ``version`` bump.\n"
        '"""\n'
        "\n"
        f"RUN_ID = {run_id!r}\n"
        f"SCENARIO = {scenario!r}\n"
        f"FIT_DATE = {fit_date!r}\n"
        f"N = {int(report['n'])}\n"
        "#: Hold-out by day (leave one release day out), seconds.\n"
        f"MAE_SECONDS = {overall['mae_s']!r}\n"
        f"P90_SECONDS = {overall['p90_abs_s']!r}  # P90 of |actual - predicted|\n"
        f"P90_SIGNED_SECONDS = {overall['p90_signed_s']!r}  # P90 of actual - predicted (late)\n"
        f"RECOMMENDED_LATE_SLACK_MIN = {slack!r}\n"
        "#: Per pickup|dropoff type pair, minutes (same rule; MODEL['late_slack'] in seconds).\n"
        f"LATE_SLACK_MIN_BY_PAIR = {pp(by_pair_min)}\n"
        "\n"
        f"BASELINE = {pp(dict(baseline))}\n"
        "\n"
        f"MODEL = {pp(dict(model))}\n"
        "\n"
        f"REPORT = {pp(dict(report))}\n"
        "\n"
        f"DIGEST = {digest!r}\n"
    )


def build(
    rows: Sequence[Row],
    *,
    skipped: Mapping[str, int],
    run_id: str,
    scenario: str,
    fit_date: str,
    baseline: Mapping,
    **fit_kw: Any,
) -> tuple[str, dict, dict]:
    """Fit on ALL rows, evaluate by day hold-out, render. Returns (source, model, report)."""
    model = fit(rows, **fit_kw)
    holdout = holdout_by_day(rows, **fit_kw)
    model["late_slack"] = late_slack_table(holdout)
    validate_model(model)
    report = {
        "n": len(rows),
        "skipped": dict(sorted(skipped.items())),
        "holdout": holdout,
        "target": target_shapes(rows),
        "deadhead_n": model["deadhead"]["n"],
    }
    source = render_module(
        model, report, run_id=run_id, scenario=scenario, fit_date=fit_date, baseline=baseline
    )
    return source, model, report


# ------------------------------------------------------------------------------- mongo


def baseline_checks(run_config: Mapping, geometry: Mapping[str, Mapping[str, int]]) -> list[str]:
    """Reasons a run is NOT a usable baseline (CLAUDE.md §6.21 / §6.26 / §8). Pure."""
    problems = []
    if run_config.get("status") not in ("success", "completed"):
        problems.append(f"run status {run_config.get('status')!r}")
    settings = _get(run_config, "meta.simulation_settings") or {}
    transport = settings.get("AGENT_TRANSPORT")
    if transport not in (None, "paho", "paho-shared"):
        problems.append(f"transport {transport!r} is not a comparable (paho) arm (§6.26)")
    if settings.get("TRUCK_CADENCE_STAGGER"):
        problems.append("TRUCK_CADENCE_STAGGER on (§6.22 comparability boundary)")
    for leg, counts in sorted(geometry.items()):
        total = sum(counts.values())
        osrm = counts.get("osrm", 0)
        if total and osrm != total:
            problems.append(f"{leg}: {total - osrm}/{total} trips without osrm geometry (§6.21)")
    return problems


def load_from_mongo(uri: str, db_name: str, run_id: str) -> dict:  # pragma: no cover - needs Mongo
    from pymongo import MongoClient

    db = MongoClient(uri, serverSelectionTimeoutMS=5000)[db_name]
    run_config = db["run_config"].find_one({"run_id": run_id})
    if run_config is None:
        raise SystemExit(f"no run_config for {run_id!r}")
    orders = {
        str(o["_id"]): o
        for o in db["container_logistics_order"].find(
            {"run_id": run_id, "state": "completed"}, ORDER_PROJECTION
        )
    }
    trips = list(
        db["container_logistics_haul_trip"].find(
            {"run_id": run_id, "state": "completed"}, TRIP_PROJECTION
        )
    )
    geometry: dict[str, Counter] = {}
    for leg in ("repositioning_to_pickup", "loaded_to_dropoff"):
        geometry[leg] = Counter(
            str(_get(t, f"routes.planned.{leg}.geometry_source")) for t in trips
        )
    return {"run_config": run_config, "orders": orders, "trips": trips, "geometry": geometry}


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27017")
    ap.add_argument("--db", default="OpenRoadDB")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--fit-date", default=date.today().isoformat())
    ap.add_argument("--note", default="", help="why this baseline was chosen (recorded)")
    ap.add_argument("--hour-bucket-hours", type=int, default=DEFAULT_HOUR_BUCKET_HOURS)
    ap.add_argument("--min-cell-n", type=int, default=DEFAULT_MIN_CELL_N)
    ap.add_argument("--levels", default=",".join(DEFAULT_LEVELS), help="lookup chain, most specific first")
    ap.add_argument("--allow-unusable", action="store_true", help="fit despite baseline problems")
    ap.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    args = ap.parse_args(argv)

    data = load_from_mongo(args.mongo_uri, args.db, args.run_id)
    rc = data["run_config"]
    settings = _get(rc, "meta.simulation_settings") or {}
    problems = baseline_checks(rc, data["geometry"])
    if problems and not args.allow_unusable:
        print("baseline unusable:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    rows, skipped = rows_from_docs(
        data["orders"],
        data["trips"],
        reference_time=settings.get("REFERENCE_TIME", "2020-01-01 08:00:00"),
        step_seconds=float(settings.get("STEP_INTERVAL", 240)),
    )
    scenario = _get(rc, "meta.scenario_slug") or ""
    baseline = {
        "run_name": rc.get("name"),
        "status": rc.get("status"),
        "transport": settings.get("AGENT_TRANSPORT"),
        "structure": _get(settings, "COOPERATION.structure_id"),
        "solver": _get(settings, "GENERATION_SPEC.solver"),
        "reference_time": settings.get("REFERENCE_TIME"),
        "step_seconds": settings.get("STEP_INTERVAL"),
        "geometry": {k: dict(sorted(v.items())) for k, v in sorted(data["geometry"].items())},
        "problems": problems,
        "note": args.note,
    }
    source, model, report = build(
        rows,
        skipped=skipped,
        run_id=args.run_id,
        scenario=scenario,
        fit_date=args.fit_date,
        baseline=baseline,
        hour_bucket_hours=args.hour_bucket_hours,
        min_cell_n=args.min_cell_n,
        levels=tuple(args.levels.split(",")),
    )
    pprint.pprint({"n": report["n"], "skipped": report["skipped"], "holdout": report["holdout"]}, width=110)
    print("late slack (min):", recommend_late_slack_min(report["holdout"]["overall"]["p90_signed_s"]))
    print("digest:", model_digest(model))
    if not args.dry_run:
        args.out.write_text(source, encoding="utf-8")
        print("wrote", args.out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
