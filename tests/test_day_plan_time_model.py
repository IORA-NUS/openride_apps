"""Day-plan time model (workstream F, plan §3 / D3, critique minor 14).

No Mongo here: the fit script is exercised on a synthetic fixture built in this file.
"""

from __future__ import annotations

import ast
import importlib.util
import random
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from apps.container_logistics.policies.solver_lib.day_plan import time_model as tm
from apps.container_logistics.policies.solver_lib.day_plan import time_model_data as data
from apps.container_logistics.policies.solver_lib.day_plan.time_model import (
    JobFeatures,
    TimeModel,
    TimeModelError,
)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fit_day_plan_time_model.py"
PKG = REPO / "apps/container_logistics/policies/solver_lib/day_plan"


def _load_script():
    spec = importlib.util.spec_from_file_location("fit_day_plan_time_model", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


fitmod = _load_script()


def _tiny_model(**over):
    model = {
        "schema_version": 1,
        "features": {"hour_bucket_hours": 6, "km_edges": [10.0], "hour_axis": "elapsed"},
        "job_seconds": {
            "min_cell_n": 2,
            "global": 1000.0,
            "cell": {"CT|CU|0|0": [100.0, 5], "CT|CU|1|0": [150.0, 1]},
            "pair_hour": {"CT|CU|1": [200.0, 4]},
            "pair": {"CT|CU": [300.0, 9], "CU|CT": [400.0, 1]},
        },
        "deadhead": {"intercept_s": 60.0, "per_km_s": 90.0},
    }
    model.update(over)
    return model


# ------------------------------------------------------------------- shipped data module


def test_data_module_schema_and_digest():
    tm.validate_model(data.MODEL)
    assert data.DIGEST == tm.model_digest(data.MODEL)
    model = TimeModel.default()
    assert model.digest == data.DIGEST
    assert isinstance(data.RUN_ID, str) and data.RUN_ID.startswith("run_")
    assert data.SCENARIO == "consortium_collab_7d"
    assert isinstance(data.N, int) and data.N > 0
    assert data.REPORT["n"] == data.N
    datetime.strptime(data.FIT_DATE, "%Y-%m-%d")
    overall = data.REPORT["holdout"]["overall"]
    assert data.MAE_SECONDS == overall["mae_s"] > 0
    assert data.P90_SECONDS == overall["p90_abs_s"] >= data.MAE_SECONDS
    assert data.P90_SIGNED_SECONDS == overall["p90_signed_s"]
    assert data.RECOMMENDED_LATE_SLACK_MIN == fitmod.recommend_late_slack_min(data.P90_SIGNED_SECONDS)
    assert data.BASELINE["problems"] == []
    assert data.MODEL["features"]["hour_axis"] == "elapsed"


def test_modules_do_no_io_at_import():
    """Both modules import only pure stdlib; the data module imports nothing at all."""
    allowed = {"__future__", "hashlib", "json", "math", "dataclasses", "typing"}
    for name, allow in (("time_model.py", allowed), ("time_model_data.py", set())):
        tree = ast.parse((PKG / name).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert imported <= allow, (name, imported - allow)
    # the data module is a literal-only module: every statement is an assignment of a literal
    for node in ast.parse((PKG / "time_model_data.py").read_text()).body:
        if isinstance(node, ast.Expr):
            continue  # docstring
        assert isinstance(node, ast.Assign), ast.dump(node)[:80]
        ast.literal_eval(node.value)


def test_shipped_model_predictions_are_deterministic():
    a, b = TimeModel.default(), TimeModel(data.MODEL)
    rng = random.Random(7)
    types = ["CT", "CU", "MT", "port_001", "customer_018", "depot_039", "Depo", None]
    for _ in range(300):
        args = (rng.choice(types), rng.choice(types), rng.uniform(0, 7 * 86400), rng.uniform(0, 60))
        assert a.predict(*args) == b.predict(*args) == a.predict(*args)
        assert a.predict(*args) >= 0
    assert a.digest == b.digest


# ------------------------------------------------------------------------- feature maps


@pytest.mark.parametrize(
    "value, code",
    [
        ("CT", "CT"),
        ("cu", "CU"),
        ("MT", "MT"),
        ("YD", "MT"),
        ("port_002", "CT"),
        ("customer_018", "CU"),
        ("depot_039", "MT"),
        ("yard_1", "MT"),
        ("Depo", "??"),  # the bundles' profile label carries no information
        ("Warehouse", "??"),
        ("", "??"),
        (None, "??"),
    ],
)
def test_location_type(value, code):
    assert tm.location_type(value) == code


def test_hour_bucket_is_on_the_elapsed_axis():
    assert tm.hour_bucket(0, 4) == 0
    assert tm.hour_bucket(4 * 3600 - 1, 4) == 0
    assert tm.hour_bucket(4 * 3600, 4) == 1
    assert tm.hour_bucket(86400 + 5 * 3600, 4) == 1  # day 2, elapsed hour 5
    assert tm.hour_bucket(23.99 * 3600, 6) == 3
    with pytest.raises(TimeModelError):
        tm.hour_bucket(0, 5)


def test_km_bucket_half_open():
    edges = (5.0, 10.0)
    assert [tm.km_bucket(x, edges) for x in (0, 4.99, 5.0, 9.99, 10.0, 99)] == [0, 0, 1, 1, 2, 2]


def test_latency_is_half_a_cadence():
    assert tm.latency_seconds(6, 240) == 720.0
    assert tm.latency_seconds(28, 240) == 3360.0
    assert tm.latency_seconds(0, 240) == 0.0
    with pytest.raises(TimeModelError):
        tm.latency_seconds(-1, 240)


def test_haversine_known_distance():
    # one degree of longitude on the equator
    assert tm.haversine_km(0, 0, 1, 0) == pytest.approx(111.195, abs=0.01)
    assert tm.haversine_km(103.8, 1.3, 103.8, 1.3) == 0.0


# ---------------------------------------------------------------------------- predictions


def test_backoff_levels():
    m = TimeModel(_tiny_model())
    assert m.lookup(JobFeatures("CT", "CU", 0, 0)) == (100.0, "cell")
    # cell exists but n=1 < min_cell_n=2 -> pair_hour
    assert m.lookup(JobFeatures("CT", "CU", 1, 0)) == (200.0, "pair_hour")
    assert m.lookup(JobFeatures("CT", "CU", 2, 1)) == (300.0, "pair")
    # pair exists but n=1 -> global
    assert m.lookup(JobFeatures("CU", "CT", 0, 0)) == (1000.0, "global")
    assert m.lookup(JobFeatures("??", "CU", 0, 0)) == (1000.0, "global")


def test_predict_from_raw_inputs_and_job_end():
    m = TimeModel(_tiny_model())
    assert m.predict("port_1", "customer_2", 3600, 3.0) == 100.0
    assert m.predict("port_1", "customer_2", 86400 + 7 * 3600, 3.0) == 200.0
    # e = r + latency + job_seconds(dispatch); dispatch = 5h + 720 s -> still bucket 0
    assert m.job_end_seconds(5 * 3600, "CT", "CU", 3.0, cadence_steps=6, step_seconds=240) == (
        5 * 3600 + 720 + 100.0
    )
    # a dispatch that crosses into the next bucket uses that bucket
    assert m.job_end_seconds(6 * 3600 - 100, "CT", "CU", 3.0, cadence_steps=6, step_seconds=240) == (
        6 * 3600 - 100 + 720 + 200.0
    )


def test_deadhead_seconds():
    m = TimeModel(_tiny_model())
    assert m.deadhead_seconds(0) == 0.0
    assert m.deadhead_seconds(-1) == 0.0
    assert m.deadhead_seconds(2.0) == 60.0 + 180.0


def test_model_is_immutable_copy():
    raw = _tiny_model()
    m = TimeModel(raw)
    raw["job_seconds"]["cell"]["CT|CU|0|0"][0] = 9999.0
    assert m.lookup(JobFeatures("CT", "CU", 0, 0))[0] == 100.0
    copy = m.model
    copy["job_seconds"]["global"] = 1.0
    assert m.job_seconds(JobFeatures("??", "??", 0, 0)) == 1000.0


def test_digest_ignores_fit_report_but_tracks_coefficients():
    a = _tiny_model()
    b = _tiny_model()
    b["features"]["note"] = "x"  # features are covered
    assert tm.model_digest(a) != tm.model_digest(b)
    c = _tiny_model()
    c["job_seconds"]["global"] = 1001.0
    assert tm.model_digest(a) != tm.model_digest(c)
    assert tm.model_digest(_tiny_model()) == tm.model_digest(_tiny_model())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.pop("deadhead"),
        lambda m: m.update(schema_version=2),
        lambda m: m["features"].update(hour_bucket_hours=5),
        lambda m: m["features"].update(km_edges=[10.0, 5.0]),
        lambda m: m["features"].update(km_edges=[]),
        lambda m: m["features"].update(hour_axis="wall"),
        lambda m: m["job_seconds"].update(min_cell_n=0),
        lambda m: m["job_seconds"].pop("global"),
        lambda m: m["job_seconds"].update({"global": float("nan")}),
        lambda m: m["job_seconds"]["cell"].update({"x": [1.0]}),
        lambda m: m["job_seconds"]["cell"].update({"x": [-1.0, 3]}),
        lambda m: m["job_seconds"]["pair"].update({"x": [1.0, 0]}),
        lambda m: m["deadhead"].update(per_km_s=-1),
    ],
)
def test_validate_rejects(mutate):
    model = _tiny_model()
    mutate(model)
    with pytest.raises(TimeModelError):
        TimeModel(model)


