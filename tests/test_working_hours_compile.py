"""F5 compile side: ``hauliers[].workingHours`` in spec.json -> ``profile["working_hours"]`` (WS-D).

Pinned:

- the key is CARRIED only when present (both normalisers: ``datagen.hauliers`` and the
  editor/save path ``scenario_config``), and a spec without it -- or with ``null`` --
  compiles to the identical bundle;
- with hours, the assignment profile gets ``{"axis": "elapsed", "hauliers": {hid:
  [start_s, end_s]}}`` (seconds into the elapsed day, overnight = end < start; a
  haulier without hours is absent), the recipe echoes the authored form, and a
  recompile from the bundle's recipe reproduces it;
- refusal **R9** for a malformed window (text pinned), surfaced as ``SpecValidationError``;
- the sim main stamps ``WORKING_HOURS`` (the compiled record) only when present.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from apps.container_logistics.datagen import hauliers as H
from apps.container_logistics.datagen.preprocess import SpecValidationError
from apps.container_logistics.scenario import scenario_config as SC

REPO = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO / "scenarios" / "collab_smoke_12t" / "spec.json"
DOMAIN = "container-logistics-sim"


def _compile(spec, folder):
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    folder.mkdir(parents=True, exist_ok=True)
    return compile_spec_to_bundle(copy.deepcopy(spec), domain=DOMAIN, scenario_dir=str(folder),
                                  slug=spec["slug"], )


def _assignment_profile(bundle):
    return next(iter(bundle["agents"]["assignment"].values()))["profile"]


def _without_clock(bundle):
    b = copy.deepcopy(bundle)
    b.pop("createdAt", None)
    return json.dumps(b, sort_keys=True)


@pytest.fixture(scope="module")
def spec():
    return json.loads(SMOKE_SPEC.read_text())


@pytest.fixture(scope="module")
def plain(spec, tmp_path_factory):
    return _compile(spec, tmp_path_factory.mktemp("plain") / spec["slug"])


# --- normalisation ---------------------------------------------------------------------


@pytest.mark.parametrize("normalize", [H.normalize_hauliers, SC.normalize_hauliers])
def test_carried_only_when_present(normalize):
    raw = [{"id": "a", "fleet_share": 50, "order_share": 50},
           {"id": "b", "fleet_share": 50, "order_share": 50, "workingHours": {"start": "06:00", "end": "20:00"}},
           {"id": "c", "fleet_share": 0, "order_share": 0, "workingHours": None}]
    out = normalize(raw)
    assert set(out[0]) == {"id", "name", "fleet_share", "order_share"}
    assert out[1]["workingHours"] == {"start": "06:00", "end": "20:00"}
    assert "workingHours" not in out[2]
    assert normalize(out) == out, "idempotent (distribute_by_share re-normalises)"


@pytest.mark.parametrize("bad", [
    {"start": "06:00", "end": "06:00"}, {"start": "6:00", "end": "20:00"}, {"start": "06:00"},
    {"start": "24:00", "end": "06:00"}, {"start": "06:60", "end": "20:00"}, {"start": "06:00", "end": "25:00"},
    {"start": "06:00", "end": "20:00", "tz": "x"}, "06:00-20:00", [6, 20], {"start": 6, "end": 20},
])
@pytest.mark.parametrize("normalize", [H.normalize_hauliers, SC.normalize_hauliers])
def test_r9(bad, normalize):
    with pytest.raises(ValueError) as exc:
        normalize([{"id": "acme", "workingHours": bad}])
    assert str(exc.value) == (
        f"Haulier 'acme': workingHours must be {{\"start\": \"HH:MM\", \"end\": \"HH:MM\"}} "
        f"with start != end (got {bad!r})."
    )


def test_the_record():
    hl = H.normalize_hauliers([
        {"id": "day", "fleet_share": 25, "order_share": 25, "workingHours": {"start": "06:00", "end": "20:00"}},
        {"id": "night", "fleet_share": 25, "order_share": 25, "workingHours": {"start": "20:00", "end": "06:00"}},
        {"id": "late", "fleet_share": 25, "order_share": 25, "workingHours": {"start": "12:30", "end": "24:00"}},
        {"id": "always", "fleet_share": 25, "order_share": 25},
    ])
    assert H.working_hours_record(hl) == {"axis": "elapsed", "hauliers": {
        "day": [21600, 72000], "night": [72000, 21600], "late": [45000, 86400]}}
    assert H.working_hours_record(H.normalize_hauliers(None)) is None


# --- compile ----------------------------------------------------------------------------


def test_absent_or_null_compiles_to_the_same_bundle(spec, plain, tmp_path):
    assert "working_hours" not in _assignment_profile(plain)
    nulled = copy.deepcopy(spec)
    for h in nulled["hauliers"]:
        h["workingHours"] = None
    assert _without_clock(_compile(nulled, tmp_path / spec["slug"])) == _without_clock(plain)


def test_hours_compile_into_the_profile_and_round_trip(spec, plain, tmp_path):
    authored = copy.deepcopy(spec)
    authored["hauliers"][0]["workingHours"] = {"start": "06:00", "end": "20:00"}
    authored["hauliers"][1]["workingHours"] = {"start": "22:00", "end": "05:30"}
    bundle = _compile(authored, tmp_path / "a" / spec["slug"])
    record = _assignment_profile(bundle)["working_hours"]
    assert record == {"axis": "elapsed", "hauliers": {"acme": [21600, 72000], "borax": [79200, 19800]}}
    assert [h.get("workingHours") for h in bundle["recipe"]["hauliers"]] == [
        {"start": "06:00", "end": "20:00"}, {"start": "22:00", "end": "05:30"}, None]
    # Only the assignment profile and the recipe echo change: trucks/orders/facilities are identical.
    for role in ("truck", "order", "facility"):
        assert json.dumps(bundle["agents"][role], sort_keys=True) == json.dumps(plain["agents"][role], sort_keys=True)
    again = _compile(bundle["recipe"], tmp_path / "b" / spec["slug"])
    assert _assignment_profile(again)["working_hours"] == record


def test_r9_refuses_the_compile(spec, tmp_path):
    bad = copy.deepcopy(spec)
    bad["hauliers"][2]["workingHours"] = {"start": "08:00", "end": "08:00"}
    with pytest.raises(SpecValidationError, match="Haulier 'cargo': workingHours must be"):
        _compile(bad, tmp_path / spec["slug"])
    assert not (tmp_path / spec["slug"] / "scenario.json").exists()


def test_the_editor_save_path_keeps_the_key():
    from apps.container_logistics.scenario.frontend_scenario_spec import assemble_spec

    raw = {"name": "x", "hauliers": [{"id": "a", "workingHours": {"start": "06:00", "end": "20:00"}}]}
    assert assemble_spec(raw, DOMAIN)["hauliers"][0]["workingHours"] == {"start": "06:00", "end": "20:00"}


# --- the sim main stamp -----------------------------------------------------------------


def _main_inputs(monkeypatch, profile, env=None):
    import types

    import apps.simulation.run_container_logistics_simulation as L

    for key in ("ORSIM_ASSIGNMENT_CADENCE", "ORSIM_MAX_ORDERS_PER_TICK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    coll = {"assignment_main": {"steps_per_action": 28, "profile": copy.deepcopy(profile)}}
    sm = types.SimpleNamespace(orsim_settings={}, get_agent_collection=lambda role: coll)
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        name = L._apply_assignment_run_inputs_or_exit(sm, "base")
    return sm.orsim_settings, name, coll


def test_working_hours_stamp_only_when_present(monkeypatch):
    record = {"axis": "elapsed", "hauliers": {"acme": [21600, 72000]}}
    settings, name, _ = _main_inputs(monkeypatch, {"working_hours": record})
    assert settings == {"WORKING_HOURS": record} and name == "base"
    settings, name, _ = _main_inputs(monkeypatch, {})
    assert settings == {} and name == "base"
