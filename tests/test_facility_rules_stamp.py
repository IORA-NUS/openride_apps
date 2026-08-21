"""Phase 6 of ``docs/facility_rules_plan.md`` — run provenance (T29–T32).

Same discipline as ``test_scenario_rebate_stamp.py``: drive the real
``ScenarioManager`` against real ``scenario.json`` bundles on disk. Nothing about
the thing under test is stubbed.

The stamp matters more here than it did for pricing. ``gate_count`` is **world
physics** — runs before and after a gate-count change are not comparable — so an
analyst comparing two runs must be able to *see* that the gate counts differ rather
than infer it from a KPI that moved.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from copy import deepcopy

import pytest

from apps.container_logistics.scenario.scenario_bundle import compile_bundle, write_bundle
from apps.container_logistics.scenario.scenario_manager import ScenarioManager

DOMAIN = "container_logistics"

_SCHEDULE = {
    "currency": "credit",
    "resolution": "hour",
    "points": [{"hour": h, "amount": 40.0 if h < 6 else -5.0} for h in range(24)],
}

_RULES = [
    {"match": {"code": "CT"}, "set": {"gate_count": 6}},
    {"match": {"code": "CT"}, "set": {"rebate": _SCHEDULE}},
    {"match": {"name": "port_003"}, "set": {"gate_count": 12}},
    {"match": {"code": "MT"}, "set": {"rebate": None}},
]


@pytest.fixture
def scenarios_tmp():
    tmp = tempfile.mkdtemp(prefix="facility_rules_stamp_")
    prev = os.environ.get("ORSIM_SCENARIOS_DIR")
    os.environ["ORSIM_SCENARIOS_DIR"] = os.path.join(tmp, DOMAIN, "scenarios")
    yield tmp
    if prev is None:
        os.environ.pop("ORSIM_SCENARIOS_DIR", None)
    else:
        os.environ["ORSIM_SCENARIOS_DIR"] = prev
    shutil.rmtree(tmp, ignore_errors=True)


def _facility(idx, ftype, name, gate_count, rebate=None):
    profile = {
        "name": name,
        "facility_type": ftype,
        "location": {"type": "Point", "coordinates": [103.8, 1.3]},
        "gate_count": gate_count,
        "service_time": 1800,
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
        "gate_count": gate_count,
        "service_time": 1800,
        "profile": profile,
    }


def _ruled_facilities():
    """The compiled consequence of ``_RULES``: 6 ports (one of them port_003 at 12
    gates), 4 warehouses, 2 depots."""
    out = {}
    idx = 0
    for i in range(6):
        name = f"port_{i:03d}"
        out[f"facility_{idx:03d}"] = _facility(
            idx, "Port", name, 12 if name == "port_003" else 6, rebate=_SCHEDULE
        )
        idx += 1
    for i in range(4):
        out[f"facility_{idx:03d}"] = _facility(idx, "Warehouse", f"customer_{i:03d}", 1)
        idx += 1
    for i in range(2):
        out[f"facility_{idx:03d}"] = _facility(idx, "Depot", f"depot_{i:03d}", 1)
        idx += 1
    return out


def _write_bundle(scenarios_tmp, slug, facilities, *, source, rules):
    scenario_dir = os.path.join(scenarios_tmp, DOMAIN, "scenarios", slug)
    os.makedirs(scenario_dir, exist_ok=True)
    from apps.container_logistics.scenario import scenario_config

    settings = {
        "REFERENCE_TIME": "2020-01-01 00:00:00",
        "STEP_INTERVAL": scenario_config.STEP_INTERVAL_SECONDS,
        "SIMULATION_DAYS": scenario_config.SIMULATION_DAYS,
        "SIMULATION_LENGTH_IN_STEPS": scenario_config.simulation_length_in_steps(),
        "BEHAVIOR_REVISION": scenario_config.BEHAVIOR_REVISION,
        "GENERATION_SPEC": {
            "slug": slug,
            "name": slug,
            "source": source,
            # `source: "frontend"` is what makes a bundle FROZEN, which takes the
            # EARLY-RETURN branch of _sync_runtime_tuning_from_config. Both branches
            # are exercised below (T31) because the flagship bundle's frozen
            # predicate is False even though its recipe says frozen: true — relying
            # on either branch alone is a coin flip.
            "frozen": source == "frontend",
            "simulationDays": scenario_config.SIMULATION_DAYS,
            "agents": {"truck": {"count": 0}, "order": {"count": 0},
                       "facility": {"count": len(facilities)}},
            "facilityRules": rules,
        },
    }
    collections = {"truck": {}, "order": {}, "facility": facilities,
                   "assignment": {}, "analytics": {}}
    bundle = compile_bundle(
        collections, settings, {"slug": slug, "source": source},
        domain=DOMAIN, slug=slug, name=slug, source=source,
    )
    write_bundle(scenario_dir, bundle)
    return scenario_dir


def _load(scenarios_tmp, slug):
    mgr = ScenarioManager(scenarios_tmp, slug, DOMAIN)
    assert len(mgr.facility_collection or {}) > 0, (
        "the bundle was regenerated instead of loaded — the hand-written facilities "
        "under test were replaced, so this fixture proves nothing"
    )
    return mgr


def _stamp(mgr):
    stamp = mgr.orsim_settings.get("FACILITY_RULES")
    assert stamp is not None, "no FACILITY_RULES stamp was written at all"
    return stamp


# --------------------------------------------------------------------------- #
# T29 — the stamp reports the EFFECTIVE resolved values, not the authored rules
# --------------------------------------------------------------------------- #

def test_stamp_reports_the_effective_resolved_values_not_the_authored_rules(scenarios_tmp):
    _write_bundle(scenarios_tmp, "ruled", _ruled_facilities(),
                  source="frontend", rules=_RULES)
    stamp = _stamp(_load(scenarios_tmp, "ruled"))

    assert stamp["enabled"] is True
    assert stamp["rule_count"] == 4
    assert stamp["facility_count"] == 12

    gates = stamp["resolved"]["gate_count"]
    assert gates["12"]["facility_count"] == 1
    assert gates["12"]["example"] == "port_003"
    assert gates["12"]["codes"] == ["CT"]
    assert gates["6"]["facility_count"] == 5, (
        "the authored rule says CT; the EFFECTIVE answer is 5, because a more "
        "specific rule took port_003 — that difference is the whole point"
    )
    assert gates["1"]["facility_count"] == 6
    assert sorted(gates["1"]["codes"]) == ["CU", "MT"]

    # Attribution: each value group names the rule that produced it.
    assert "match.name='port_003'" in gates["12"]["source"]
    assert "match.code='CT'" in gates["6"]["source"]
    assert gates["1"]["source"] is None, (
        "no rule produced the default — claiming one would be a fiction"
    )

    # The per-rule audit `resolved` cannot give.
    applied = {r["index"]: r for r in stamp["rules_applied"]}
    assert applied[0]["matched"] == 6 and applied[0]["set_keys"] == ["gate_count"]
    assert applied[1]["matched"] == 6 and applied[1]["set_keys"] == ["rebate"]
    assert applied[2]["matched"] == 1
    assert applied[3]["matched"] == 2
    assert applied[0]["matched"] != gates["6"]["facility_count"], (
        "rules_applied and resolved must be able to DISAGREE — a rule matching 6 "
        "while only 5 facilities carry its value IS rule 2 winning on port_003"
    )

    rebates = stamp["resolved"]["rebate"]
    assert rebates["none"]["facility_count"] == 6
    priced = [k for k in rebates if k != "none"]
    assert len(priced) == 1 and rebates[priced[0]]["facility_count"] == 6
    assert priced[0].startswith("blake2b:"), (
        "300 inline copies of one schedule is how a stamp becomes unreadable"
    )


def test_the_stamp_describes_what_RAN_not_what_was_authored(scenarios_tmp):
    """§10.1: people hand-patch compiled bundles. A patched value must show up as
    what it is — a value no authored rule explains — rather than being quietly
    reported as the rule's."""
    facilities = _ruled_facilities()
    facilities["facility_000"]["profile"]["gate_count"] = 99
    facilities["facility_000"]["gate_count"] = 99
    _write_bundle(scenarios_tmp, "patched", facilities, source="frontend", rules=_RULES)
    stamp = _stamp(_load(scenarios_tmp, "patched"))

    gates = stamp["resolved"]["gate_count"]
    assert "99" in gates, "the stamp reported the authored rule instead of the bundle"
    assert gates["99"]["facility_count"] == 1
    assert gates["99"]["source"] is None
    assert gates["6"]["facility_count"] == 4


