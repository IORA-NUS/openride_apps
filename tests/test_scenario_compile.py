"""spec.json source + compile/preprocess pipeline.

Verifies: generate writes spec.json; compile_scenario rebuilds scenario.json from the
folder's spec.json; hand-editing spec.json + recompiling takes effect; a scenario_gen.py
override is honored on compile; missing spec.json is synthesized from the embedded recipe.
"""

import json
import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario import (
    frontend_scenario_spec as fe,
    scenario_bundle,
)

DOMAIN = "container-logistics-sim-test"


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="scenario_compile_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _spec(slug, *, trucks=4, orders=8, facilities=3):
    return {
        "name": slug.replace("_", " ").title(),
        "slug": slug,
        "simulationDays": 1,
        "orderCountUnit": "total",
        "agents": {"truck": {"count": trucks}, "order": {"count": orders}, "facility": {"count": facilities}},
        "earlyOrderCount": 0,
        "solver": "GreedyNearest",
    }


def _folder(datahub, slug):
    return fe.scenario_dir(datahub, DOMAIN, slug)


def test_generate_writes_spec_json(datahub_tmp):
    fe.generate_scenario(datahub_tmp, DOMAIN, _spec("gen_spec"), overwrite=True)
    folder = _folder(datahub_tmp, "gen_spec")
    spec = fe.read_spec(folder)
    assert spec is not None, "spec.json was not written"
    assert spec["agents"]["truck"]["count"] == 4
    assert spec["solver"] == "GreedyNearest"
    # The run artifact exists and its recipe mirrors the spec.
    _coll, _settings, recipe = scenario_bundle.load_bundle(folder)
    assert recipe["agents"]["truck"]["count"] == 4


def test_compile_from_spec_reproduces_bundle(datahub_tmp):
    fe.generate_scenario(datahub_tmp, DOMAIN, _spec("recompile", trucks=4, orders=8), overwrite=True)
    folder = _folder(datahub_tmp, "recompile")
    # Hand-edit spec.json (bump trucks) and recompile from the folder alone.
    spec = fe.read_spec(folder)
    spec["agents"]["truck"]["count"] = 7
    fe.write_spec(folder, spec)
    entry = fe.compile_scenario(datahub_tmp, DOMAIN, "recompile")
    assert entry["agents"]["truck"] == 7
    coll, _settings, _recipe = scenario_bundle.load_bundle(folder)
    assert len(coll["truck"]) == 7, "compile did not regenerate from edited spec.json"


def test_compile_honors_scenario_gen_override(datahub_tmp):
    fe.generate_scenario(datahub_tmp, DOMAIN, _spec("ovr", orders=8), overwrite=True)
    folder = _folder(datahub_tmp, "ovr")
    base_orders = len(scenario_bundle.load_bundle(folder)[0]["order"])
    # Drop an override that doubles orders, then recompile.
    with open(os.path.join(folder, "scenario_gen.py"), "w", encoding="utf-8") as fp:
        fp.write("from dataclasses import replace\n"
                 "def customize_spec(spec):\n"
                 "    return replace(spec, num_orders=spec.num_orders * 2)\n")
    fe.compile_scenario(datahub_tmp, DOMAIN, "ovr")
    coll = scenario_bundle.load_bundle(folder)[0]
    assert len(coll["order"]) == base_orders * 2
    # Override sha recorded in the (recompiled) bundle integrity block.
    assert scenario_bundle.read_bundle(folder)["integrity"]["generatorOverrideSha256"]


def test_compile_synthesizes_missing_spec(datahub_tmp):
    fe.generate_scenario(datahub_tmp, DOMAIN, _spec("synth", trucks=5), overwrite=True)
    folder = _folder(datahub_tmp, "synth")
    # Remove spec.json — the recipe is still mirrored inside scenario.json.
    os.remove(os.path.join(folder, fe.SPEC_FILENAME))
    assert fe.read_spec(folder) is None
    entry = fe.compile_scenario(datahub_tmp, DOMAIN, "synth")
    assert entry["agents"]["truck"] == 5
    # spec.json is now materialized from the embedded recipe.
    assert fe.read_spec(folder) is not None


