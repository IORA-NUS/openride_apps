"""Dashboard browse-index (Layer A per-scenario index.json + Layer B _index.json roll-up).

Verifies the list/detail paths read the derived index instead of parsing the multi-MB
bundle, that it self-heals on drift, and that mutations keep it in sync.
"""

import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario import (
    frontend_scenario_spec as fe,
    scenario_bundle,
    scenario_index as idx,
)

DOMAIN = "container-logistics-sim-test"


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="scenario_index_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _spec(slug, *, trucks=3, orders=6, facilities=2):
    return fe.normalize_generate_spec(
        {
            "name": slug.replace("_", " ").title(),
            "slug": slug,
            "simulationDays": 1,
            "agents": {"truck": {"count": trucks}, "order": {"count": orders}, "facility": {"count": facilities}},
            "earlyOrderCount": 0,
            "solver": "GreedyNearest",
        }
    )


def _generate(datahub, slug, **kw):
    fe.generate_scenario(datahub, DOMAIN, _spec(slug, **kw), overwrite=True)
    return fe.scenario_dir(datahub, DOMAIN, slug)


def test_generate_emits_index_and_rollup(datahub_tmp):
    sdir = _generate(datahub_tmp, "alpha")
    root = fe.scenario_root(datahub_tmp, DOMAIN)
    # Layer A: per-scenario index.json present with detail fields.
    detail = idx.read_index(sdir)
    assert detail is not None
    assert detail["entry"]["agents"] == {"truck": 3, "order": 6, "facility": 2}
    assert len(detail["preview"]["truck"]) == 3
    assert detail["editForm"]["solver"] == "GreedyNearest"
    # Layer B: rollup has a list-only entry (no preview/editForm).
    rollup = idx.read_rollup(root)
    assert "alpha" in rollup
    assert "preview" not in rollup["alpha"] and "editForm" not in rollup["alpha"]
    assert rollup["alpha"]["agents"] == {"truck": 3, "order": 6, "facility": 2}


def test_list_does_not_parse_bundle_when_fresh(datahub_tmp, monkeypatch):
    _generate(datahub_tmp, "beta")
    # After generation the rollup is fresh — listing must NOT parse any scenario.json.
    calls = {"n": 0}
    real = scenario_bundle.read_bundle

    def spy(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(scenario_bundle, "read_bundle", spy)
    entries = fe.list_scenarios(datahub_tmp, DOMAIN)
    assert any(e["slug"] == "beta" for e in entries)
    assert calls["n"] == 0, "list_scenarios parsed a bundle despite a fresh index"
    # Internal freshness keys are not leaked to the API.
    assert all("bundleMtime" not in e and "bundleSize" not in e for e in entries)


def test_self_heal_on_drift(datahub_tmp):
    sdir = _generate(datahub_tmp, "gamma", trucks=3)
    root = fe.scenario_root(datahub_tmp, DOMAIN)
    cached_mtime = idx.read_rollup(root)["gamma"]["bundleMtime"]
    # Out-of-band bundle rewrite (changes the recipe-derived displayed count) WITHOUT
    # touching the index — must be caught by the os.stat mtime/size self-heal.
    coll, settings, recipe = scenario_bundle.load_bundle(sdir)
    recipe["agents"]["truck"]["count"] = 9
    bundle = scenario_bundle.compile_bundle(coll, settings, recipe, domain=DOMAIN, slug="gamma", name="Gamma")
    scenario_bundle.write_bundle(sdir, bundle)

    entries = {e["slug"]: e for e in fe.list_scenarios(datahub_tmp, DOMAIN)}
    assert entries["gamma"]["agents"]["truck"] == 9, "list did not self-heal after drift"
    refreshed = idx.read_rollup(root)["gamma"]
    assert refreshed["agents"]["truck"] == 9
    assert refreshed["bundleMtime"] != cached_mtime, "freshness stamp not updated"


def test_detail_served_from_index(datahub_tmp):
    _generate(datahub_tmp, "delta")
    detail = fe.get_scenario_detail(datahub_tmp, DOMAIN, "delta")
    assert detail["agents"] == {"truck": 3, "order": 6, "facility": 2}
    assert len(detail["preview"]["truck"]) == 3
    assert detail["editForm"]["numTrucks"] == 3


def test_delete_removes_rollup_entry(datahub_tmp):
    _generate(datahub_tmp, "epsilon")
    root = fe.scenario_root(datahub_tmp, DOMAIN)
    assert "epsilon" in idx.read_rollup(root)
    fe.delete_scenario(datahub_tmp, DOMAIN, "epsilon")
    assert "epsilon" not in idx.read_rollup(root)


def test_rebuild_rollup_from_scratch(datahub_tmp):
    _generate(datahub_tmp, "one")
    _generate(datahub_tmp, "two")
    root = fe.scenario_root(datahub_tmp, DOMAIN)
    os.remove(os.path.join(root, idx.ROLLUP_FILENAME))  # wipe the roll-up
    out = idx.rebuild_rollup(datahub_tmp, DOMAIN)
    assert out["scenarios"] == 2
    rollup = idx.read_rollup(root)
    assert set(rollup) == {"one", "two"}


def test_legacy_folder_without_bundle_still_lists(datahub_tmp):
    # A folder with no scenario.json (legacy/ridehail) must still appear, via fallback.
    root = fe.scenario_root(datahub_tmp, DOMAIN)
    legacy = os.path.join(root, "legacy_one")
    os.makedirs(legacy)
    import json

    for fname, payload in (
        ("truck_behavior.json", {"truck_000000": {"profile": {}}}),
        ("order_behavior.json", {"order_000000": {"request_time_step": 0, "profile": {}}}),
        ("facility_behavior.json", {"facility_000": {"profile": {}}}),
        ("assignment_behavior.json", {"assignment_main": {"profile": {}}}),
        ("analytics_behavior.json", {"analytics_000": {"profile": {}}}),
        ("orsim_settings.json", {"SIMULATION_DAYS": 1, "STEP_INTERVAL": 240, "SIMULATION_LENGTH_IN_STEPS": 360}),
    ):
        with open(os.path.join(legacy, fname), "w") as fp:
            json.dump(payload, fp)
    slugs = {e["slug"] for e in fe.list_scenarios(datahub_tmp, DOMAIN)}
    assert "legacy_one" in slugs
    # Legacy folder is not indexed (no bundle), so no rollup entry for it.
    assert "legacy_one" not in idx.read_rollup(root)