# --------------------------------------------------------------- fit script, synthetic data

REF = datetime(2020, 1, 1, 8, 0, 0)
STEP = 240.0
# (pickup facility, dropoff facility, true median seconds); locations ~ 3 km / 15 km apart
_PAIRS = [
    ("port_000", "customer_001", 9000.0, (103.80, 1.30), (103.827, 1.30)),
    ("customer_002", "depot_003", 45000.0, (103.85, 1.35), (103.985, 1.35)),
    ("depot_004", "port_005", 30000.0, (103.70, 1.28), (103.727, 1.28)),
]


def _fixture(n_per_pair_day: int = 40, days: int = 4, seed: int = 11):
    """Synthetic order + haul-trip docs whose job time = pair median + symmetric noise."""
    rng = random.Random(seed)
    orders, trips = {}, []
    k = 0
    for day in range(days):
        for pfac, dfac, base, ploc, dloc in _PAIRS:
            for _ in range(n_per_pair_day):
                k += 1
                oid = f"o{k:05d}"
                req = day * 360 + rng.randrange(0, 300)
                award = req + rng.randrange(0, 20)
                job = base + rng.choice((-1, 1)) * rng.uniform(0, 600)
                done = REF + timedelta(seconds=award * STEP + job)
                arrival = REF + timedelta(seconds=award * STEP + 1200 + 70 * 2.0)
                orders[oid] = {
                    "_id": oid,
                    "meta": {"request_time_step": req},
                    "profile": {
                        "pickup_facility_name": pfac,
                        "dropoff_facility_name": dfac,
                        "pickup_location_type": "Depo",
                        "dropoff_location_type": "Warehouse",
                    },
                }
                trips.append(
                    {
                        "order": oid,
                        "state": "completed",
                        "sim_clock": done,  # naive datetime, as pymongo returns it
                        "pickup_loc": {"type": "Point", "coordinates": list(ploc)},
                        "dropoff_loc": {"type": "Point", "coordinates": list(dloc)},
                        "meta": {
                            "award_time_step": award,
                            # 2 km west of the pickup
                            "reposition_origin_loc": {"coordinates": [ploc[0] - 0.017986, ploc[1]]},
                        },
                        "stats": {
                            "reposition_started_at": "x",
                            "pickup_queue_arrival_time": arrival.strftime("%a, %d %b %Y %H:%M:%S GMT"),
                        },
                    }
                )
    # junk the join must skip
    trips.append({"order": "missing", "state": "completed"})
    trips.append({"order": "o00001", "state": "cancelled"})
    return orders, trips


