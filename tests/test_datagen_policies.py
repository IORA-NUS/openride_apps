"""Acceptance gate for the policy-based datagen (plan §9).

Covers: policy==builder equivalence under a shared seed, reproducibility + seed
locality, validation, distribution units, and defaults/demand parity with the
legacy scenario_config (guards the copies made during the collapse).
"""

import random
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import defaults as D
from apps.container_logistics.datagen import demand
from apps.container_logistics.datagen.builders import (
    FacilityBuilder,
    OrderBuilder,
    TruckBuilder,
)
from apps.container_logistics.datagen.distributions import (
    Categorical,
    Curve,
    EmpiricalData,
    Uniform,
)
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.agents.base import PolicyContext
from apps.container_logistics.datagen.agents.facility import AllocateFacilityPolicy, FacilityAgent
from apps.container_logistics.datagen.agents.order import HistoricalOrderPolicy, OrderAgent
from apps.container_logistics.datagen.agents.truck import HistoricalTruckPolicy, TruckAgent
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container-logistics-sim"

BASE_SPEC = {
    "name": "Gate Test",
    "simulationDays": 1,
    "seed": 12345,
    "solver": "GreedyNearest",
    "hauliers": [
        {"name": "Patrick Inc", "fleet_share": 60, "order_share": 60},
        {"name": "Global Inc", "fleet_share": 40, "order_share": 40},
    ],
    "agents": {
        "truck": {"count": 8, "policy": {"type": "default"}},
        "order": {
            "count": 40,
            "orderCountUnit": "total",
            "policy": {
                "type": "historical",
                "matrix": {"CT": {"CU": 15, "MT": 4}, "CU": {"CT": 11, "MT": 14}, "MT": {"CT": 3, "CU": 14}},
            },
        },
        "facility": {"count": 6, "policy": {"type": "allocate"}},
    },
}


def _compile(spec=None):
    return Preprocessor.compile(deepcopy(spec or BASE_SPEC), domain=DOMAIN)


# ---- equivalence: policy wraps builder verbatim under a shared seed ----------

def test_truck_agent_equals_builder():
    """TruckAgent + HistoricalTruckPolicy (default) == old TruckBuilder under a shared seed."""
    spec = _compile().spec
    cat = Preprocessor.catalog()
    n = spec.num_trucks
    hauliers = list(spec.truck_hauliers)
    b = TruckBuilder(spec, cat, random.Random("t"))
    old = {f"truck_{i:06d}": b.build(f"truck_{i:06d}", haulier=hauliers[i]) for i in range(n)}
    ctx = PolicyContext(spec, cat)
    agent = TruckAgent(spec, cat, random.Random("t"), HistoricalTruckPolicy({}, ctx), hauliers=hauliers)
    assert old == agent.generate(n)


def test_order_agent_equals_builder():
    """OrderAgent + HistoricalOrderPolicy (supplied matrix) == old OrderBuilder."""
    spec = _compile().spec
    cat = Preprocessor.catalog()
    n = spec.num_orders
    hauliers = list(spec.order_hauliers)
    b = OrderBuilder(spec, cat, random.Random("o"))
    old = {f"order_{i:06d}": b.build(f"order_{i:06d}", haulier=hauliers[i]) for i in range(n)}
    ctx = PolicyContext(spec, cat)
    agent = OrderAgent(spec, cat, random.Random("o"), HistoricalOrderPolicy({}, ctx), hauliers=hauliers)
    assert old == agent.generate(n)


def test_facility_agent_equals_builder():
    spec = _compile().spec
    cat = Preprocessor.catalog()
    from apps.container_logistics.datagen.builders import resolve_facilities

    count = len(resolve_facilities(spec.facility_settings))
    b = FacilityBuilder(spec, cat, random.Random("f"))
    old = {f"facility_{i:03d}": b.build(f"facility_{i:03d}", facility_index=i) for i in range(count)}
    ctx = PolicyContext(spec, cat)
    agent = FacilityAgent(spec, cat, random.Random("f"), AllocateFacilityPolicy({}, ctx))
    assert old == agent.generate(count)


# ---- reproducibility + seed locality ----------------------------------------

def test_reproducible_same_seed():
    spec = _compile().spec
    r1 = ScenarioGenerator(spec).generate()
    r2 = ScenarioGenerator(spec).generate()
    assert r1.truck == r2.truck
    assert r1.order == r2.order
    assert r1.facility == r2.facility


