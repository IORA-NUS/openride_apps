"""The self-contained scenario.json bundle: compile/load round-trip, single-file
layout, and the optional per-scenario datagen override."""

import json
import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario import scenario_bundle
from apps.container_logistics.scenario.scenario_manager import ScenarioManager


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="scenario_bundle_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, "container-logistics-sim-test", "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _norm(obj):
    """JSON-normalize (tuples -> lists) for faithful comparison across a dump/load."""
    return json.loads(json.dumps(obj))


def _generate(datahub, slug, *, override_src=None):
    domain = "container-logistics-sim-test"
    sdir = os.path.join(datahub, domain, "scenarios", slug)
    os.makedirs(sdir, exist_ok=True)
    if override_src is not None:
        with open(os.path.join(sdir, "scenario_gen.py"), "w", encoding="utf-8") as fp:
            fp.write(override_src)
    mgr = ScenarioManager(datahub, slug, domain, generation_profile="smoke")
    return mgr, sdir


def test_single_file_and_exact_round_trip(datahub_tmp):
    mgr, sdir = _generate(datahub_tmp, "round-trip")

    # Exactly one artifact (no loose behavior files), and it's scenario.json.
    json_files = [f for f in os.listdir(sdir) if f.endswith(".json")]
    assert json_files == ["scenario.json"]

    collections, settings, recipe = scenario_bundle.load_bundle(sdir)
    for role, mem in (
        ("truck", mgr.truck_collection),
        ("order", mgr.order_collection),
        ("facility", mgr.facility_collection),
        ("assignment", mgr.assignment_collection),
        ("analytics", mgr.analytics_collection),
    ):
        assert _norm(mem) == collections[role], f"{role} did not round-trip"
    assert settings["SIMULATION_LENGTH_IN_STEPS"] == mgr.orsim_settings["SIMULATION_LENGTH_IN_STEPS"]


def test_bundle_header_has_counts_and_preview(datahub_tmp):
    _mgr, sdir = _generate(datahub_tmp, "header")
    bundle = scenario_bundle.read_bundle(sdir)
    assert bundle["counts"] == {"truck": 5, "order": 50, "facility": 15}
    assert len(bundle["preview"]["truck"]) == 5
    assert len(bundle["preview"]["order"]) == 10
    # Header (counts/preview) comes before the heavy agents blob.
    keys = list(bundle.keys())
    assert keys.index("counts") < keys.index("agents")
    assert keys.index("preview") < keys.index("agents")


def test_reload_uses_bundle_not_regenerate(datahub_tmp):
    # A frozen frontend scenario reloads its persisted bundle verbatim (does not
    # re-sample). The smoke profile is a poor vehicle here — it intentionally
    # regenerates — so use the real frontend generate path.
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        generate_scenario,
        normalize_generate_spec,
    )

    domain = "container-logistics-sim-test"
    slug = "reload"
    generate_scenario(
        datahub_tmp,
        domain,
        normalize_generate_spec(
            {
                "name": "Reload",
                "slug": slug,
                "simulationDays": 1,
                "agents": {"truck": {"count": 3}, "order": {"count": 6}, "facility": {"count": 2}},
                "earlyOrderCount": 0,
            }
        ),
        overwrite=True,
    )
    sdir = os.path.join(datahub_tmp, domain, "scenarios", slug)
    collections, _settings, _recipe = scenario_bundle.load_bundle(sdir)
    persisted = collections["truck"]["truck_000000"]["init_loc"]

    mgr2 = ScenarioManager(datahub_tmp, slug, domain)
    assert mgr2.truck_collection["truck_000000"]["init_loc"] == persisted


def test_override_customizes_generation_and_records_sha(datahub_tmp):
    base_mgr, _ = _generate(datahub_tmp, "ovr-base")
    base_orders = len(base_mgr.order_collection)
    assert scenario_bundle.read_bundle(
        os.path.join(datahub_tmp, "container-logistics-sim-test", "scenarios", "ovr-base")
    )["integrity"]["generatorOverrideSha256"] is None

    override = (
        "from dataclasses import replace\n"
        "def customize_spec(spec):\n"
        "    return replace(spec, num_orders=spec.num_orders * 2)\n"
        "def customize_catalog(catalog, spec):\n"
        "    catalog.add_manual_site('CT', lat=1.30, lon=103.80, name='Mega Port')\n"
        "    return catalog\n"
    )
    ovr_mgr, ovr_dir = _generate(datahub_tmp, "ovr-doubled", override_src=override)
    assert len(ovr_mgr.order_collection) == base_orders * 2
    assert scenario_bundle.read_bundle(ovr_dir)["integrity"]["generatorOverrideSha256"]
