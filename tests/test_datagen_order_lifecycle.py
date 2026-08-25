"""WP6: the ``order_lifecycle`` service-role agent (one instance per run, mirrors the
existing ``assignment``/``analytics`` roles).

Covers: ScenarioGenerator produces exactly one ``order_lifecycle_main`` agent with the
pinned default email/persona/profile; a compile -> load bundle round-trip preserves the
``order_lifecycle`` collection; and a bundle dict predating this role (no
``order_lifecycle`` key under ``agents`` at all) loads to ``{}`` rather than raising —
the back-compat pin required because old compiled scenario bundles have no such key.
"""

import os
import shutil
import tempfile
from copy import deepcopy

import pytest

from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor
from apps.container_logistics.scenario import scenario_bundle

DOMAIN = "container-logistics-sim-test"

BASE_SPEC = {
    "name": "Order Lifecycle Gate Test",
    "simulationDays": 1,
    "seed": 12345,
    "solver": "GreedyNearest",
    "hauliers": [
        {"name": "Patrick Inc", "fleet_share": 60, "order_share": 60},
        {"name": "Global Inc", "fleet_share": 40, "order_share": 40},
    ],
    "agents": {
        "truck": {"count": 4, "policy": {"type": "default"}},
        "order": {
            "count": 8,
            "orderCountUnit": "total",
            "policy": {
                "type": "historical",
                "matrix": {"CT": {"CU": 15, "MT": 4}, "CU": {"CT": 11, "MT": 14}, "MT": {"CT": 3, "CU": 14}},
            },
        },
        "facility": {"count": 3, "policy": {"type": "allocate"}},
    },
}


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="datagen_order_lifecycle_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _compile(spec=None):
    return Preprocessor.compile(deepcopy(spec or BASE_SPEC), domain=DOMAIN)


# ---- generation: one order_lifecycle_main agent with pinned defaults ----------------

def test_generate_includes_one_order_lifecycle_agent():
    compiled = _compile()
    result = ScenarioGenerator(compiled.spec, catalog=Preprocessor.catalog()).generate()

    assert list(result.order_lifecycle.keys()) == ["order_lifecycle_main"]
    agent = result.order_lifecycle["order_lifecycle_main"]
    assert agent["email"] == "order_lifecycle_main@test.com"
    assert agent["password"] == "password"
    assert agent["persona"] == {"role": "order_lifecycle", "domain": DOMAIN}
    assert agent["steps_per_action"] == 1
    assert agent["response_rate"] == 1.0
    assert agent["step_only_on_events"] is False
    # Pinned default profile — a drift test in the wiring module checks against this
    # same constant, so it must not change casually.
    assert agent["profile"] == {"haulier_filter": None, "sweep_interval_steps": 30}


def test_order_lifecycle_included_in_collections_and_write(tmp_path):
    compiled = _compile()
    result = ScenarioGenerator(compiled.spec, catalog=Preprocessor.catalog()).generate()

    assert "order_lifecycle" in result.collections()
    assert result.collections()["order_lifecycle"] == result.order_lifecycle

    out_dir = tmp_path / "written"
    result.write(str(out_dir))
    assert os.path.isfile(out_dir / "order_lifecycle_behavior.json")


# ---- compile -> load bundle round-trip ----------------------------------------------

def test_compile_to_bundle_round_trips_order_lifecycle(datahub_tmp):
    from apps.container_logistics.scenario import frontend_scenario_spec as fe

    slug = "order-lifecycle-roundtrip"
    fe.generate_scenario(
        datahub_tmp,
        DOMAIN,
        fe.normalize_generate_spec(
            {
                "name": "Order Lifecycle Roundtrip",
                "slug": slug,
                "simulationDays": 1,
                "agents": {"truck": {"count": 3}, "order": {"count": 6}, "facility": {"count": 2}},
                "earlyOrderCount": 0,
            }
        ),
        overwrite=True,
    )
    folder = fe.scenario_dir(datahub_tmp, DOMAIN, slug)

    # The primary five-role collections dict is unaffected (back-compat shape).
    collections, _settings, _recipe = scenario_bundle.load_bundle(folder)
    assert set(collections) == {"truck", "order", "facility", "assignment", "analytics"}

    # order_lifecycle round-trips via the dedicated optional-collection accessor.
    order_lifecycle = scenario_bundle.load_bundle_collection(folder, "order_lifecycle")
    assert list(order_lifecycle.keys()) == ["order_lifecycle_main"]
    assert order_lifecycle["order_lifecycle_main"]["email"] == "order_lifecycle_main@test.com"

    # And ScenarioManager exposes it through get_agent_collection, like assignment/analytics.
    from apps.container_logistics.scenario.scenario_manager import ScenarioManager

    mgr = ScenarioManager(datahub_tmp, slug, DOMAIN)
    assert mgr.get_agent_collection("order_lifecycle") == order_lifecycle


# ---- back-compat: a bundle predating order_lifecycle loads to {} --------------------

