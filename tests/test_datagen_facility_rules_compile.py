"""Phase 2 of ``docs/facility_rules_plan.md`` — the COMPILE wiring (T13–T16).

The Phase-1 suite proves the resolver is correct. These prove it is *connected*,
which is a different claim and the one the plan's §17.2 calls out: applying rules
before the blanket re-stamp inverts precedence while every pure-resolver test still
passes, because the resolver is right and only the wiring is wrong.

Mutations: M7 (resolver returns nothing — anti-vacuity), M9 (a stray RNG draw).
"""

from __future__ import annotations

import json
import random
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.agents.facility import FacilityAgent
from apps.container_logistics.datagen.builders import FacilityBuilder
from apps.container_logistics.datagen.catalog import LocationCatalog
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container-logistics-sim-test"
MIDNIGHT = "2020-01-01 00:00:00"

# 30 facilities under the default ("allocate") policy + default trip matrix yields
# all three active codes, so a code rule always bites and a code with NO rule always
# exists to act as the control (verified in test_datagen_rebate.py).
_FACILITY_COUNT = 30

_SCHEDULE = {
    "currency": "credit",
    "resolution": "hour",
    "points": [{"hour": h, "amount": 40.0 if h < 6 else -5.0} for h in range(24)],
}


def _spec(rules=None, world=None, **kw):
    spec = {
        "name": "Rules Compile Test",
        "slug": "rules_compile_test",
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
    if rules is not None:
        spec["facilityRules"] = deepcopy(rules)
        spec["facilityRulesWorld"] = deepcopy(world) if world is not None else _world()
    spec.update(kw)
    return spec


def _compile(spec_raw):
    return Preprocessor.compile(deepcopy(spec_raw), domain=DOMAIN, reference_time=MIDNIGHT)


def _sites(compiled):
    return compiled.spec.facility_settings["profile"]["facilities"]


_WORLD_CACHE = {}


def _world():
    """The baseline block for the fixture's own facility world.

    Computed by compiling the rule-less spec once — i.e. exactly what
    ``openride scenario rules-baseline`` does — so the fixture can never drift
    from the generator.
    """
    if "w" not in _WORLD_CACHE:
        _WORLD_CACHE["w"] = fr.world_snapshot(_sites(_compile(_spec())), seed=None)
    return _WORLD_CACHE["w"]


def _generate(spec_raw):
    compiled = _compile(spec_raw)
    result = ScenarioGenerator(compiled.spec).generate()
    return {"truck": result.truck, "order": result.order, "facility": result.facility}


def _port_names():
    return [s["name"] for s in _sites(_compile(_spec())) if s["code"] == "CT"]


# --------------------------------------------------------------------------- #
# T13 — §12.1: the MECHANISM is inert (the VALUES are not, and nothing here claims
#       they are — see the plan's §12.2)
# --------------------------------------------------------------------------- #

_RULED = ("gate_count", "service_time", "rebate")
#: An order behavior copies its facilities' values verbatim — the embedded
#: ``pickup_facility``/``dropoff_facility`` snapshot and the derived
#: ``pickup_service_time``/``dropoff_service_time``. Verified pure copies
#: (``builders.py`` reads ``facility["service_time"]`` straight through; nothing
#: samples from it), so removing them cannot mask an RNG perturbation — a stray
#: draw would move request times, codes, coordinates and durations, all of which
#: stay under comparison.
_ORDER_FACILITY_DERIVED = (
    "pickup_service_time", "dropoff_service_time",
)


def _strip_ruled(collections):
    """Remove every value a rule is ALLOWED to change, wherever it is copied to.

    §12.1's invariant is "nothing is perturbed except the allow-listed keys on the
    facilities the rules match". An order's embedded facility snapshot is a copy of
    a matched facility, so its ruled keys are inside that invariant, not outside it.
    """
    out = deepcopy(collections)
    for behavior in out["facility"].values():
        for key in _RULED:
            behavior.pop(key, None)
            behavior["profile"].pop(key, None)
    for behavior in out["order"].values():
        containers = [behavior, behavior.get("profile") or {}]
        for container in containers:
            for key in _ORDER_FACILITY_DERIVED:
                container.pop(key, None)
            for facility_key in ("pickup_facility", "dropoff_facility"):
                snapshot = container.get(facility_key)
                if isinstance(snapshot, dict):
                    for key in _RULED:
                        snapshot.pop(key, None)
    return out


def test_rules_do_not_perturb_generation_beyond_the_keys_they_set():
    base = _generate(_spec())
    ruled = _generate(_spec([
        {"match": {"code": "CT"}, "set": {"gate_count": 6, "rebate": _SCHEDULE}},
        {"match": {"name": _port_names()[0]}, "set": {"service_time": 900}},
    ]))

    assert json.dumps(_strip_ruled(base), sort_keys=True, default=str) == json.dumps(
        _strip_ruled(ruled), sort_keys=True, default=str
    ), (
        "a rule list must perturb NOTHING in generation except the allow-listed keys "
        "on the facilities it matches — datagen is seeded per role, so one stray RNG "
        "draw in the resolver would shift every downstream sample"
    )

    # The TRUCK collection has no facility-derived field at all, so it is compared
    # whole and untouched: this is where a stray draw surfaces with nothing masked.
    assert json.dumps(base["truck"], sort_keys=True, default=str) == json.dumps(
        ruled["truck"], sort_keys=True, default=str
    )
    # Every order's sampled content (request time, codes, coordinates, durations,
    # haulier) is likewise compared un-stripped except for the copied facility values.
    assert [b["request_time_step"] for b in base["order"].values()] == [
        b["request_time_step"] for b in ruled["order"].values()
    ]

    # Anti-vacuity: the arms MUST differ before stripping, or the assertion above is
    # satisfied by a resolver that does nothing (mutation M7).
    assert json.dumps(base["facility"], sort_keys=True, default=str) != json.dumps(
        ruled["facility"], sort_keys=True, default=str
    ), "the fixture's rules changed nothing — this test would pass on a dead resolver"


def test_a_service_time_rule_reaches_the_orders_that_use_that_facility():
    """The other half of "mechanism-inert, VALUE-live" (§12.2).

    ``_strip_ruled`` above removes the order-side copies so the inertness assertion
    can be about the mechanism. This pins that those copies really do move — without
    it, stripping them could hide a resolver that never reached the order side.
    """
    port = _port_names()[0]
    ruled = _generate(_spec([{"match": {"name": port}, "set": {"service_time": 900}}]))
    touched = [
        b for b in ruled["order"].values()
        if (b.get("pickup_facility") or {}).get("name") == port
    ]
    assert touched, "fixture: no order picks up at the ruled facility"
    assert all(b["pickup_service_time"] == 900 for b in touched)
    assert all(b["pickup_facility"]["service_time"] == 900 for b in touched)


def test_the_resolved_rebate_never_rides_along_into_an_order():
    # A 24-point schedule copied into every one of 5000 orders is pure bundle bloat,
    # and nothing outside datagen reads the embedded snapshot at all.
    ruled = _generate(_spec([{"match": {"code": "CT"}, "set": {"rebate": _SCHEDULE}}]))
    assert any("rebate" in b["profile"] for b in ruled["facility"].values())
    for behavior in ruled["order"].values():
        for key in ("pickup_facility", "dropoff_facility"):
            assert "rebate" not in (behavior.get(key) or {})


# --------------------------------------------------------------------------- #
# T14 — V11: the runtime schema validates the TOP-LEVEL key, the queue controller
#       reads the PROFILE key. Patching either alone is silently wrong.
# --------------------------------------------------------------------------- #

def test_top_level_and_profile_gate_count_agree_for_every_facility():
    ports = _port_names()
    collections = _generate(_spec([
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"name": ports[0]}, "set": {"gate_count": 12, "service_time": 600}},
    ]))
    seen = set()
    for aid, behavior in collections["facility"].items():
        assert behavior["gate_count"] == behavior["profile"]["gate_count"], aid
        assert behavior["service_time"] == behavior["profile"]["service_time"], aid
        seen.add(behavior["profile"]["gate_count"])
    # facility/app.py validates the top-level key; facility/manager.py builds the
    # queue from profile.gate_count. A patch that sets only profile passes the schema
    # and changes queueing; only the top level passes the schema and changes nothing.
    assert {12, 6, 1} <= seen, f"the fixture did not exercise all three ranks: {seen}"


