"""The Preprocessor is the single scenario-spec validation surface (2026-07-13).

The active generation path assembles a canonical spec (pure shaping, NO validation)
and hands it to ``Preprocessor.compile``, which is the ONLY place that validates,
clamps, resolves files, and normalizes. These tests pin that contract:

- ``assemble_spec`` never rejects/clamps (pure structural shaping);
- the Preprocessor rejects every bad spec with ``SpecValidationError``;
- over-cap counts are *clamped*, not rejected;
- a compiled scenario's ``spec.json`` is the resolved, self-contained recipe
  (matrix/curve inlined, no ``$file``), and round-trips through edit/recompile;
- slug path-traversal is stopped at the write boundary (filesystem safety);
- the deprecated ``build_generation_spec_from_meta`` legacy path still works.
"""

import json
import os
import shutil
import tempfile

import pytest

from apps.container_logistics.datagen import preprocess as pp
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError
from apps.container_logistics.scenario import frontend_scenario_spec as fe

DOMAIN = "container-logistics-sim-test"


@pytest.fixture
def datahub_tmp():
    tmp = tempfile.mkdtemp(prefix="valsurface_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _spec(slug, **over):
    spec = {
        "name": slug.replace("_", " ").title(),
        "slug": slug,
        "simulationDays": 1,
        "agents": {
            "truck": {"count": 4, "policy": {"type": "default"}},
            "order": {"count": 8, "orderCountUnit": "per_day", "policy": {"type": "historical"}},
            "facility": {"count": 4, "policy": {"type": "allocate"}},
        },
        "hauliers": [
            {"name": "A", "fleet_share": 60, "order_share": 60},
            {"name": "B", "fleet_share": 40, "order_share": 40},
        ],
    }
    spec.update(over)
    return spec


# ---- the assembler does NO validation (pure shaping) -----------------------

def test_assemble_spec_never_rejects():
    # Empty name, over-cap count, bad slug, unknown policy — all pass through
    # untouched; assemble_spec must not raise or clamp.
    raw = {
        "name": "",
        "slug": "../evil",
        "simulationDays": 999,
        "agents": {"order": {"count": 10 ** 9, "policy": {"type": "nope"}}},
        "numTrucks": 7,  # legacy top-level key -> mapped structurally
    }
    out = fe.assemble_spec(raw, DOMAIN)
    assert out["name"] == ""                       # not rejected
    assert out["agents"]["truck"]["count"] == 7    # legacy key mapped
    assert out["agents"]["order"]["count"] == 10 ** 9   # not clamped
    assert out["agents"]["order"]["policy"]["type"] == "nope"  # not validated
    assert out["simulationDays"] == 999            # not clamped


# ---- the Preprocessor rejects every bad spec -------------------------------

@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.update(name=""), "name is required"),
    (lambda s: s["agents"]["order"].__setitem__("policy", {"type": "nope"}), "Unknown order policy"),
    (lambda s: s.__setitem__("hauliers", [{"name": "A", "fleet_share": 50, "order_share": 50},
                                          {"name": "B", "fleet_share": 40, "order_share": 40}]),
     "must sum to 100"),
])
def test_preprocessor_is_the_gate(mutate, needle):
    spec = _spec("gate")
    mutate(spec)
    with pytest.raises(SpecValidationError) as exc:
        Preprocessor.compile(spec, domain=DOMAIN)
    assert needle.lower() in str(exc.value).lower()


def test_preprocessor_rejects_missing_file(tmp_path):
    spec = _spec("missingfile")
    spec["agents"]["order"]["policy"] = {"type": "historical", "matrix": {"$file": str(tmp_path / "nope.json")}}
    with pytest.raises(SpecValidationError) as exc:
        Preprocessor.compile(spec, domain=DOMAIN, scenario_dir=str(tmp_path))
    assert "not found" in str(exc.value).lower()


def test_over_cap_counts_are_clamped_not_rejected():
    spec = _spec("clamp")
    spec["agents"]["truck"]["count"] = pp.MAX_TRUCKS + 5
    spec["simulationDays"] = pp.MAX_SIMULATION_DAYS + 10
    compiled = Preprocessor.compile(spec, domain=DOMAIN)   # must NOT raise
    assert compiled.spec.num_trucks == pp.MAX_TRUCKS
    assert compiled.spec.simulation_days == pp.MAX_SIMULATION_DAYS


# ---- self-contained persistence + round-trip -------------------------------

def test_spec_json_is_resolved_self_contained_recipe(datahub_tmp, tmp_path):
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps({"CT": {"CU": 1.0}, "CU": {"CT": 1.0}}))
    spec = _spec("filematrix")
    spec["agents"]["order"]["policy"] = {"type": "historical", "matrix": {"$file": str(mpath)}}
    fe.generate_scenario(datahub_tmp, DOMAIN, spec, overwrite=True)

    folder = fe.scenario_dir(datahub_tmp, DOMAIN, "filematrix")
    on_disk = fe.read_spec(folder)
    blob = json.dumps(on_disk)
    assert "$file" not in blob, "spec.json still references an external file"
    assert isinstance(on_disk.get("tripMatrix"), dict), "matrix not inlined"
    # The raw matrix ref was stripped from the persisted policy (value lives in tripMatrix).
    assert "matrix" not in on_disk["agents"]["order"]["policy"]
    # Deleting the source file must not break a recompile (self-contained).
    mpath.unlink()
    fe.compile_scenario(datahub_tmp, DOMAIN, "filematrix")


def test_order_unit_round_trips_through_edit(datahub_tmp):
    fe.generate_scenario(datahub_tmp, DOMAIN, _spec("units"), overwrite=True)
    fe.edit_scenario(datahub_tmp, DOMAIN, "units",
                     {"agents": {"order": {"count": 33, "policy": {"type": "historical"}},
                                 "truck": {"count": 4}, "facility": {"count": 4}}})
    on_disk = fe.read_spec(fe.scenario_dir(datahub_tmp, DOMAIN, "units"))
    assert on_disk["agents"]["order"]["count"] == 33
    assert on_disk["agents"]["order"]["orderCountUnit"] == "per_day"  # survived the edit


def test_reseed_draws_new_seed(datahub_tmp):
    fe.generate_scenario(datahub_tmp, DOMAIN, _spec("reseed", seed=123), overwrite=True)
    folder = fe.scenario_dir(datahub_tmp, DOMAIN, "reseed")
    assert fe.read_spec(folder)["seed"] == 123
    fe.compile_scenario(datahub_tmp, DOMAIN, "reseed", reseed=True)
    assert fe.read_spec(folder)["seed"] != 123


# ---- filesystem-path safety stays at the boundary --------------------------

@pytest.mark.parametrize("bad", ["../evil", "a/b", ".."])
def test_slug_path_traversal_blocked_at_boundary(datahub_tmp, bad):
    spec = _spec("valid_name")
    spec["slug"] = bad
    with pytest.raises(ValueError):
        fe.generate_scenario(datahub_tmp, DOMAIN, spec, overwrite=True)


# ---- the deprecated legacy path is untouched -------------------------------

def test_legacy_build_from_meta_still_works():
    meta = {
        "source": "frontend",
        "name": "Legacy Meta",
        "slug": "legacy_meta",
        "simulationDays": 1,
        "orderCountUnit": "total",
        "agents": {"truck": {"count": 3}, "order": {"count": 6}, "facility": {"count": 2}},
        "hauliers": [{"name": "A", "fleet_share": 100, "order_share": 100}],
    }
    out = fe.build_generation_spec_from_meta(meta)
    assert isinstance(out, dict)
    assert out["agents"]["truck"]["count"] == 3