# --------------------------------------------------------------------------- #
# T30 — the Sigma self-check
# --------------------------------------------------------------------------- #

def test_stamp_partitions_every_facility_for_every_key(scenarios_tmp):
    _write_bundle(scenarios_tmp, "partition", _ruled_facilities(),
                  source="frontend", rules=_RULES)
    stamp = _stamp(_load(scenarios_tmp, "partition"))
    total = stamp["facility_count"]
    assert total == 12
    for key in ("gate_count", "service_time", "rebate"):
        groups = stamp["resolved"][key]
        assert sum(g["facility_count"] for g in groups.values()) == total, key
    assert stamp["partitions_every_facility"] is True


# --------------------------------------------------------------------------- #
# T31 — BOTH branches of _sync_runtime_tuning_from_config
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("source, frozen", [("frontend", True), ("spec", False)])
def test_stamp_is_written_from_both_frozen_and_non_frozen_paths(
    scenarios_tmp, source, frozen
):
    slug = f"branch_{source}"
    _write_bundle(scenarios_tmp, slug, _ruled_facilities(), source=source, rules=_RULES)
    mgr = _load(scenarios_tmp, slug)
    assert mgr._is_frontend_frozen_scenario() is frozen, (
        "fixture: this arm did not take the branch it claims to test"
    )
    stamp = _stamp(mgr)
    assert stamp["enabled"] is True
    assert stamp["resolved"]["gate_count"]["12"]["facility_count"] == 1


