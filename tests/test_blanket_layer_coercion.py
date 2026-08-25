"""R2-3 (F2) — the rank-0 door gets the rank-10/30 validator.

`SETTABLE_KEYS` validated what a RULE may set and nothing validated what the blanket
layer may set, so the two authoring paths to the same key disagreed about what a legal
value is. `overrides.facility.gate_count: 0` reached `FacilityQueueController`, whose
assert fires at agent boot — the exact crash the rules validator's own comment says it
exists to prevent, reachable through the door it did not cover.

The invariant worth testing is not eight separate rejections; it is that **both doors
answer identically**.
"""

from __future__ import annotations

import glob
import json
import os
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container-logistics-sim-test"
MIDNIGHT = "2020-01-01 00:00:00"


def _base(**kw):
    spec = {
        "name": "Blanket Probe", "slug": "blanket_probe", "simulationDays": 1,
        "orderCountUnit": "total", "referenceTime": MIDNIGHT,
        "agents": {"truck": {"count": 4}, "order": {"count": 8},
                   "facility": {"count": 12}},
        "earlyOrderCount": 0,
    }
    spec.update(kw)
    return spec


def _compile(raw):
    return Preprocessor.compile(deepcopy(raw), domain=DOMAIN, reference_time=MIDNIGHT)


def _world():
    sites = _compile(_base()).spec.facility_settings["profile"]["facilities"]
    return fr.world_snapshot(sites, seed=None)


def _via_blanket(key, value):
    return _base(overrides={"facility": {key: value}})


def _via_rule(key, value):
    return _base(
        facilityRules=[{"match": {"code": "CT"}, "set": {key: value}}],
        facilityRulesWorld=_world(),
    )


#: One value set, exercised through BOTH authoring paths.
_ILLEGAL = [0, -5, "6", 2.5, True, [], {}]
_LEGAL = [1, 4, 12]


@pytest.mark.parametrize("key", ["gate_count", "service_time"])
@pytest.mark.parametrize("value", _ILLEGAL)
def test_blanket_and_rule_layers_reject_identically(key, value):
    """The invariant, not eight assertions: one validator, two doors, same answer."""
    with pytest.raises(SpecValidationError):
        _compile(_via_rule(key, value))
    with pytest.raises(SpecValidationError) as exc:
        _compile(_via_blanket(key, value))
    # And the blanket rejection must name the blanket key, or an author cannot tell
    # which of the two layers they got it wrong in.
    assert f"overrides.facility.{key}" in str(exc.value)


@pytest.mark.parametrize("key", ["gate_count", "service_time"])
@pytest.mark.parametrize("value", _LEGAL)
def test_blanket_and_rule_layers_accept_identically(key, value):
    _compile(_via_rule(key, value))
    _compile(_via_blanket(key, value))


def test_blanket_gate_count_zero_is_rejected_at_compile():
    """The motivating case. `FacilityQueueController` asserts `>= 1` at boot, so this
    used to surface as an agent crash 500 agents into a run rather than as a compile
    error."""
    with pytest.raises(SpecValidationError) as exc:
        _compile(_via_blanket("gate_count", 0))
    assert "positive integer" in str(exc.value)


def test_a_bool_is_rejected_before_the_int_check_on_the_blanket_layer_too():
    with pytest.raises(SpecValidationError, match="boolean"):
        _compile(_via_blanket("gate_count", True))


# --------------------------------------------------------------------------- #
# the guard must be narrow: dead keys and null keep flowing
# --------------------------------------------------------------------------- #

def test_dead_blanket_keys_still_flow_untouched():
    """`facility_type`, `fifo_queue_policy`, `max_queue_size`, `operating_hours`,
    `status` are rejected as RULE targets (they have no runtime reader), but every
    shipped spec sets them on the blanket layer. The allow-list governs what a rule
    may set, not what the blanket layer may carry."""
    compiled = _compile(_via_blanket("gate_count", 1) | {"overrides": {"facility": {
        "gate_count": 1, "service_time": 1800, "facility_type": "Depo",
        "fifo_queue_policy": True, "max_queue_size": None,
        "operating_hours": "24/7", "operating_days": "7 days a week", "status": "Open",
    }}})
    facilities = ScenarioGenerator(compiled.spec).generate().facility
    assert all(b["profile"]["gate_count"] == 1 for b in facilities.values())


def test_a_null_blanket_value_is_not_set_and_is_not_refused():
    """The blanket merge itself skips None (`if v is not None`), so a null there means
    'not set' rather than 'set to null' — the one place this layer's semantics
    legitimately differ from a rule's, where null is a value."""
    compiled = _compile(_via_blanket("gate_count", None))
    facilities = ScenarioGenerator(compiled.spec).generate().facility
    assert all(b["profile"]["gate_count"] == 1 for b in facilities.values())
    # ... whereas a rule may NOT null a non-nullable key.
    with pytest.raises(SpecValidationError, match="null is not a legal value"):
        _compile(_via_rule("gate_count", None))


def test_the_legacy_role_settings_door_is_covered_too():
    """`_merged_overrides` folds `roleSettings.facility.profile` into `overrides`, so
    a check reading only `overrides` would leave a back door open."""
    with pytest.raises(SpecValidationError, match="positive integer"):
        _compile(_base(roleSettings={"facility": {"profile": {"gate_count": 0}}}))


# --------------------------------------------------------------------------- #
# blast radius: measured as zero, pinned as zero
# --------------------------------------------------------------------------- #

def test_all_shipped_scenarios_still_compile_their_blanket_layer():
    """Every shipped spec's `overrides.facility` must pass the new coercers.

    This reads the real scenario folders rather than a fixture: the claim that the
    blast radius is zero is only worth anything if it is checked against what actually
    ships.
    """
    specs = sorted(glob.glob("scenarios/*/spec.json"))
    assert len(specs) >= 10, f"expected the shipped scenario set, found {len(specs)}"
    checked = 0
    for path in specs:
        with open(path, encoding="utf-8") as fp:
            spec = json.load(fp)
        merged = {}
        merged.update((((spec.get("roleSettings") or {}).get("facility") or {})
                       .get("profile") or {}))
        merged.update((spec.get("overrides") or {}).get("facility") or {})
        for key, key_spec in fr.SETTABLE_KEYS.items():
            if key not in merged or merged[key] is None:
                continue
            checked += 1
            key_spec.coerce(merged[key], f"{os.path.basename(os.path.dirname(path))}.{key}")
    assert checked > 0, "no shipped spec carries an allow-listed blanket key — the "\
                        "blast-radius claim would be vacuous"
