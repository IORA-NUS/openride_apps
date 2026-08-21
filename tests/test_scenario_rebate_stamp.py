"""Phase 3 — rebate provenance (plan §9, §11 Phase 3).

A run must explain its own pricing, from the **effective** compiled artefact rather
than the authored one. These tests drive the real ``ScenarioManager`` against real
``scenario.json`` bundles on disk — no stubbing of the thing under test.
"""

import os
import shutil
import tempfile

import pytest

from apps.container_logistics.scenario.scenario_bundle import compile_bundle, write_bundle
from apps.container_logistics.scenario.scenario_manager import ScenarioManager

DOMAIN = "container_logistics"

PORT_SCHEDULE = {
    "currency": "credit",
    "resolution": "hour",
    "points": [{"hour": 22, "amount": 40.0}, {"hour": 8, "amount": -15.0}],
}
DEPOT_SCHEDULE = {
    "currency": "credit",
    "resolution": "hour",
    "points": [{"hour": 3, "amount": 5.0}],
}


@pytest.fixture
def scenarios_tmp():
    tmp = tempfile.mkdtemp(prefix="rebate_stamp_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _facility(idx, ftype, rebate=None):
    profile = {
        "name": f"{ftype.lower()}_{idx:03d}",
        "facility_type": ftype,
        "location": {"type": "Point", "coordinates": [103.8, 1.3]},
        "gate_count": 8 if ftype == "Port" else 1,
        "service_time": 600,
        "status": "Open",
        "operating_hours": "24/7",
        "operating_days": "7 days a week",
    }
    if rebate is not None:
        profile["rebate"] = rebate
    return {
        "email": f"facility_{idx:03d}@test.com",
        "password": "password",
        "persona": {"role": "facility", "domain": DOMAIN},
        "steps_per_action": 1,
        "gate_count": profile["gate_count"],
        "service_time": profile["service_time"],
        "profile": profile,
    }


def _write_bundle(scenarios_tmp, slug, facilities, *, source, reference_time,
                  step_interval):
    """Write a real self-contained ``scenario.json`` and return its directory."""
    scenario_dir = os.path.join(scenarios_tmp, DOMAIN, "scenarios", slug)
    os.makedirs(scenario_dir, exist_ok=True)
    from apps.container_logistics.scenario import scenario_config

    settings = {
        "REFERENCE_TIME": reference_time,
        "STEP_INTERVAL": step_interval,
        "SIMULATION_DAYS": scenario_config.SIMULATION_DAYS,
        "SIMULATION_LENGTH_IN_STEPS": scenario_config.simulation_length_in_steps(),
        # Read from scenario_config rather than pinned: `_loaded_behaviors_match_config`
        # regenerates the whole bundle on a mismatch, which would silently replace the
        # hand-written facilities under test with freshly generated rebate-less ones.
        "BEHAVIOR_REVISION": scenario_config.BEHAVIOR_REVISION,
        # `source: "frontend"` on the spec is what makes a bundle FROZEN. Almost every
        # real compiled bundle is, which is why the frozen path is tested explicitly.
        # The agent counts must match the collections below for the same reason.
        "GENERATION_SPEC": {
            "slug": slug,
            "name": slug,
            "source": source,
            "frozen": source == "frontend",
            "simulationDays": scenario_config.SIMULATION_DAYS,
            "agents": {
                "truck": {"count": 0},
                "order": {"count": 0},
                "facility": {"count": len(facilities)},
            },
        },
    }
    collections = {
        "truck": {},
        "order": {},
        "facility": facilities,
        "assignment": {},
        "analytics": {},
    }
    bundle = compile_bundle(
        collections, settings, {"slug": slug, "source": source},
        domain=DOMAIN, slug=slug, name=slug, source=source,
    )
    write_bundle(scenario_dir, bundle)
    return scenario_dir


def _load(scenarios_tmp, slug):
    # ``BaseScenarioManager.__init__`` already runs ``load_or_generate_behaviors()``,
    # which is the real load path a run takes.
    mgr = ScenarioManager(scenarios_tmp, slug, DOMAIN)
    assert len(mgr.facility_collection or {}) > 0, (
        "the bundle was regenerated instead of loaded — the hand-written facilities "
        "under test were replaced, so this fixture proves nothing"
    )
    return mgr


def _stamp(mgr):
    stamp = mgr.orsim_settings.get("REBATE")
    assert stamp is not None, "no REBATE stamp was written at all"
    return stamp


# --------------------------------------------------------------------------------


def test_run_stamp_records_effective_schedules_for_a_by_code_bundle(scenarios_tmp):
    """The stamp reports the resolved PER-FACILITY reality, not the authored by-code map.

    Six ports carry a schedule; 294 other facilities do not. An authored
    ``rebate_by_code: {"CT": {...}}`` says nothing about how many facilities that hit —
    the stamp must answer that from the compiled artefact.
    """
    facilities = {}
    for i in range(6):
        facilities[f"facility_{i:03d}"] = _facility(i, "Port", PORT_SCHEDULE)
    for i in range(6, 300):
        facilities[f"facility_{i:03d}"] = _facility(i, "Warehouse")
    _write_bundle(scenarios_tmp, "by_code", facilities,
                  source="generated", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    stamp = _stamp(_load(scenarios_tmp, "by_code"))

    assert stamp["enabled"] is True
    assert stamp["total_facilities"] == 300
    assert stamp["facilities_with_schedule"] == 6
    assert stamp["facilities_with_unparseable_schedule"] == 0
    assert stamp["currency"] == "credit"
    assert len(stamp["schedules"]) == 1
    (bucket,) = stamp["schedules"].values()
    assert bucket["facility_count"] == 6
    assert bucket["facility_types"] == ["Port"]
    assert bucket["example_facility"] == "port_000"
    # Densified to the full day, so the zero-gap rule is legible in the run record.
    assert len(bucket["points"]) == 24
    by_hour = {p["hour"]: p["amount"] for p in bucket["points"]}
    assert by_hour[22] == 40.0 and by_hour[8] == -15.0 and by_hour[0] == 0.0


def test_stamp_collapses_identical_schedules_by_digest(scenarios_tmp):
    """300 inline copies of one curve is how a stamp becomes unreadable."""
    facilities = {}
    for i in range(6):
        facilities[f"facility_{i:03d}"] = _facility(i, "Port", PORT_SCHEDULE)
    for i in range(6, 10):
        facilities[f"facility_{i:03d}"] = _facility(i, "Depot", DEPOT_SCHEDULE)
    _write_bundle(scenarios_tmp, "two_curves", facilities,
                  source="generated", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    stamp = _stamp(_load(scenarios_tmp, "two_curves"))

    assert stamp["facilities_with_schedule"] == 10
    assert len(stamp["schedules"]) == 2, "identical schedules must collapse to one digest"
    counts = sorted(b["facility_count"] for b in stamp["schedules"].values())
    assert counts == [4, 6]
    types = sorted(tuple(b["facility_types"]) for b in stamp["schedules"].values())
    assert types == [("Depot",), ("Port",)]


def test_stamp_records_reference_time_and_step_interval(scenarios_tmp):
    """The G5 mitigation, in the run record.

    The tree carries three conflicting epochs (04:00, 08:00, 00:00). A reader asking
    "which hour did 14:00 mean in this run?" must be able to answer from the record.
    """
    facilities = {"facility_000": _facility(0, "Port", PORT_SCHEDULE)}
    _write_bundle(scenarios_tmp, "epoch", facilities,
                  source="frontend", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    stamp = _stamp(_load(scenarios_tmp, "epoch"))
    assert stamp["reference_time"] == "2020-01-01 08:00:00"
    assert stamp["step_interval_seconds"] == 240


def test_disabled_is_stamped_explicitly_when_nothing_resolves(scenarios_tmp):
    """"No rebates" and "rebates silently lost" must stay distinguishable."""
    facilities = {f"facility_{i:03d}": _facility(i, "Warehouse") for i in range(5)}
    _write_bundle(scenarios_tmp, "none", facilities,
                  source="generated", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    stamp = _stamp(_load(scenarios_tmp, "none"))
    assert stamp["enabled"] is False   # present and explicitly false, not absent
    assert stamp["facilities_with_schedule"] == 0
    assert stamp["total_facilities"] == 5
    assert stamp["schedules"] == {}
    assert stamp["currency"] is None
    # No caveat block when nothing is priced — a caveat about dropoff clocks would be
    # noise on a run that prices nothing.
    assert "caveats" not in stamp


def test_rebate_stamp_survives_a_frozen_scenario(scenarios_tmp):
    """The single most likely way this stamp silently does nothing.

    ``_sync_runtime_tuning_from_config`` returns early for a frontend-frozen bundle, and
    a compiled bundle's ``source: "frontend"`` makes almost every real scenario frozen.
    ``USE_OSRM_AT_ASSIGNMENT`` had to be moved to the near side of that guard for exactly
    this reason; the rebate stamp must sit on the same side.
    """
    facilities = {f"facility_{i:03d}": _facility(i, "Port", PORT_SCHEDULE) for i in range(3)}
    mgr_dir = _write_bundle(scenarios_tmp, "frozen", facilities,
                            source="frontend", reference_time="2020-01-01 08:00:00",
                            step_interval=240)
    assert os.path.isfile(os.path.join(mgr_dir, "scenario.json"))

    mgr = _load(scenarios_tmp, "frozen")
    assert mgr._is_frontend_frozen_scenario() is True, "fixture is not actually frozen"

    stamp = _stamp(mgr)
    assert stamp["enabled"] is True
    assert stamp["facilities_with_schedule"] == 3


def test_stamp_reaches_run_config_meta_on_both_channels(scenarios_tmp):
    """Provenance is not an afterthought: it must be in the run record, both ways."""
    facilities = {"facility_000": _facility(0, "Port", PORT_SCHEDULE)}
    _write_bundle(scenarios_tmp, "meta", facilities,
                  source="frontend", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    meta = _load(scenarios_tmp, "meta").get_run_config_meta()
    assert meta["rebate"]["enabled"] is True
    assert meta["simulation_settings"]["REBATE"]["enabled"] is True
    # EQUALITY, not identity (R2-13 / review F15). Whether the two channels share one
    # object or hold two equal copies is an implementation detail; the contract is that
    # a reader finds the same stamp on either. Asserting `is` pinned the detail and would
    # have failed a harmless defensive deepcopy.
    assert meta["rebate"] == meta["simulation_settings"]["REBATE"]


def test_stamp_records_the_zero_time_laden_leg_caveat(scenarios_tmp):
    """Plan §16.1, made observable rather than left in a doc nobody reads.

    Re-verified 2026-08-21 against MongoDB: on 785/785 completed trips of
    run_20260817_164156, ``loaded_started_at == dropoff_queue_arrival_time``. A dropoff
    rebate is therefore priced on a clock with no driving in it.
    """
    facilities = {"facility_000": _facility(0, "Port", PORT_SCHEDULE)}
    _write_bundle(scenarios_tmp, "caveat", facilities,
                  source="generated", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    stamp = _stamp(_load(scenarios_tmp, "caveat"))
    assert stamp["caveats"], "a priced run must carry the dropoff-clock caveat"
    assert any("dropoff" in c for c in stamp["caveats"])


def test_stamp_counts_an_unparseable_block_rather_than_dying(scenarios_tmp):
    """A hand-patched bundle (they happen) must not take the run down."""
    facilities = {
        "facility_000": _facility(0, "Port", PORT_SCHEDULE),
        "facility_001": _facility(1, "Port", {"points": [{"hour": 99, "amount": 1.0}]}),
    }
    _write_bundle(scenarios_tmp, "broken", facilities,
                  source="generated", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    stamp = _stamp(_load(scenarios_tmp, "broken"))
    assert stamp["enabled"] is True
    assert stamp["facilities_with_schedule"] == 1
    assert stamp["facilities_with_unparseable_schedule"] == 1


def test_stamp_is_derived_from_the_bundle_not_the_authored_spec(scenarios_tmp):
    """Effective, not authored.

    An operator who hand-patches a compiled bundle must get a stamp describing what
    actually ran. Here the recipe/spec claims nothing about rebates at all, yet three
    facilities carry a patched-in block — the stamp must report the block.
    """
    facilities = {f"facility_{i:03d}": _facility(i, "Port", PORT_SCHEDULE) for i in range(3)}
    facilities["facility_003"] = _facility(3, "Warehouse")
    _write_bundle(scenarios_tmp, "patched", facilities,
                  source="frontend", reference_time="2020-01-01 08:00:00",
                  step_interval=240)

    mgr = _load(scenarios_tmp, "patched")
    spec = mgr._generation_spec() or {}
    assert "rebate" not in (spec.get("overrides") or {}).get("facility", {})
    stamp = _stamp(mgr)
    assert stamp["facilities_with_schedule"] == 3
    assert stamp["total_facilities"] == 4
