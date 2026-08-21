"""Phase 5 of ``docs/facility_rules_plan.md`` — the migration (T24–T28).

**The single most likely way this feature ships broken** is §11.2's disarm, and it
fails silently and green: ``_validate_facility_rebate`` used to early-return when
``overrides.facility.rebate`` and ``.rebate_by_code`` were both absent, and the
migration makes both absent. Four checks sat downstream of that return — the
midnight-epoch refusal, the mixed-currency refusal, the flat-schedule warning and
the nothing-resolved warning — and none of the tests covering them would have
noticed, because every one of them authored the key being removed.

T26/T27/T28 are the proof that the guard now judges the POST-RESOLUTION set.
Mutation **M10** reverts the guard to keying off ``overrides.facility.rebate*`` and
must make T26 and T27 fail.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container-logistics-sim-test"
MIDNIGHT = "2020-01-01 00:00:00"
EIGHT_AM = "2020-01-01 08:00:00"
_FACILITY_COUNT = 30

_CREDIT = {
    "currency": "credit",
    "resolution": "hour",
    "points": [{"hour": h, "amount": 40.0 if h < 6 else -5.0} for h in range(24)],
}
_EURO = {
    "currency": "eur",
    "resolution": "hour",
    "points": [{"hour": h, "amount": 3.0 if h < 6 else -1.0} for h in range(24)],
}
_FLAT = {
    "currency": "credit",
    "resolution": "hour",
    "points": [{"hour": h, "amount": 5.0} for h in range(24)],
}


def _spec(rules=None, reference_time=MIDNIGHT, **kw):
    spec = {
        "name": "Migration Test",
        "slug": "migration_test",
        "simulationDays": 1,
        "orderCountUnit": "total",
        "referenceTime": reference_time,
        "agents": {
            "truck": {"count": 6},
            "order": {"count": 12},
            "facility": {"count": _FACILITY_COUNT},
        },
        "earlyOrderCount": 0,
    }
    if rules is not None:
        spec["facilityRules"] = deepcopy(rules)
        spec["facilityRulesWorld"] = deepcopy(_world())
    spec.update(kw)
    return spec


def _compile(spec_raw, reference_time=None):
    ref = reference_time or spec_raw.get("referenceTime") or MIDNIGHT
    return Preprocessor.compile(deepcopy(spec_raw), domain=DOMAIN, reference_time=ref)


_CACHE = {}


def _world():
    if "w" not in _CACHE:
        sites = _compile(_spec()).spec.facility_settings["profile"]["facilities"]
        _CACHE["w"] = fr.world_snapshot(sites, seed=None)
    return _CACHE["w"]


# --------------------------------------------------------------------------- #
# T24 — the removed key stays removed, and the error IS the migration guide
# --------------------------------------------------------------------------- #

def test_rebate_by_code_is_rejected_with_a_translation():
    spec = _spec(overrides={"facility": {"rebate_by_code": {"CT": deepcopy(_CREDIT)}}})
    with pytest.raises(SpecValidationError) as exc:
        _compile(spec)
    msg = str(exc.value)
    assert "no longer supported" in msg
    # The translation must be IN the error — an author hitting this has no other
    # place to learn the new shape, and a bare rejection is how a migration becomes
    # a support ticket.
    assert "facilityRules" in msg
    assert '"match": {"code": "CT"}' in msg
    assert '"set": {"rebate": null}' in msg
    assert "presence" in msg
    assert "rules-baseline migration_test" in msg


def test_rebate_by_code_cannot_ride_in_through_legacy_role_settings():
    # `_merged_overrides` folds `roleSettings.<role>.profile` into `overrides`, so a
    # check that only looked at `overrides` would leave a back door open.
    spec = _spec(roleSettings={"facility": {"profile": {
        "rebate_by_code": {"CT": deepcopy(_CREDIT)}}}})
    with pytest.raises(SpecValidationError, match="no longer supported"):
        _compile(spec)


def test_the_scenario_wide_rebate_override_survives_as_rank_zero():
    # Only the parallel PER-CODE mechanism dies. `overrides.facility.rebate` is rank
    # 0 for an allow-listed key and is coherent with the two-layer model.
    compiled = _compile(_spec(overrides={"facility": {"rebate": deepcopy(_CREDIT)}}))
    facilities = ScenarioGenerator(compiled.spec).generate().facility
    assert all("rebate" in b["profile"] for b in facilities.values())


# --------------------------------------------------------------------------- #
# T26 / T27 / T28 — the guards must fire for a RULES-authored schedule (§11.2)
# --------------------------------------------------------------------------- #

def test_midnight_epoch_guard_fires_for_a_rules_authored_schedule():
    # FIXTURE PROPERTY: NOTHING is authored under `overrides.facility`. The only
    # schedule in this spec arrives through `facilityRules`. Under the pre-migration
    # guard this compiled silently, because both authored keys were absent.
    spec = _spec(
        [{"match": {"code": "CT"}, "set": {"rebate": deepcopy(_CREDIT)}}],
        reference_time=EIGHT_AM,
    )
    assert "overrides" not in spec, "fixture: the guard must not be reachable via overrides"
    with pytest.raises(SpecValidationError) as exc:
        _compile(spec, reference_time=EIGHT_AM)
    msg = str(exc.value)
    assert "midnight" in msg
    assert "hour 8" in msg


def test_the_midnight_guard_still_fires_for_a_blanket_authored_schedule():
    # The other arm: re-pointing the guard must not have narrowed it.
    spec = _spec(overrides={"facility": {"rebate": deepcopy(_CREDIT)}},
                 reference_time=EIGHT_AM)
    with pytest.raises(SpecValidationError, match="midnight"):
        _compile(spec, reference_time=EIGHT_AM)


def test_a_non_midnight_scenario_with_no_schedule_at_all_still_compiles():
    # And it must not have WIDENED either: the midnight requirement is rebate-scoped
    # because a non-rebate scenario has no second axis to collide with.
    _compile(_spec(reference_time=EIGHT_AM), reference_time=EIGHT_AM)
    _compile(_spec([{"match": {"code": "CT"}, "set": {"gate_count": 6}}],
                   reference_time=EIGHT_AM), reference_time=EIGHT_AM)


def test_mixed_currency_refused_for_rules_authored_schedules():
    # FIXTURE PROPERTY: again nothing under `overrides` — two RULES disagree.
    spec = _spec([
        {"match": {"code": "CT"}, "set": {"rebate": deepcopy(_CREDIT)}},
        {"match": {"code": "CU"}, "set": {"rebate": deepcopy(_EURO)}},
    ])
    assert "overrides" not in spec
    with pytest.raises(SpecValidationError) as exc:
        _compile(spec)
    msg = str(exc.value)
    assert "2 different currencies" in msg
    assert "'credit'" in msg and "'eur'" in msg


def test_mixed_currency_is_judged_on_the_effective_set_not_the_authored_blocks():
    # An authored default in one currency, fully overridden by rules to another,
    # is NOT mixed — nothing in the run prices in the default's currency. This is
    # the property that only a post-resolution check can get right.
    spec = _spec(
        [
            {"match": {"code": "CT"}, "set": {"rebate": deepcopy(_CREDIT)}},
            {"match": {"code": "CU"}, "set": {"rebate": deepcopy(_CREDIT)}},
            {"match": {"code": "MT"}, "set": {"rebate": deepcopy(_CREDIT)}},
        ],
        overrides={"facility": {"rebate": deepcopy(_EURO)}},
    )
    compiled = _compile(spec)  # must NOT raise
    facilities = ScenarioGenerator(compiled.spec).generate().facility
    currencies = {b["profile"]["rebate"]["currency"] for b in facilities.values()
                  if "rebate" in b["profile"]}
    assert currencies == {"credit"}, "the euro default must have been fully overridden"


def test_flat_schedule_warning_still_fires_for_a_rules_authored_schedule(caplog):
    spec = _spec([{"match": {"code": "CT"}, "set": {"rebate": deepcopy(_FLAT)}}])
    with caplog.at_level(logging.WARNING):
        _compile(spec)
    assert any("FLAT" in r.getMessage() for r in caplog.records), (
        "a zero-variance schedule is a participation payment, not a time-of-day "
        "incentive, and an author who believed otherwise must be told"
    )


def test_nothing_resolved_warning_fires_when_every_rule_nulls_the_schedule(caplog):
    spec = _spec(
        [
            {"match": {"code": "CT"}, "set": {"rebate": None}},
            {"match": {"code": "CU"}, "set": {"rebate": None}},
            {"match": {"code": "MT"}, "set": {"rebate": None}},
        ],
        overrides={"facility": {"rebate": deepcopy(_CREDIT)}},
    )
    with caplog.at_level(logging.WARNING):
        _compile(spec)
    assert any("NO facility resolves to a schedule" in r.getMessage()
               for r in caplog.records)


# --------------------------------------------------------------------------- #
# T25 — the flagship scenario's migration is a pure re-expression
# --------------------------------------------------------------------------- #

_FLAGSHIP_DIR = "scenarios/rebate_ports_500_trucks"


def _flagship():
    import os

    if not os.path.isfile(f"{_FLAGSHIP_DIR}/scenario.json"):
        pytest.skip("flagship scenario bundle not present")
    with open(f"{_FLAGSHIP_DIR}/spec.json", encoding="utf-8") as fp:
        spec = json.load(fp)
    with open(f"{_FLAGSHIP_DIR}/scenario.json", encoding="utf-8") as fp:
        bundle = json.load(fp)
    return spec, bundle


def test_migrated_rebate_scenario_no_longer_authors_the_removed_key():
    spec, _bundle = _flagship()
    assert "rebate_by_code" not in spec["overrides"]["facility"]
    assert "facilityRules" in spec and "facilityRulesWorld" in spec
    matched = {tuple(r["match"].items())[0] for r in spec["facilityRules"]}
    assert matched == {("code", "CT"), ("code", "CU"), ("code", "MT")}
    # The explicit null must have survived the translation as a null, not vanished:
    # presence in `set` is what counts, exactly as presence in `rebate_by_code` did.
    mt = next(r for r in spec["facilityRules"] if r["match"] == {"code": "MT"})
    assert "rebate" in mt["set"] and mt["set"]["rebate"] is None


def test_migrated_rebate_scenario_compiles_to_the_same_facility_collection():
    """The migration proof is EQUIVALENCE, available because this case is supposed
    to be a pure re-expression.

    ``sort_keys=True``, **not** byte-identity: ``rebate`` moved from a
    builder-appended key to a site-derived one, so JSON key *order* legitimately
    changes while content must not. Asserting byte-identity here would fail
    spuriously and the predictable repair is to weaken the assertion into vacuity.
    """
    spec, bundle = _flagship()
    compiled = Preprocessor.compile(
        deepcopy(spec), domain=bundle["domain"], scenario_dir=_FLAGSHIP_DIR,
        reference_time=spec.get("referenceTime") or MIDNIGHT,
    )
    regenerated = ScenarioGenerator(compiled.spec).generate().facility
    assert json.dumps(regenerated, sort_keys=True) == json.dumps(
        bundle["agents"]["facility"], sort_keys=True
    ), "the migrated spec no longer compiles to the bundle on disk — recompile it"


def test_migrated_rebate_scenario_census_is_unchanged():
    _spec_json, bundle = _flagship()
    facilities = bundle["agents"]["facility"]
    assert len(facilities) == 300
    types: dict = {}
    with_rebate = 0
    for behavior in facilities.values():
        prof = behavior["profile"]
        types[prof["facility_type"]] = types.get(prof["facility_type"], 0) + 1
        if "rebate" in prof:
            with_rebate += 1
            assert len(prof["rebate"]["points"]) == 24, "schedules stay dense"
        # The authoring surface must never reach a compiled profile.
        assert "rebate_by_code" not in prof
        assert "facilityRules" not in prof
    assert types == {"Port": 6, "Warehouse": 234, "Depot": 60}
    assert with_rebate == 240, "MT's explicit null still suppresses 60 schedules"


def test_the_flagship_bundle_does_not_carry_the_world_fingerprint_outside_the_recipe():
    """§7.2: the baseline lives in ``spec.json`` and only echoes through
    ``$.recipe``. Anywhere else in a REGENERATED artefact it would recompute itself,
    always match, and guard nothing while looking exactly like a guard."""
    _spec_json, bundle = _flagship()
    assert "facilityRulesWorld" not in bundle
    assert bundle["recipe"]["facilityRulesWorld"]["site_digest"].startswith("blake2b16:")
    assert bundle["recipe"]["facilityRules"], "the recipe echo is the recompile source"