# --------------------------------------------------------------------------- #
# T15 — V15: two facility builders are still both live
# --------------------------------------------------------------------------- #

def test_both_facility_builders_emit_identical_profiles():
    ports = _port_names()
    compiled = _compile(_spec([
        {"match": {"code": "CT"}, "set": {"gate_count": 6, "rebate": _SCHEDULE}},
        {"match": {"name": ports[0]}, "set": {"gate_count": 12}},
        {"match": {"code": "MT"}, "set": {"rebate": None}},
    ]))
    gspec = compiled.spec
    catalog = LocationCatalog(
        gspec.locations_csv, gspec.sg_mask_path, excluded=gspec.excluded_codes
    )

    live_out = FacilityAgent(gspec, catalog, random.Random(0), None, hauliers=[]).generate(
        gspec.num_facilities
    )
    legacy = FacilityBuilder(gspec, catalog)
    agent_ids = list(live_out.keys())
    legacy_out = {
        aid: legacy.build(aid, facility_index=idx) for idx, aid in enumerate(agent_ids)
    }

    assert agent_ids
    saw_rebate = saw_no_rebate = saw_ruled_gate = False
    for aid in agent_ids:
        live = live_out[aid]
        legacy_b = legacy_out[aid]
        assert live["profile"] == legacy_b["profile"], aid
        assert live["gate_count"] == legacy_b["gate_count"], aid
        assert live["service_time"] == legacy_b["service_time"], aid
        if "rebate" in live["profile"]:
            saw_rebate = True
        else:
            saw_no_rebate = True
        if live["gate_count"] != 1:
            saw_ruled_gate = True
    # Both branches must occur or the equality above is vacuous.
    assert saw_rebate and saw_no_rebate and saw_ruled_gate