def test_seed_locality_curve_only_touches_orders():
    """Changing only the demand curve must leave trucks & facilities byte-identical."""
    base = _compile().spec
    r_base = ScenarioGenerator(base).generate()

    spec2 = deepcopy(BASE_SPEC)
    spec2["agents"]["order"]["policy"]["curve"] = {
        "points": [{"hour": h, "weight": (1 if 9 <= h <= 17 else 0)} for h in range(24)]
    }
    changed = _compile(spec2).spec
    r_changed = ScenarioGenerator(changed).generate()

    assert r_base.truck == r_changed.truck, "truck data moved when only the curve changed"
    assert r_base.facility == r_changed.facility, "facility data moved when only the curve changed"
    assert r_base.order != r_changed.order, "orders should reflect the new curve"


def test_different_seed_changes_data():
    s1 = _compile().spec
    spec2 = deepcopy(BASE_SPEC)
    spec2["seed"] = 999
    s2 = _compile(spec2).spec
    r1 = ScenarioGenerator(s1).generate()
    r2 = ScenarioGenerator(s2).generate()
    assert r1.truck != r2.truck


# ---- validation (single site) -----------------------------------------------

def test_missing_name_errors():
    with pytest.raises(SpecValidationError):
        Preprocessor.compile({"agents": {"truck": {"count": 1}}}, domain=DOMAIN)


def test_unknown_policy_type_errors():
    bad = deepcopy(BASE_SPEC)
    bad["agents"]["truck"]["policy"] = {"type": "no_such_policy"}
    with pytest.raises(SpecValidationError):
        Preprocessor.compile(bad, domain=DOMAIN)


def test_historical_without_source_or_matrix_uses_default():
    """`historical` no longer requires a source: with neither source nor authored matrix
    it falls back to the default trip matrix and compiles cleanly."""
    spec = deepcopy(BASE_SPEC)
    spec["agents"]["order"]["policy"] = {"type": "historical"}
    compiled = Preprocessor.compile(spec, domain=DOMAIN)  # must NOT raise
    assert compiled.spec.trip_matrix  # a usable matrix was resolved


def test_haulier_shares_must_sum_100():
    bad = deepcopy(BASE_SPEC)
    bad["hauliers"] = [{"name": "A", "fleet_share": 60, "order_share": 60}]
    with pytest.raises(ValueError):
        Preprocessor.compile(bad, domain=DOMAIN)


# ---- coherence invariants ---------------------------------------------------

def test_order_coherence_and_ranges():
    spec = _compile().spec
    res = ScenarioGenerator(spec).generate()
    sim_end = spec.simulation_length_in_steps - 1
    fac_names = {f["profile"]["name"] for f in res.facility.values()}
    for o in res.order.values():
        assert o["pickup_code"] == o["pickup_facility"]["code"]
        assert o["delivery_code"] == o["dropoff_facility"]["code"]
        assert 0 <= o["request_time_step"] <= sim_end
        assert o["profile"]["pickup_facility_name"] in fac_names
    # haulier counts match distribute_by_share
    from collections import Counter
    from apps.container_logistics.datagen.hauliers import distribute_by_share

    expected = Counter(h["id"] for h in distribute_by_share(spec.num_orders, BASE_SPEC["hauliers"], "order_share"))
    got = Counter(o["haulier_id"] for o in res.order.values())
    assert got == expected


# ---- distribution units -----------------------------------------------------

def test_probability_matrix_from_records_recovers_joint():
    recs = [{"pickup_code": "CT", "dropoff_code": "CU"}] * 3 + [{"pickup_code": "CU", "dropoff_code": "MT"}] * 1
    grid = EmpiricalData(recs).to_probability_matrix().as_grid()
    assert grid["CT"]["CU"] == pytest.approx(0.75)
    assert grid["CU"]["MT"] == pytest.approx(0.25)


def test_distribution_contracts():
    rng = random.Random(1)
    assert Uniform(["a", "b"]).sample(rng) in ("a", "b")
    assert Categorical({"x": 1, "y": 0}).sample(rng) == "x"
    c = Curve.from_hourly_weights([1.0 / 24] * 24)
    assert 0 <= c.sample(rng) < 24
    assert 0 <= c.sample_step(rng, simulation_end=100, simulation_days=1, step_interval_seconds=240) <= 100


