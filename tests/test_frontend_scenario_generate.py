import json
import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario.frontend_scenario_spec import (
    delete_scenario,
    generate_scenario,
    get_scenario_detail,
    normalize_generate_spec,
    PLATFORM_DEFAULT_SCENARIO_SLUG,
    PROTECTED_SCENARIO_SLUGS,
)
from apps.container_logistics.scenario.order_demand import (
    hourly_weights_from_spec,
    recommended_early_order_count,
)
from apps.container_logistics.scenario.scenario_bundle import load_bundle
from apps.simulation.container_logistics_wiring import get_datahub_dir


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="scenario_gen_")
    # Redirect the (now code-package-anchored) scenarios root into the temp dir so
    # tests stay isolated; mirror the historical <datahub>/<domain>/scenarios shape.
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, "container-logistics-sim-test", "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _scenario_dir(datahub: str, domain: str, slug: str) -> str:
    # New self-contained bundle layout: <domain>/scenarios/<slug>/scenario.json
    return f"{datahub}/{domain}/scenarios/{slug}"


def _read_scenario(datahub: str, domain: str, slug: str):
    """Return (collections, settings) from the compiled scenario.json bundle."""
    collections, settings, _recipe = load_bundle(_scenario_dir(datahub, domain, slug))
    return collections, settings


def test_normalize_manual_order_count():
    spec = normalize_generate_spec(
        {
            "name": "UI test",
            "slug": "ui_test_counts",
            "simulationDays": 2,
            "agents": {
                "truck": {"count": 4},
                "order": {"count": 99},
                "facility": {"count": 3},
            },
        }
    )
    assert spec["agents"]["truck"]["count"] == 4
    assert spec["agents"]["order"]["count"] == 99
    assert spec["agents"]["facility"]["count"] == 3
    assert "linked" not in spec["agents"]["order"]


def test_normalize_clamps_legacy_early_order_count():
    spec = normalize_generate_spec(
        {
            "name": "200 trucks",
            "slug": "two_hundred_trucks",
            "simulationDays": 1,
            "agents": {
                "truck": {"count": 200},
                "order": {"count": 2000},
                "facility": {"count": 15},
            },
            "earlyOrderCount": 600,
        }
    )
    assert spec["earlyOrderCount"] == recommended_early_order_count(200, 2000)


def test_normalize_explicit_counts_and_role_settings():
    spec = normalize_generate_spec(
        {
            "name": "UI test",
            "slug": "ui_test_role",
            "simulationDays": 2,
            "agents": {
                "truck": {"count": 4},
                "order": {"count": 99},
                "facility": {"count": 3},
            },
            "roleSettings": {
                "truck": {"profile": {"truck_size": "40ft", "haulier_name": "TestCo"}},
                "order": {
                    "profile": {"order_type": "export"},
                    "business_hour_start": 8,
                    "business_hour_end": 20,
                    "early_order_count": 10,
                },
            },
        }
    )
    assert spec["agents"]["truck"]["count"] == 4
    assert spec["agents"]["order"]["count"] == 99
    assert spec["agents"]["facility"]["count"] == 3
    assert spec["roleSettings"]["truck"]["profile"]["truck_size"] == "40ft"