# --------------------------------------------------------------------------- #
# "no rules" and "rules silently lost" must stay distinguishable (§10.4)
# --------------------------------------------------------------------------- #

def test_a_rule_less_scenario_stamps_enabled_false_explicitly(scenarios_tmp):
    plain = {
        f"facility_{i:03d}": _facility(i, "Warehouse", f"customer_{i:03d}", 1)
        for i in range(4)
    }
    _write_bundle(scenarios_tmp, "plain", plain, source="frontend", rules=None)
    stamp = _stamp(_load(scenarios_tmp, "plain"))
    assert stamp["enabled"] is False
    assert stamp["rule_count"] == 0
    # The resolved distribution is still recorded — "no rules" does not mean
    # "nothing worth describing"; the gate counts are still what this run runs on.
    assert stamp["resolved"]["gate_count"]["1"]["facility_count"] == 4
    assert stamp["facility_count"] == 4


# --------------------------------------------------------------------------- #
# T32 — the REBATE stamp is unchanged by the migration
# --------------------------------------------------------------------------- #

def test_rebate_stamp_is_unchanged_by_the_migration(scenarios_tmp):
    """``_stamp_rebate_provenance`` scans ``profile.rebate``, which is still
    populated — just by a different resolver. It needed no change and must not have
    acquired one."""
    _write_bundle(scenarios_tmp, "both", _ruled_facilities(),
                  source="frontend", rules=_RULES)
    mgr = _load(scenarios_tmp, "both")
    rebate = mgr.orsim_settings.get("REBATE")
    assert rebate is not None
    assert rebate["enabled"] is True
    assert rebate["total_facilities"] == 12
    assert rebate["facilities_with_schedule"] == 6
    assert rebate["currency"] == "credit"
    assert rebate["facilities_with_unparseable_schedule"] == 0


def test_the_stamp_reaches_run_config_meta(scenarios_tmp):
    """The mirror (§10). Without it a run can only explain its facility setup to a
    reader who knows which channel carried it."""
    _write_bundle(scenarios_tmp, "meta", _ruled_facilities(),
                  source="frontend", rules=_RULES)
    mgr = _load(scenarios_tmp, "meta")
    meta = mgr.get_run_config_meta()
    assert meta["facility_rules"] is not None
    assert meta["facility_rules"]["rule_count"] == 4
    # It rides simulation_settings too, which is what ships to every Celery agent.
    assert meta["simulation_settings"]["FACILITY_RULES"]["rule_count"] == 4