def test_historical_source_is_staged_and_portable(datahub_tmp, tmp_path):
    # A historical order policy pointing at an ABSOLUTE records file outside the folder.
    recs = tmp_path / "orders_history.json"
    recs.write_text(json.dumps([{"pickup_code": "CT", "dropoff_code": "CU"}] * 6
                               + [{"pickup_code": "CU", "dropoff_code": "CT"}] * 2))
    spec = _spec("histport", orders=8)
    spec["agents"]["order"]["policy"] = {"type": "historical", "source": str(recs)}
    fe.generate_scenario(datahub_tmp, DOMAIN, spec, overwrite=True)

    folder = _folder(datahub_tmp, "histport")
    on_disk = fe.read_spec(folder)
    # The absolute ref was staged into inputs/ and rewritten to a folder-relative path.
    assert on_disk["agents"]["order"]["policy"]["source"] == os.path.join("inputs", "orders_history.json")
    assert os.path.isfile(os.path.join(folder, "inputs", "orders_history.json"))
    assert str(tmp_path) not in json.dumps(on_disk), "an absolute path leaked into spec.json"
    # Deleting the original source must NOT break a recompile (folder-portable).
    recs.unlink()
    fe.compile_scenario(datahub_tmp, DOMAIN, "histport")


def test_generation_consistency_guard():
    """The compile guard refuses to persist a bundle whose agents don't match the spec —
    the safeguard for the corrupt 'everything on the default haulier' bundle bug."""
    from types import SimpleNamespace
    from apps.container_logistics.scenario.spec_compile import _assert_generation_consistent
    from apps.container_logistics.datagen.preprocess import SpecValidationError

    compiled = SimpleNamespace(
        spec=SimpleNamespace(num_trucks=2, num_orders=2),
        recipe={"hauliers": [{"id": "patrick_inc"}, {"id": "global_inc"}]},
    )
    good = SimpleNamespace(
        truck={"t0": {"profile": {"haulier_id": "patrick_inc"}}, "t1": {"profile": {"haulier_id": "global_inc"}}},
        order={"o0": {"haulier_id": "patrick_inc"}, "o1": {"haulier_id": "global_inc"}},
    )
    _assert_generation_consistent(good, compiled, has_gen_override=False)  # passes

    # the test5000 failure: every agent collapsed onto the default 'haulier'
    corrupt = SimpleNamespace(
        truck={"t0": {"profile": {"haulier_id": "haulier"}}, "t1": {"profile": {"haulier_id": "haulier"}}},
        order={"o0": {"haulier_id": "haulier"}, "o1": {"haulier_id": "haulier"}},
    )
    with pytest.raises(SpecValidationError):
        _assert_generation_consistent(corrupt, compiled, has_gen_override=False)
    # a Tier-2 scenario_gen.py override bypasses the guard (it may change hauliers/counts)
    _assert_generation_consistent(corrupt, compiled, has_gen_override=True)

    # count mismatch is caught too
    short = SimpleNamespace(
        truck={"t0": {"profile": {"haulier_id": "patrick_inc"}}},
        order={"o0": {"haulier_id": "patrick_inc"}, "o1": {"haulier_id": "global_inc"}},
    )
    with pytest.raises(SpecValidationError):
        _assert_generation_consistent(short, compiled, has_gen_override=False)


def test_run_artifact_still_only_scenario_json(datahub_tmp):
    # The compile inputs (spec.json/scenario_gen.py) live in the folder, but the bundle
    # is self-contained: load_bundle reads only scenario.json.
    fe.generate_scenario(datahub_tmp, DOMAIN, _spec("selfcontained"), overwrite=True)
    folder = _folder(datahub_tmp, "selfcontained")
    coll, settings, recipe = scenario_bundle.load_bundle(folder)
    assert set(coll) == {"truck", "order", "facility", "assignment", "analytics"}
    assert settings.get("SIMULATION_LENGTH_IN_STEPS")