def test_generate_scenario_writes_behaviors(datahub_tmp):
    # Demand-curve request-time sampling uses the module RNG; seed it so the
    # peak-hour assertion below is deterministic rather than flaky on 6 orders.
    import random

    random.seed(20260617)
    domain = "container-logistics-sim-test"
    slug = "pytest_ui_gen"
    spec = normalize_generate_spec(
        {
            "name": "Pytest Gen",
            "slug": slug,
            "simulationDays": 1,
            "agents": {
                "truck": {"count": 2},
                "order": {"count": 6},
                "facility": {"count": 2},
            },
            "earlyOrderCount": 0,
            "roleSettings": {
                "truck": {"profile": {"truck_size": "40ft"}},
                "order": {"profile": {"order_type": "export"}},
            },
            "orderDemandCurve": {
                "resolution": "hour",
                "points": [{"hour": h, "weight": (1.0 if h == 9 else 0.1)} for h in range(24)],
            },
        }
    )
    generate_scenario(datahub_tmp, domain, spec, overwrite=True)
    collections, settings = _read_scenario(datahub_tmp, domain, slug)
    trucks = collections["truck"]
    orders = collections["order"]
    facilities = collections["facility"]

    assert len(trucks) == 2
    assert trucks["truck_000000"]["profile"]["truck_size"] == "40ft"
    assert len(orders) == 6
    assert orders["order_000000"]["order_type"] == "export"

    # YD is excluded; active codes are CT/CU/MT with config-layer labels.
    code_to_type = {"CT": "Port", "CU": "Warehouse", "MT": "Depot"}
    for order in orders.values():
        assert order["pickup_code"] in code_to_type
        assert order["delivery_code"] in code_to_type
        assert order["pickup_location_type"] == code_to_type[order["pickup_code"]]
        assert order["dropoff_location_type"] == code_to_type[order["delivery_code"]]
    assert len(facilities) == 2
    assert settings["GENERATION_SPEC"]["agents"]["truck"]["count"] == 2

    hours = [((int(o["request_time_step"]) * 240) // 3600) % 24 for o in orders.values()]
    assert hours.count(9) >= 2

    detail = get_scenario_detail(datahub_tmp, domain, slug)
    assert detail["editForm"]["numTrucks"] == 2
    assert detail["editForm"]["numOrders"] == 6
    assert detail["editForm"]["numFacilities"] == 2


def test_order_count_per_day_multiplies_over_horizon(datahub_tmp):
    """A per-day order count generates count * simulationDays total orders, and the
    editor round-trips the stored per-day value (not the total)."""
    domain = "container-logistics-sim-test"
    slug = "pytest_perday_gen"
    spec = normalize_generate_spec(
        {
            "name": "Pytest PerDay",
            "slug": slug,
            "simulationDays": 3,
            "orderCountUnit": "per_day",
            "agents": {
                "truck": {"count": 2},
                "order": {"count": 5},
                "facility": {"count": 2},
            },
            "earlyOrderCount": 0,
        }
    )
    assert spec["orderCountUnit"] == "per_day"
    # Stored value stays per-day.
    assert spec["agents"]["order"]["count"] == 5

    generate_scenario(datahub_tmp, domain, spec, overwrite=True)
    collections, settings = _read_scenario(datahub_tmp, domain, slug)
    orders = collections["order"]
    # 5 per day * 3 days = 15 total order agents generated.
    assert len(orders) == 15

    # GENERATION_SPEC persists the per-day value + marker; editor shows per-day.
    gen = settings["GENERATION_SPEC"]
    assert gen["orderCountUnit"] == "per_day"
    assert gen["agents"]["order"]["count"] == 5

    detail = get_scenario_detail(datahub_tmp, domain, slug)
    assert detail["editForm"]["numOrders"] == 5


def test_legacy_order_count_no_marker_is_total(datahub_tmp):
    """A spec without orderCountUnit keeps the legacy 'total over run' meaning."""
    domain = "container-logistics-sim-test"
    slug = "pytest_legacy_gen"
    spec = normalize_generate_spec(
        {
            "name": "Pytest Legacy",
            "slug": slug,
            "simulationDays": 3,
            "agents": {
                "truck": {"count": 2},
                "order": {"count": 12},
                "facility": {"count": 2},
            },
            "earlyOrderCount": 0,
        }
    )
    assert spec["orderCountUnit"] == "total"
    generate_scenario(datahub_tmp, domain, spec, overwrite=True)
    collections, _settings = _read_scenario(datahub_tmp, domain, slug)
    orders = collections["order"]
    # No multiply: 12 total. Editor displays it as per-day (12 / 3 days = 4).
    assert len(orders) == 12
    detail = get_scenario_detail(datahub_tmp, domain, slug)
    assert detail["editForm"]["numOrders"] == 4


def test_trip_matrix_normalized_and_diagonal_forced_zero():
    from apps.container_logistics.scenario.location_sampler import parse_trip_matrix

    spec = normalize_generate_spec(
        {
            "name": "Matrix test",
            "slug": "matrix_test",
            "simulationDays": 1,
            "agents": {
                "truck": {"count": 2},
                "order": {"count": 4},
                "facility": {"count": 2},
            },
            # Diagonal weight + denormalized + unknown code should be sanitized.
            "tripMatrix": {
                "CT": {"CT": 99, "CU": 10},
                "CU": {"MT": 5},
                "XX": {"CU": 3},
            },
        }
    )
    matrix = spec["tripMatrix"]
    # Codes are data-driven; unknown "XX" (no real addresses) is dropped.
    total = sum(w for row in matrix.values() for w in row.values())
    assert total == pytest.approx(1.0)
    assert all(row.get(p, 0) == 0 for p, row in matrix.items())  # diagonal forced 0
    assert matrix["CT"]["CU"] == pytest.approx(10 / 15)
    assert matrix["CU"]["MT"] == pytest.approx(5 / 15)

    # The strict (datagen) parser requires a real matrix — empty/all-diagonal raises
    # rather than inventing a hidden default.
    with pytest.raises(ValueError):
        parse_trip_matrix({"CT": {"CT": 7}})

    # The frontend proxy supplies the config-layer observed default instead.
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        parse_trip_matrix as fe_parse_trip_matrix,
    )

    fallback = fe_parse_trip_matrix({"CT": {"CT": 7}})
    assert sum(fallback["CT"].values()) > 0


def test_trip_matrix_round_trips_through_generation_spec(datahub_tmp):
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        build_generation_spec_payload,
    )

    domain = "container-logistics-sim-test"
    slug = "pytest_trip_matrix"
    spec = normalize_generate_spec(
        {
            "name": "Trip Matrix Gen",
            "slug": slug,
            "simulationDays": 1,
            "agents": {
                "truck": {"count": 2},
                "order": {"count": 6},
                "facility": {"count": 2},
            },
            "earlyOrderCount": 0,
            "tripMatrix": {"CT": {"CU": 1}},  # all orders run CT -> CU
        }
    )
    payload = build_generation_spec_payload(spec)
    assert payload["tripMatrix"]["CT"]["CU"] == pytest.approx(1.0)

    generate_scenario(datahub_tmp, domain, spec, overwrite=True)
    collections, _settings = _read_scenario(datahub_tmp, domain, slug)
    orders = collections["order"]
    # With a single CT->CU cell, every generated order must follow it.
    for order in orders.values():
        assert order["pickup_code"] == "CT"
        assert order["delivery_code"] == "CU"

    detail = get_scenario_detail(datahub_tmp, domain, slug)
    rt = detail["editForm"]["tripMatrix"]
    total = sum(w for row in rt.values() for w in row.values())
    assert total == pytest.approx(1.0)
    assert rt["CT"]["CU"] == pytest.approx(1.0)


