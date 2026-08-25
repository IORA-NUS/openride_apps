"""Phase 4 of ``docs/facility_rules_plan.md`` — the staleness guard (T20–T23).

**This is the load-bearing half of the addressing guard, and the mismatch rule is
the cheap complement.** A zero-match error catches a renamed or deleted facility. It
structurally cannot catch the expensive case: the name space is dense and positional
(``port_000 … port_005``), so regenerating with 8 ports instead of 6 leaves all six
original names in existence, every rule matching, and nothing erroring — while the
single shared RNG stream across codes means several of those names now denote
different physical sites. Zero rules mismatch; every rule mis-targets.

Mutation **M8** — recompute the digest at compile and write it back — is the
well-known way this guard becomes a no-op that still looks like a guard.
"""

from __future__ import annotations

import random
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.catalog import _FACILITY_SAMPLE_SEED
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container-logistics-sim-test"
MIDNIGHT = "2020-01-01 00:00:00"


def _spec(facility_count=30, rules=None, world=None, seed=4242, **kw):
    spec = {
        "name": "World Guard Test",
        "slug": "world_guard_test",
        "simulationDays": 1,
        "orderCountUnit": "total",
        "referenceTime": MIDNIGHT,
        "seed": seed,
        "agents": {
            "truck": {"count": 6},
            "order": {"count": 12},
            "facility": {"count": facility_count},
        },
        "earlyOrderCount": 0,
    }
    if rules is not None:
        spec["facilityRules"] = deepcopy(rules)
        if world is not None:
            spec["facilityRulesWorld"] = deepcopy(world)
    spec.update(kw)
    return spec


def _compile(spec_raw):
    return Preprocessor.compile(deepcopy(spec_raw), domain=DOMAIN, reference_time=MIDNIGHT)


def _sites(facility_count=30, seed=4242):
    return _compile(_spec(facility_count, seed=seed)).spec.facility_settings["profile"]["facilities"]


def _world_for(facility_count=30, seed=4242):
    return fr.world_snapshot(_sites(facility_count, seed=seed), seed=seed)


# --------------------------------------------------------------------------- #
# T20 — a changed world is refused
# --------------------------------------------------------------------------- #

def test_a_changed_facility_world_is_rejected():
    ports = [s["name"] for s in _sites(30) if s["code"] == "CT"]
    rules = [{"match": {"name": ports[0]}, "set": {"gate_count": 12}}]
    world_30 = _world_for(30)

    # Sanity: against the world it was recorded for, this compiles.
    _compile(_spec(30, rules, world_30))

    # Now grow the facility count. The rule's target name still EXISTS, so nothing
    # zero-matches — the mismatch rule sees a perfectly healthy rule list.
    grown = _sites(60)
    assert ports[0] in {s["name"] for s in grown}, (
        "fixture: the name must still exist, or this test proves the cheap rule, "
        "not the fingerprint"
    )
    fr.validate_facility_rules(rules, grown, ["CT", "CU", "MT"], slug="x")  # no error

    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(60, rules, world_30))
    msg = str(exc.value)
    assert "different facility world" in msg
    assert "recorded" in msg and "now" in msg
    assert world_30["site_digest"] in msg
    assert "rules-baseline" in msg
    assert "does NOT check that your rules still mean what you intended" in msg
    assert f"match.name={ports[0]!r}" in msg, (
        "the message must name the rules that address a facility BY NAME — they are "
        "the ones a shifted site list silently re-targets"
    )


def test_the_shared_rng_stream_really_does_move_sites_when_a_count_changes():
    """The premise the whole guard rests on (plan V4), pinned as a test.

    All codes are sampled from ONE ``random.Random(_FACILITY_SAMPLE_SEED)`` stream in
    code order, so changing the total facility count re-partitions every code and
    re-draws every later one. If this ever stopped being true the guard would be
    over-strict, and someone would be tempted to weaken it without knowing why it
    was there.
    """
    small = {s["name"]: (s["lat"], s["lon"]) for s in _sites(30)}
    large = {s["name"]: (s["lat"], s["lon"]) for s in _sites(60)}
    shared = set(small) & set(large)
    assert shared, "fixture: the two worlds share no names at all"
    moved = [n for n in shared if small[n] != large[n]]
    assert moved, (
        "a name that survives a regeneration is supposed to be able to denote a "
        "DIFFERENT physical site — that is the entire reason the fingerprint exists"
    )