def _rows():
    orders, trips = _fixture()
    return fitmod.rows_from_docs(orders, trips, reference_time="2020-01-01 08:00:00", step_seconds=STEP)


def test_rows_from_docs_join_and_skips():
    rows, skipped = _rows()
    assert len(rows) == 4 * 3 * 40
    assert skipped == Counter({"no_order": 1, "not_completed": 1})
    assert {(r.pickup, r.dropoff) for r in rows} == {("CT", "CU"), ("CU", "MT"), ("MT", "CT")}
    assert all(r.deadhead_km == pytest.approx(2.0, abs=0.01) for r in rows)
    assert all(r.deadhead_s == pytest.approx(1340.0) for r in rows)
    assert {r.day for r in rows} == {0, 1, 2, 3}


def test_rows_are_input_order_independent():
    orders, trips = _fixture()
    a, _ = fitmod.rows_from_docs(orders, trips, reference_time="2020-01-01 08:00:00", step_seconds=STEP)
    b, _ = fitmod.rows_from_docs(
        orders, list(reversed(trips)), reference_time="2020-01-01 08:00:00", step_seconds=STEP
    )
    assert a == b


def test_fit_recovers_pair_medians_and_holdout_mae():
    rows, _ = _rows()
    model = fitmod.fit(rows, min_cell_n=5)
    pair = model["job_seconds"]["pair"]
    for key, base in (("CT|CU", 9000.0), ("CU|MT", 45000.0), ("MT|CT", 30000.0)):
        assert pair[key][0] == pytest.approx(base, abs=120.0)
        assert pair[key][1] == 160
    assert model["deadhead"]["intercept_s"] + 2.0 * model["deadhead"]["per_km_s"] == pytest.approx(
        1340.0, abs=1.0
    )
    report = fitmod.holdout_by_day(rows, min_cell_n=5)
    overall = report["overall"]
    # noise is U(0, 600) in magnitude with a random sign: MAE ~ 300 s, |res| P90 ~ 540 s
    assert overall["n"] == len(rows)
    assert 200.0 < overall["mae_s"] < 420.0
    assert overall["p90_abs_s"] < 700.0
    assert set(report["per_day"]) == {"0", "1", "2", "3"}
    assert set(report["by_group"]["pair"]) == {"CT->CU", "CU->MT", "MT->CT"}
    assert set(report["by_group"]["depot_leg"]) == {"touches_MT", "no_MT"}