# --------------------------------------------------------------------------- #
# T16 — §8.3.1: the ordering trap. A correct resolver applied in the wrong place.
# --------------------------------------------------------------------------- #

def test_blanket_override_cannot_overwrite_a_rule():
    # FIXTURE PROPERTY: the blanket layer sets the SAME keys the rules do, to
    # DIFFERENT values. If rules were applied before the blanket re-stamp, every
    # Phase-1 test would still pass and the ports would silently come out at 3.
    ports = _port_names()
    collections = _generate(_spec(
        [
            {"match": {"code": "CT"}, "set": {"gate_count": 6, "service_time": 1200}},
            {"match": {"name": ports[0]}, "set": {"gate_count": 12}},
        ],
        overrides={"facility": {"gate_count": 3, "service_time": 2400}},
    ))
    by_name = {b["profile"]["name"]: b for b in collections["facility"].values()}
    assert by_name[ports[0]]["profile"]["gate_count"] == 12
    assert by_name[ports[1]]["profile"]["gate_count"] == 6
    assert by_name[ports[0]]["profile"]["service_time"] == 1200
    # A code with no rule still gets the blanket value — the layer is not disabled,
    # it is outranked.
    unruled = [b for n, b in by_name.items() if n.startswith("customer_")]
    assert unruled
    assert all(b["profile"]["gate_count"] == 3 for b in unruled)
    assert all(b["profile"]["service_time"] == 2400 for b in unruled)