# --------------------------------------------------------------------------- #
# T21 — rules with no recorded world are refused
# --------------------------------------------------------------------------- #

def test_rules_without_a_recorded_world_are_rejected():
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": 6}}]
    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(30, rules, world=None))
    msg = str(exc.value)
    assert "facilityRulesWorld is missing" in msg
    assert "rules-baseline world_guard_test" in msg
    # Auto-populating on first compile is the self-updating fingerprint in disguise:
    # it would silently bless whatever world happened to exist.
    assert "silently bless" in msg


def test_a_scenario_with_no_rules_needs_no_recorded_world():
    # The guard is scoped to the feature, not to every scenario in the tree.
    _compile(_spec(30))
    _compile(_spec(30, rules=[]))


# --------------------------------------------------------------------------- #
# T22 — --reseed alone must NOT trip the guard (plan §7.5 / §18.1)
# --------------------------------------------------------------------------- #

def test_reseed_alone_does_not_trip_the_guard():
    """Blocking ``--reseed`` for a rules-carrying scenario would be a defect, not
    caution: facility placement is seeded from a module constant, never from the
    spec seed, so a reseed provably cannot move a facility. Pinned as a test rather
    than believed, because the belief is what a future edit would break.
    """
    assert _FACILITY_SAMPLE_SEED == 20260617

    ports = [s["name"] for s in _sites(30, seed=4242) if s["code"] == "CT"]
    rules = [{"match": {"name": ports[0]}, "set": {"gate_count": 12}}]
    world = _world_for(30, seed=4242)

    # `--reseed` redraws ONLY the master spec seed.
    for reseeded in (1, 999999, random.Random(0).randint(1, 2**31 - 1)):
        assert fr.site_digest(_sites(30, seed=reseeded)) == world["site_digest"], (
            "a reseed moved a facility — the guard's premise is broken, not the guard"
        )
        compiled = _compile(_spec(30, rules, world, seed=reseeded))
        facilities = ScenarioGenerator(compiled.spec).generate().facility
        by_name = {b["profile"]["name"]: b for b in facilities.values()}
        assert by_name[ports[0]]["profile"]["gate_count"] == 12

    # ... and the recorded seed is deliberately NOT compared, so a stale
    # `seed_at_baseline` cannot fail a compile on its own.
    stale = deepcopy(world)
    stale["seed_at_baseline"] = -1
    _compile(_spec(30, rules, stale, seed=777))


def test_the_guard_still_fires_when_something_else_moved_even_under_a_reseed():
    # The fingerprint is defined over the OUTCOME, so it does not care whether a
    # reseed was involved — which is the point of §0.4.
    ports = [s["name"] for s in _sites(30) if s["code"] == "CT"]
    rules = [{"match": {"name": ports[0]}, "set": {"gate_count": 12}}]
    with pytest.raises(SpecValidationError, match="different facility world"):
        _compile(_spec(60, rules, _world_for(30), seed=987654))


# --------------------------------------------------------------------------- #
# the digest itself
# --------------------------------------------------------------------------- #

def test_the_digest_covers_identity_position_and_place():
    sites = _sites(30)
    base = fr.site_digest(sites)

    renamed = deepcopy(sites)
    renamed[0]["name"] = "port_ZZZ"
    assert fr.site_digest(renamed) != base

    recoded = deepcopy(sites)
    recoded[0]["code"] = "CU"
    assert fr.site_digest(recoded) != base

    reordered = deepcopy(sites)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    assert fr.site_digest(reordered) != base, (
        "generation ORDER is part of the binding — facility_000 <- sites[0]"
    )

    dropped = deepcopy(sites)[:-1]
    assert fr.site_digest(dropped) != base