def test_generate_facility_count_above_template_pool(datahub_tmp):
    domain = "container-logistics-sim-test"
    slug = "pytest_facility_25"
    generate_scenario(
        datahub_tmp,
        domain,
        normalize_generate_spec(
            {
                "name": "25 facilities",
                "slug": slug,
                "simulationDays": 1,
                "agents": {
                    "truck": {"count": 1},
                    "order": {"count": 2},
                    "facility": {"count": 25},
                },
                "earlyOrderCount": 0,
            }
        ),
        overwrite=True,
    )
    collections, _settings = _read_scenario(datahub_tmp, domain, slug)
    facilities = collections["facility"]
    assert len(facilities) == 25


def test_delete_scenario(datahub_tmp):
    domain = "container-logistics-sim-test"
    slug = "pytest_delete_me"
    generate_scenario(
        datahub_tmp,
        domain,
        normalize_generate_spec(
            {
                "name": "Delete me",
                "slug": slug,
                "simulationDays": 1,
                "agents": {
                    "truck": {"count": 1},
                    "order": {"count": 1},
                    "facility": {"count": 1},
                },
            }
        ),
        overwrite=True,
    )
    path = _scenario_dir(datahub_tmp, domain, slug)
    assert os.path.isdir(path)
    delete_scenario(datahub_tmp, domain, slug)
    assert not os.path.exists(path)


def test_delete_blocks_protected_scenario():
    for slug in PROTECTED_SCENARIO_SLUGS:
        with pytest.raises(ValueError, match="protected"):
            delete_scenario("/tmp/unused", "container-logistics-sim-test", slug)


def test_platform_default_slug_constant():
    assert PLATFORM_DEFAULT_SCENARIO_SLUG == "default_container_logistics_7d"