def test_a_rule_beats_the_scenario_wide_rebate_including_an_explicit_null():
    # The blanket merge skips None (`if v is not None`); re-using that idiom for
    # rules would turn "these facilities get NO schedule" into "these facilities get
    # the default". This is the one precedence trap that survives from the rebate
    # feature verbatim.
    collections = _generate(_spec(
        [{"match": {"code": "MT"}, "set": {"rebate": None}}],
        overrides={"facility": {"rebate": deepcopy(_SCHEDULE)}},
    ))
    by_type = {}
    for behavior in collections["facility"].values():
        t = behavior["profile"]["facility_type"]
        by_type.setdefault(t, []).append("rebate" in behavior["profile"])
    assert by_type["Depot"] and not any(by_type["Depot"]), "MT must resolve to NO schedule"
    assert by_type["Port"] and all(by_type["Port"]), "CT keeps the scenario-wide schedule"
    assert by_type["Warehouse"] and all(by_type["Warehouse"])


def test_the_scenario_wide_rebate_does_not_ride_the_blanket_merge_onto_profiles():
    # `overrides.facility.rebate` is rank 0 and is folded into the site's resolved
    # value. If it were ALSO left on the profile by the blanket merge it would
    # shadow the resolved value and a null rule would appear not to work.
    collections = _generate(_spec(
        [{"match": {"code": "MT"}, "set": {"rebate": None}}],
        overrides={"facility": {"rebate": deepcopy(_SCHEDULE)}},
    ))
    depots = [b for b in collections["facility"].values()
              if b["profile"]["facility_type"] == "Depot"]
    assert depots
    for b in depots:
        assert "rebate" not in b["profile"]


# --------------------------------------------------------------------------- #
# compile-surface errors come out as the house type
# --------------------------------------------------------------------------- #

def test_rule_errors_surface_as_spec_validation_errors():
    with pytest.raises(SpecValidationError, match="matches no facility"):
        _compile(_spec([{"match": {"name": "port_999"}, "set": {"gate_count": 2}}]))
    with pytest.raises(SpecValidationError, match="not a settable facility key"):
        _compile(_spec([{"match": {"code": "CT"}, "set": {"nope": 2}}]))


def test_a_spec_with_no_rules_compiles_exactly_as_before():
    # The feature must be invisible to every scenario that does not use it.
    a = _generate(_spec())
    b = _generate(_spec())
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )
    assert all(f["gate_count"] == 1 for f in a["facility"].values())


def test_the_two_stray_draw_classes_have_distinct_guards():
    """F13 / M9b — pins WHICH guard catches which stray-draw class.

    `ScenarioGenerator._role_rng` returns `random.Random(f"{seed}:{role}")`, so the
    global stream and every role stream are mutually disjoint. Two consequences that
    are easy to get backwards, and the plan got one of them backwards:

    * a stray draw from the **global** `random` perturbs NOTHING and is invisible here
      — only `test_resolution_draws_no_rng` sees it (that was mutation M9, and the
      plan wrongly claimed this test would catch it);
    * a stray draw from a **role** rng shifts every later sample for that role, and
      only an output-equality test like this one sees it (M9b).

    Neither guard subsumes the other, which is why both exist.
    """
    import random

    from apps.container_logistics.datagen.generator import ScenarioGenerator

    a = ScenarioGenerator._role_rng(42, "facility")
    b = ScenarioGenerator._role_rng(42, "truck")
    assert a.random() != b.random(), "role streams are supposed to be independent"

    # A global draw cannot move a role stream: same seed+role, same first value,
    # regardless of what the global stream did in between.
    first = ScenarioGenerator._role_rng(42, "facility").random()
    random.seed(1)
    for _ in range(100):
        random.random()
    assert ScenarioGenerator._role_rng(42, "facility").random() == first, (
        "a global draw perturbed a role stream — if this ever becomes true, M9's "
        "original rationale becomes correct and the guards can be reconsidered"
    )