def test_render_round_trip_is_deterministic_and_loadable():
    rows, skipped = _rows()
    kw = dict(
        skipped=skipped,
        run_id="run_synthetic",
        scenario="synthetic",
        fit_date="2026-10-25",
        baseline={"note": "fixture", "problems": []},
        min_cell_n=5,
    )
    src1, model1, report1 = fitmod.build(rows, **kw)
    src2, _, _ = fitmod.build(list(reversed(rows)), **kw)
    assert src1 == src2
    namespace: dict = {}
    exec(compile(src1, "time_model_data.py", "exec"), namespace)
    assert namespace["RUN_ID"] == "run_synthetic"
    assert namespace["N"] == len(rows)
    assert namespace["MODEL"] == model1
    assert namespace["DIGEST"] == tm.model_digest(model1) == TimeModel(namespace["MODEL"]).digest
    assert namespace["MAE_SECONDS"] == report1["holdout"]["overall"]["mae_s"]
    assert namespace["RECOMMENDED_LATE_SLACK_MIN"] == fitmod.recommend_late_slack_min(
        namespace["P90_SIGNED_SECONDS"]
    )


@pytest.mark.parametrize("p90, minutes", [(0, 0), (-50, 0), (1, 5), (300, 5), (301, 10), (21120, 355)])
def test_recommend_late_slack(p90, minutes):
    assert fitmod.recommend_late_slack_min(p90) == minutes


def test_quantile_nearest_rank():
    assert fitmod.quantile([], 0.9) == 0.0
    assert fitmod.quantile(list(range(1, 11)), 0.9) == 9.0
    assert fitmod.quantile([5.0], 0.9) == 5.0


def test_baseline_checks():
    ok = {"status": "success", "meta": {"simulation_settings": {"AGENT_TRANSPORT": "paho-shared"}}}
    assert fitmod.baseline_checks(ok, {"repositioning_to_pickup": {"osrm": 10}}) == []
    bad = {"status": "failed", "meta": {"simulation_settings": {"AGENT_TRANSPORT": "mosquitto"}}}
    problems = fitmod.baseline_checks(bad, {"loaded_to_dropoff": {"osrm": 9, "unavailable": 1}})
    assert len(problems) == 3
    assert any("1/10" in p for p in problems)


def test_fit_rejects_empty():
    with pytest.raises(ValueError):
        fitmod.fit([])