# ---- parity with legacy (guards the copies) ---------------------------------

def test_defaults_parity_with_scenario_config():
    from apps.container_logistics.scenario import scenario_config as C

    def strip(s):
        s = deepcopy(s)
        s.get("profile", {}).pop("facilities", None)
        return s

    assert D.TRUCK_SETTINGS == C.truck_settings
    assert D.ORDER_SETTINGS == C.order_settings
    assert strip(D.FACILITY_SETTINGS) == strip(C.facility_settings)
    assert D.ASSIGNMENT_SETTINGS == C.assignment_settings
    assert D.ANALYTICS_SETTINGS == C.analytics_settings
    assert D.DEFAULT_TRIP_MATRIX == C.DEFAULT_TRIP_MATRIX
    assert D.LOCATION_TYPE_METADATA == C.LOCATION_TYPE_METADATA


def test_demand_parity_with_order_demand():
    from apps.container_logistics.scenario import order_demand as OD

    curve = {"points": [{"hour": h, "weight": h} for h in range(24)]}
    assert demand.hourly_weights_from_spec(curve) == OD.hourly_weights_from_spec(curve)
    assert demand.default_peak_hours_curve() == OD.default_peak_hours_curve()


# ---- policy family: random + historical -------------------------------------

def test_random_order_policy_spreads_uniformly():
    spec = deepcopy(BASE_SPEC)
    spec["agents"]["order"] = {"count": 120, "policy": {"type": "random"}}
    res = ScenarioGenerator(_compile(spec).spec).generate()
    from collections import Counter

    pairs = Counter((o["pickup_code"], o["delivery_code"]) for o in res.order.values())
    # uniform should touch most off-diagonal pairs (>= 4 of 6 for CT/CU/MT)
    assert len(pairs) >= 4


def test_historical_order_policy_learns_matrix(tmp_path):
    import json as _json

    recs = [{"pickup_code": "CT", "dropoff_code": "CU"}] * 20 + [{"pickup_code": "MT", "dropoff_code": "CT"}] * 5
    src = tmp_path / "orders.json"
    src.write_text(_json.dumps(recs))
    spec = deepcopy(BASE_SPEC)
    spec["agents"]["order"] = {"count": 100, "policy": {"type": "historical", "source": "orders.json"}}
    compiled = Preprocessor.compile(spec, domain=DOMAIN, scenario_dir=str(tmp_path))
    # learned matrix strongly favors CT->CU
    assert compiled.spec.trip_matrix["CT"]["CU"] > 0.5
    res = ScenarioGenerator(compiled.spec).generate()
    from collections import Counter

    pairs = Counter((o["pickup_code"], o["delivery_code"]) for o in res.order.values())
    assert pairs[("CT", "CU")] > pairs[("MT", "CT")]  # follows the learned distribution


def test_matrix_is_not_a_policy_and_aliases_to_historical():
    """`matrix` was never a policy (a matrix is a Distribution). It's a deprecated alias:
    known-policies must not list it, but an old spec.json using it must still compile and
    the recipe must record the canonical `historical`."""
    from apps.container_logistics.datagen.agents.registry import known_policies

    assert "matrix" not in known_policies()["order"]
    assert known_policies()["order"] == ["historical", "random"]

    spec = deepcopy(BASE_SPEC)
    spec["agents"]["order"]["policy"]["type"] = "matrix"  # legacy name
    compiled = Preprocessor.compile(spec, domain=DOMAIN)
    # normalized to the canonical type in the persisted recipe
    assert compiled.recipe["agents"]["order"]["policy"]["type"] == "historical"


def test_historical_without_source_uses_authored_matrix():
    """`historical` with no `source` samples the authored matrix (the old `matrix` default)."""
    spec = deepcopy(BASE_SPEC)
    spec["agents"]["order"] = {
        "count": 40,
        "orderCountUnit": "total",
        "policy": {"type": "historical", "matrix": {"CT": {"CU": 1.0}, "CU": {"CT": 1.0}}},
    }
    compiled = Preprocessor.compile(spec, domain=DOMAIN)  # must NOT require a source
    # authored matrix honored (normalized to 0.5 across the two off-diagonal pairs),
    # clearly distinct from the uniform ~0.167 a `random` policy would give.
    assert compiled.spec.trip_matrix["CT"]["CU"] > 0.4