def test_bundle_without_order_lifecycle_key_loads_to_empty_dict(datahub_tmp):
    # Also exercisable as a pure dict fixture (no disk / no scenario pipeline at all):
    # a bundle whose "agents" block simply has no "order_lifecycle" key.
    old_style_agents_only_bundle = {
        "agents": {
            "truck": {"truck_000": {"email": "t@test.com"}},
            "order": {"order_000": {"email": "o@test.com"}},
            "facility": {"facility_000": {"email": "f@test.com"}},
            "assignment": {"assignment_main": {"email": "a@test.com"}},
            "analytics": {"analytics_000": {"email": "an@test.com"}},
            # note: no "order_lifecycle" key here at all
        },
        "settings": {"SIMULATION_LENGTH_IN_STEPS": 10, "STEP_INTERVAL": 240},
    }
    fixture_dir = os.path.join(datahub_tmp, "fixture-only")
    scenario_bundle.write_bundle(fixture_dir, old_style_agents_only_bundle)
    assert scenario_bundle.load_bundle_collection(fixture_dir, "order_lifecycle") == {}
    collections, _settings, _recipe = scenario_bundle.load_bundle(fixture_dir)
    assert set(collections) == {"truck", "order", "facility", "assignment", "analytics"}
    assert collections["truck"] == {"truck_000": {"email": "t@test.com"}}

    # Realistic path: a real compiled scenario, then strip "order_lifecycle" back out
    # of the on-disk bundle to simulate one compiled by pre-WP6 code, and reload it
    # through the full ScenarioManager (counts still match the spec, so this reuses
    # the persisted bundle rather than regenerating — same pattern as
    # test_scenario_bundle.py::test_reload_uses_bundle_not_regenerate).
    from apps.container_logistics.scenario import frontend_scenario_spec as fe
    from apps.container_logistics.scenario.scenario_manager import ScenarioManager

    slug = "pre-wp6-bundle"
    fe.generate_scenario(
        datahub_tmp,
        DOMAIN,
        fe.normalize_generate_spec(
            {
                "name": "Pre WP6 Bundle",
                "slug": slug,
                "simulationDays": 1,
                "agents": {"truck": {"count": 3}, "order": {"count": 6}, "facility": {"count": 2}},
                "earlyOrderCount": 0,
            }
        ),
        overwrite=True,
    )
    folder = fe.scenario_dir(datahub_tmp, DOMAIN, slug)
    bundle = scenario_bundle.read_bundle(folder)
    assert "order_lifecycle" in bundle["agents"], "expected WP6 generation to have written it"
    del bundle["agents"]["order_lifecycle"]
    scenario_bundle.write_bundle(folder, bundle)

    assert scenario_bundle.load_bundle_collection(folder, "order_lifecycle") == {}

    mgr = ScenarioManager(datahub_tmp, slug, DOMAIN)
    assert mgr.get_agent_collection("order_lifecycle") == {}
    # Never raises a KeyError, and the other roles are unaffected.
    assert len(mgr.get_agent_collection("truck")) == 3


# --------------------------------------------------------------------------- single-parse pin


def test_run_load_parses_the_bundle_exactly_once(tmp_path, monkeypatch):
    """A second read_bundle() on the run-load path re-parses a multi-hundred-MB JSON file on
    EVERY run (both lifecycle modes). Pin one parse per ScenarioManager construction."""
    import os

    from apps.container_logistics.scenario import scenario_bundle
    from apps.container_logistics.scenario.scenario_manager import ScenarioManager
    from apps.simulation.container_logistics_wiring import get_domain

    scenarios_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scenarios")
    slugs = [
        d for d in sorted(os.listdir(scenarios_root))
        if os.path.isfile(os.path.join(scenarios_root, d, "scenario.json"))
    ]
    if not slugs:
        import pytest

        pytest.skip("no compiled scenario bundle on disk")

    calls = []
    real_read = scenario_bundle.read_bundle

    def counting_read(scenario_dir):
        calls.append(scenario_dir)
        return real_read(scenario_dir)

    monkeypatch.setattr(scenario_bundle, "read_bundle", counting_read)

    sm = ScenarioManager(
        os.path.join(os.path.dirname(scenarios_root), "datahub"), slugs[0], domain=get_domain()
    )

    assert len(calls) == 1, f"bundle parsed {len(calls)}x on run load (must be exactly 1)"
    # ...and the optional role still comes through from that single parse.
    assert isinstance(sm.get_agent_collection("order_lifecycle"), dict)
    assert len(sm.get_agent_collection("truck")) > 0


def test_load_bundle_with_extras_returns_both_from_one_parse(monkeypatch):
    from apps.container_logistics.scenario import scenario_bundle

    bundle = {
        "agents": {
            **{k: {f"{k}_0": {}} for k in scenario_bundle.COLLECTION_KEYS},
            "order_lifecycle": {"order_lifecycle_main": {"email": "x@test.com"}},
        },
        "settings": {"SIMULATION_LENGTH_IN_STEPS": 10},
        "recipe": {"slug": "s"},
    }
    calls = []
    monkeypatch.setattr(
        scenario_bundle, "read_bundle", lambda d: (calls.append(d), bundle)[1]
    )

    collections, settings, recipe, extras = scenario_bundle.load_bundle_with_extras("/x")

    assert len(calls) == 1
    assert set(collections) == set(scenario_bundle.COLLECTION_KEYS)
    assert extras["order_lifecycle"] == {"order_lifecycle_main": {"email": "x@test.com"}}
    assert settings["SIMULATION_LENGTH_IN_STEPS"] == 10 and recipe == {"slug": "s"}

    # load_bundle keeps its five-key contract and is still a single parse.
    calls.clear()
    coll, _s, _r = scenario_bundle.load_bundle("/x")
    assert set(coll) == set(scenario_bundle.COLLECTION_KEYS)
    assert len(calls) == 1
