"""Phase 3 of ``docs/facility_rules_plan.md`` — ``SPEC_KEYS`` + the recipe echo
(T17–T19), i.e. the F1 trap applied *forward* instead of backward.

``assemble_spec`` builds its output **solely** from ``SPEC_KEYS``, so an
unregistered top-level key vanishes on every dashboard save. The rebate work
registered nothing at the top level to dodge that trap and then walked into the
identical silent drop one level down. This feature adds a top-level key on purpose,
so it must survive all three closed literals it passes through:

1. ``SPEC_KEYS`` + a value builder in ``assemble_spec``'s ``from_body``,
2. the ``recipe`` literal in ``preprocess.py`` (``scenario.json -> $.recipe``),
3. the run provenance stamp (covered in ``test_facility_rules_stamp.py``).

**T17 drives from a COMPILED spec, not a hand-built profile.** That omission is why
the ``rebate_aware`` strip went undetected through three review rounds.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor
from apps.container_logistics.scenario.frontend_scenario_spec import (
    Carry,
    NullPolicy,
    SPEC_KEY_NULL_POLICY,
    SPEC_KEYS,
    _NULL_MEANS_UNSUPPLIED,
    assemble_spec,
)

DOMAIN = "container-logistics-sim-test"
MIDNIGHT = "2020-01-01 00:00:00"
_FACILITY_COUNT = 30


def _body(**kw):
    body = {
        "name": "Rules Save Test",
        "slug": "rules_save_test",
        "domain": DOMAIN,
        "simulationDays": 1,
        "orderCountUnit": "total",
        "referenceTime": MIDNIGHT,
        "agents": {
            "truck": {"count": 6},
            "order": {"count": 12},
            "facility": {"count": _FACILITY_COUNT},
        },
        "earlyOrderCount": 0,
    }
    body.update(kw)
    return body


def _compile(spec_raw):
    return Preprocessor.compile(deepcopy(spec_raw), domain=DOMAIN, reference_time=MIDNIGHT)


def _world_and_ports():
    compiled = _compile(_body())
    sites = compiled.spec.facility_settings["profile"]["facilities"]
    ports = [s["name"] for s in sites if s["code"] == "CT"]
    return fr.world_snapshot(sites, seed=None), ports


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

def test_facility_rules_keys_are_registered_with_a_value_builder():
    assert SPEC_KEYS["facilityRules"] is Carry.CARRY_IF_ABSENT
    assert SPEC_KEYS["facilityRulesWorld"] is Carry.CARRY_IF_ABSENT
    # `assemble_spec` raises a RuntimeError for a registered key with no value
    # builder, so simply calling it proves the builder exists.
    out = assemble_spec(_body(), DOMAIN)
    assert "facilityRules" in out and "facilityRulesWorld" in out
    # CORRECTED (F1). This used to assert the OPPOSITE, faithfully implementing a
    # wrong instruction from the plan: `[]` is in a list's domain and legitimately
    # clears, but `null` is not, and honouring it as a clear wipes the rule list —
    # reverting every per-facility physics value with nothing in the bundle saying so.
    assert SPEC_KEY_NULL_POLICY["facilityRules"] is NullPolicy.UNSUPPLIED
    assert SPEC_KEY_NULL_POLICY["facilityRulesWorld"] is NullPolicy.UNSUPPLIED
    assert "facilityRules" in _NULL_MEANS_UNSUPPLIED


# --------------------------------------------------------------------------- #
# T17 — the mandatory compiled-spec-driven test (§9)
# --------------------------------------------------------------------------- #

def test_facility_rules_survive_assemble_spec_and_compile():
    world, ports = _world_and_ports()
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"name": ports[0]}, "set": {"gate_count": 12}},
    ]
    # 1. the dashboard-save path
    saved = assemble_spec(
        _body(facilityRules=deepcopy(rules), facilityRulesWorld=deepcopy(world)),
        DOMAIN,
    )
    assert saved["facilityRules"] == rules, "the save path dropped the rules"
    assert saved["facilityRulesWorld"] == world

    # 2. the REAL compile, from the saved spec — not a hand-built profile
    compiled = _compile(saved)
    facilities = ScenarioGenerator(compiled.spec).generate().facility
    by_name = {b["profile"]["name"]: b for b in facilities.values()}

    # 3. V11: BOTH the top-level key (which the runtime schema validates) and the
    #    profile key (which facility/manager.py builds the queue from).
    assert by_name[ports[0]]["profile"]["gate_count"] == 12
    assert by_name[ports[0]]["gate_count"] == 12
    assert by_name[ports[1]]["profile"]["gate_count"] == 6
    assert by_name[ports[1]]["gate_count"] == 6
    unruled = [b for n, b in by_name.items() if not n.startswith("port_")]
    assert unruled and all(b["profile"]["gate_count"] == 1 for b in unruled)


# --------------------------------------------------------------------------- #
# T18 — save -> edit -> save (CARRY_IF_ABSENT)
# --------------------------------------------------------------------------- #

def test_facility_rules_survive_a_save_edit_save_round_trip():
    world, ports = _world_and_ports()
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": 6}}]
    first = assemble_spec(
        _body(facilityRules=deepcopy(rules), facilityRulesWorld=deepcopy(world)),
        DOMAIN,
    )

    # A dashboard save that does NOT speak the key (the common case: the editor
    # does not carry rules in v1) must INHERIT it, not null it out. This is the
    # exact shape that silently turned shared-pool planning off.
    second = assemble_spec(_body(simulationDays=2), DOMAIN, previous=first)
    assert second["facilityRules"] == rules
    assert second["facilityRulesWorld"] == world

    # A deep copy, not an alias: mutating the result must not corrupt `previous`.
    second["facilityRules"][0]["set"]["gate_count"] = 99
    assert first["facilityRules"][0]["set"]["gate_count"] == 6

    # An explicit empty list is a supplied CLEAR, not an omission.
    cleared = assemble_spec(_body(facilityRules=[]), DOMAIN, previous=first)
    assert cleared["facilityRules"] == []

    # ... but an explicit NULL is not a clear, it is a lost value (F1). This
    # assertion was inverted when the feature shipped: it asserted the wipe and
    # therefore certified the defect.
    nulled = assemble_spec(_body(facilityRules=None), DOMAIN, previous=first)
    assert nulled["facilityRules"] == rules, (
        "a save payload carrying \"facilityRules\": null must INHERIT, not wipe — a "
        "generic form serialiser emits null for an untouched field, and a wiped rule "
        "list reverts every per-facility value to the blanket layer"
    )
    nulled_world = assemble_spec(_body(facilityRulesWorld=None), DOMAIN, previous=first)
    assert nulled_world["facilityRulesWorld"] == world


# --------------------------------------------------------------------------- #
# T19 — the recipe echo (literal 2)
# --------------------------------------------------------------------------- #

def test_recipe_echoes_facility_rules_verbatim():
    world, ports = _world_and_ports()
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"name": ports[0]}, "set": {"gate_count": 12}},
    ]
    compiled = _compile(
        _body(facilityRules=deepcopy(rules), facilityRulesWorld=deepcopy(world))
    )
    assert compiled.recipe["facilityRules"] == rules
    assert compiled.recipe["facilityRulesWorld"] == world

    # And the round trip: recompiling FROM the recipe reproduces the same world.
    # This is the path a recompile-from-bundle takes; missing the echo makes it
    # silently drop the rules.
    recompiled = Preprocessor.compile(
        deepcopy(compiled.recipe), domain=DOMAIN, reference_time=MIDNIGHT
    )
    a = ScenarioGenerator(compiled.spec).generate().facility
    b = ScenarioGenerator(recompiled.spec).generate().facility
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )
    assert any(x["gate_count"] == 12 for x in b.values()), (
        "fixture: the round trip proved nothing because no rule bit"
    )


def test_the_world_baseline_is_never_written_into_the_compiled_bundle():
    """§7.2: a self-updating fingerprint always matches and guards nothing.

    The baseline may appear in ``$.recipe`` (which IS the authored spec, echoed),
    but it must never be *recomputed* there — the value in the recipe must be the
    one the author recorded, byte for byte, even when it is stale enough to have
    just raised.
    """
    world, ports = _world_and_ports()
    stale = deepcopy(world)
    compiled = _compile(_body(
        facilityRules=[{"match": {"code": "CT"}, "set": {"gate_count": 6}}],
        facilityRulesWorld=stale,
    ))
    assert compiled.recipe["facilityRulesWorld"]["site_digest"] == stale["site_digest"]
    assert compiled.recipe["facilityRulesWorld"] is not stale, "must be a deep copy"