def test_the_stamp_never_breaks_a_run(scenarios_tmp):
    """A provenance stamp must not be able to fail a run — an unrecoverable code, a
    malformed rule and a missing profile key must all be survivable."""
    facilities = _ruled_facilities()
    facilities["facility_000"]["profile"]["facility_type"] = "Spaceport"
    facilities["facility_000"]["profile"]["name"] = "unparseable"
    facilities["facility_001"]["profile"].pop("gate_count")
    bad_rules = list(_RULES) + ["not a rule", {"match": "nope", "set": {}}]
    _write_bundle(scenarios_tmp, "hostile", facilities, source="frontend",
                  rules=bad_rules)
    stamp = _stamp(_load(scenarios_tmp, "hostile"))
    assert stamp["facility_count"] == 12
    assert stamp["partitions_every_facility"] is True
    # The unrecoverable facility contributes to a group but names no code.
    assert any("none" in groups for groups in [stamp["resolved"]["gate_count"]])


# --------------------------------------------------------------------------- #
# R2-4 (F4) — physics_digest: the question site_digest cannot answer
# --------------------------------------------------------------------------- #

def _digest(scenarios_tmp, slug, facilities, rules=_RULES):
    _write_bundle(scenarios_tmp, slug, facilities, source="frontend", rules=rules)
    return _stamp(_load(scenarios_tmp, slug))["physics_digest"]


def test_physics_digest_differs_for_two_gate_counts_on_one_world(scenarios_tmp):
    """The case `site_digest` provably cannot see.

    MEASURED on the two shipped arms: `rebate_ports_500_trucks` (gate_count 1) and
    `port_gates_500_trucks` (gate_count 4) carry the IDENTICAL site_digest
    blake2b16:86bfd8764d6d9d46. Same facility identities, different physics — so a
    comparability gate reading the world fingerprint ships still failing open.
    """
    four = _ruled_facilities()
    one = deepcopy(four)
    for behavior in one.values():
        behavior["gate_count"] = 1
        behavior["profile"]["gate_count"] = 1

    d_four = _digest(scenarios_tmp, "gates_four", four)
    d_one = _digest(scenarios_tmp, "gates_one", one)
    assert d_four.startswith("blake2b16:")
    assert d_four != d_one, (
        "the digest is blind to a gate-count change, which is the entire finding"
    )

    # And the facility IDENTITIES are unchanged between the two, which is what makes
    # this the case a site fingerprint cannot catch.
    assert sorted(b["profile"]["name"] for b in four.values()) == sorted(
        b["profile"]["name"] for b in one.values()
    )


def test_physics_digest_is_stable_under_a_facility_rename(scenarios_tmp):
    """`example` and `source` are excluded from the digest for exactly this reason:
    a rename changes which facility is quoted, not what the model runs on."""
    base = _ruled_facilities()
    renamed = deepcopy(base)
    for behavior in renamed.values():
        behavior["profile"]["name"] = behavior["profile"]["name"].replace("port_", "quay_")
    assert _digest(scenarios_tmp, "names_a", base) == _digest(
        scenarios_tmp, "names_b", renamed
    )


def test_physics_digest_changes_for_a_blanket_only_edit(scenarios_tmp):
    """It covers BOTH layers. `resolved` already carries rank-0 groups, so a
    scenario with NO rules at all that edits the blanket gate_count is caught —
    which matters, because that is how 13 of the 15 shipped scenarios are authored."""
    plain = {
        f"facility_{i:03d}": _facility(i, "Warehouse", f"customer_{i:03d}", 1)
        for i in range(4)
    }
    bumped = deepcopy(plain)
    for behavior in bumped.values():
        behavior["gate_count"] = 3
        behavior["profile"]["gate_count"] = 3
    a = _digest(scenarios_tmp, "blanket_a", plain, rules=None)
    b = _digest(scenarios_tmp, "blanket_b", bumped, rules=None)
    assert a != b


def test_physics_digest_changes_when_the_POPULATION_at_a_value_moves(scenarios_tmp):
    """Sensitive to the distribution, not just the value set: moving one facility
    from 6 gates to 12 leaves both values present and must still register."""
    base = _ruled_facilities()
    moved = deepcopy(base)
    target = next(k for k, v in moved.items() if v["profile"]["gate_count"] == 6)
    moved[target]["gate_count"] = 12
    moved[target]["profile"]["gate_count"] = 12
    assert _digest(scenarios_tmp, "pop_a", base) != _digest(scenarios_tmp, "pop_b", moved)


def test_physics_digest_is_reproducible_for_an_unchanged_bundle(scenarios_tmp):
    base = _ruled_facilities()
    assert _digest(scenarios_tmp, "repro_a", base) == _digest(
        scenarios_tmp, "repro_b", deepcopy(base)
    )
