"""ScenarioManager must honor scenario_meta.json when behavior cache is stale."""

import json
import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario.frontend_scenario_spec import write_meta
from apps.container_logistics.scenario.scenario_manager import ScenarioManager


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="scenario_reload_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, "container-logistics-sim-test", "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _write_minimal_behaviors(scenario_path: str, *, trucks: int, orders: int, facilities: int) -> None:
    os.makedirs(scenario_path, exist_ok=True)
    trucks_map = {f"truck_{i:06d}": {"profile": {}} for i in range(trucks)}
    orders_map = {
        f"order_{i:06d}": {"request_time_step": i, "profile": {}} for i in range(orders)
    }
    facilities_map = {f"facility_{i:03d}": {"profile": {}} for i in range(facilities)}
    for fname, payload in (
        ("truck_behavior.json", trucks_map),
        ("order_behavior.json", orders_map),
        ("facility_behavior.json", facilities_map),
        ("assignment_behavior.json", {"assignment_main": {"profile": {}}}),
        ("analytics_behavior.json", {"analytics_000": {"profile": {}}}),
    ):
        with open(os.path.join(scenario_path, fname), "w", encoding="utf-8") as fp:
            json.dump(payload, fp)
    with open(os.path.join(scenario_path, "orsim_settings.json"), "w", encoding="utf-8") as fp:
        json.dump(
            {
                "DOMAIN": "container-logistics-sim-test",
                "SIMULATION_DAYS": 7,
                "SIMULATION_LENGTH_IN_STEPS": 2520,
                "STEP_INTERVAL": 240,
                "REFERENCE_TIME": "2020-01-01 04:00:00",
            },
            fp,
        )


def test_stale_cache_regenerates_from_scenario_meta(datahub_tmp):
    domain = "container-logistics-sim-test"
    slug = "meta_mismatch"
    # Legacy six-file layout placed under the new scenarios/ root (the manager now
    # reads scenarios/<slug>/) — exercises the back-compat path: legacy files + meta
    # with a stale cache regenerate from the meta recipe into a scenario.json bundle.
    scenario_path = os.path.join(datahub_tmp, domain, "scenarios", slug)
    _write_minimal_behaviors(scenario_path, trucks=500, orders=35000, facilities=15)
    write_meta(
        scenario_path,
        {
            "name": "Two hundred trucks",
            "slug": slug,
            "domain": domain,
            "simulationDays": 7,
            "source": "frontend",
            "status": "complete",
            "agents": {
                "truck": {"count": 4},
                "order": {"count": 12},
                "facility": {"count": 2},
            },
        },
    )

    mgr = ScenarioManager(datahub_tmp, slug, domain)
    mgr.load_or_generate_behaviors()

    assert len(mgr.get_agent_collection("truck")) == 4
    assert len(mgr.get_agent_collection("order")) == 12
    assert len(mgr.get_agent_collection("facility")) == 2
    # Regeneration persists the self-contained bundle; settings carry GENERATION_SPEC.
    from apps.container_logistics.scenario.scenario_bundle import load_bundle

    _collections, settings, _recipe = load_bundle(scenario_path)
    assert settings["GENERATION_SPEC"]["agents"]["truck"]["count"] == 4


def test_build_generation_spec_from_meta_roundtrip(datahub_tmp):
    from apps.container_logistics.scenario.frontend_scenario_spec import build_generation_spec_from_meta

    meta = {
        "name": "UI scenario",
        "slug": "ui_scenario",
        "source": "frontend",
        "simulationDays": 3,
        "agents": {
            "truck": {"count": 200},
            "order": {"count": 500},
            "facility": {"count": 14},
        },
    }
    spec = build_generation_spec_from_meta(meta)
    assert spec is not None
    assert spec["agents"]["truck"]["count"] == 200
    assert spec["agents"]["order"]["count"] == 500
    assert spec["simulationDays"] == 3
    assert spec["frozen"] is True
