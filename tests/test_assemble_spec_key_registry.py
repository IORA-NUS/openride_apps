"""`assemble_spec` must MERGE over a saved spec, not reconstruct it (plan §14.5, R3-3).

Round-2 CRITICAL-1: `assemble_spec` built the spec from a flat 18-key dict literal in
which exactly ONE key (`cooperation`) consulted `previous`. A dashboard save sends
`cooperation` but not `planner` / `solverParams` / `overrides` / `earlyOrderCount`, so
all four were rewritten to `null`. Because `_normalize_planner(None)` rebuilds
`topology="partitioned"` and `assignment/app.py` gates the whole feature on
`topology == "pooled"`, **a save silently turned shared-pool planning off while the
user's pools stayed visibly intact in the editor.**

The narrow fix (add `planner` to the dashboard body) would leave the other three broken
and **key nineteen broken by default**. So the fixture below is GENERATED FROM THE
REGISTRY: adding a key to `SPEC_KEYS` without thinking about its carry policy makes
these tests fail rather than silently shipping another dropped key.
"""

import pytest

from apps.container_logistics.scenario.frontend_scenario_spec import (
    SPEC_KEYS,
    Carry,
    assemble_spec,
)

DOMAIN = "container-logistics-sim"


def _sentinel_for(key: str):
    """A distinctive, non-default value for every declared key."""
    special = {
        "name": "previous-name",
        "slug": "previous-slug",
        "domain": DOMAIN,
        "source": "frontend",
        "simulationDays": 7,
        "seed": 424242,
        "orderCountUnit": "per_day",
        # A scenario's own simulation epoch (R2-1 / review F2). CARRY_IF_ABSENT: a
        # dashboard save that does not speak it must INHERIT the authored axis, never
        # reset it — silently reverting a midnight scenario to the 08:00 default would
        # reintroduce the eight-hour axis collision this key exists to prevent.
        "referenceTime": "2020-01-01 00:00:00",
        "agents": {"truck": {"count": 99}, "order": {"count": 98}, "facility": {"count": 97}},
        "earlyOrderCount": 1234,
        "orderDemandCurve": [0.1, 0.2, 0.7],
        "tripMatrix": {"MT": {"CU": 1.0}},
        "hauliers": [{"id": "acme", "fleet_share": 50.0, "order_share": 50.0},
                     {"id": "borax", "fleet_share": 50.0, "order_share": 50.0}],
        "cooperation": {"active": "pair", "structures": [{"id": "pair", "edges": [["acme", "borax"]]}]},
        "planner": {"topology": "pooled", "timing": {"mode": "online"},
                    "market": {"max_rounds": 7}},
        "solver": "GreedyNearest",
        "solverParams": {"dual_cycle_bonus_km": 3.25},
        "roleSettings": {"truck": {"speed": 42}},
        "overrides": {"facility": {"gate_count": 8}},
        # Per-facility rules (facility rules plan §9). CARRY_IF_ABSENT, matching
        # `overrides`: the v1 editor does not carry rules at all, so EVERY dashboard
        # save is silent on this key — the exact shape that silently turned
        # shared-pool planning off. Presence-based, so `[]` is a deliberate clear.
        # A POPULATED list paired with a recorded world — the only legal combination
        # now that "a world with no rules" is a hard error (the world-orphan lock).
        # `[]` + a world block was the fixture here before and is exactly the lost-rule
        # state the lock exists to refuse, so the sentinel could not stay as it was.
        "facilityRules": [{"match": {"code": "CT"}, "set": {"gate_count": 6}}],
        # The recorded facility world those rules were authored against. Carried for
        # the same reason and, additionally, because losing it while KEEPING the
        # rules turns a hard compile error ("rules with no recorded world") into the
        # author's problem for something a save did to them.
        "facilityRulesWorld": {"site_digest": "blake2b16:0000000000000000",
                               "facility_count": 300,
                               "code_counts": {"CT": 6, "CU": 234, "MT": 60}},
    }
    if key not in special:
        raise AssertionError(
            f"SPEC_KEYS gained {key!r} with no sentinel here. Add one AND decide its "
            f"Carry policy — that decision is exactly what this test exists to force."
        )
    return special[key]


def _full_previous():
    return {k: _sentinel_for(k) for k in SPEC_KEYS}


def _dashboard_body():
    """A realistic dashboard save: it speaks some keys and is silent on others."""
    return {
        "name": "edited",
        "slug": "previous-slug",
        "simulationDays": 1,
        "seed": 500777,
        "agents": {"truck": {"count": 12}},
        "hauliers": [{"id": "acme", "fleet_share": 50.0, "order_share": 50.0},
                     {"id": "borax", "fleet_share": 50.0, "order_share": 50.0}],
        "cooperation": {"active": "pair", "structures": [{"id": "pair", "edges": [["acme", "borax"]]}]},
        "solver": "GreedyNearest",
    }


def test_every_declared_key_has_a_sentinel():
    """Guards the guard: the fixture must cover the registry exhaustively."""
    for key in SPEC_KEYS:
        assert _sentinel_for(key) is not None


def test_save_preserves_every_key_absent_from_the_body():
    """THE regression test for CRITICAL-1, generated from the registry.

    Verified to have teeth: reverting `planner` to `deepcopy(raw.get("planner"))`
    makes this fail with "planner was silently dropped".
    """
    previous = _full_previous()
    body = _dashboard_body()
    out = assemble_spec(body, DOMAIN, previous=previous)

    dropped = []
    for key, policy in SPEC_KEYS.items():
        if policy is Carry.CARRY_IF_ABSENT and key not in body:
            if out.get(key) != previous[key]:
                dropped.append((key, previous[key], out.get(key)))
    assert not dropped, "keys silently dropped by a save: " + ", ".join(
        f"{k}: {before!r} -> {after!r}" for k, before, after in dropped
    )


def test_the_four_named_keys_specifically_survive():
    """The exact four the review measured being destroyed."""
    out = assemble_spec(_dashboard_body(), DOMAIN, previous=_full_previous())
    assert out["planner"]["topology"] == "pooled", "shared-pool planning silently disabled"
    assert out["solverParams"] == {"dual_cycle_bonus_km": 3.25}
    assert out["overrides"] == {"facility": {"gate_count": 8}}
    assert out["earlyOrderCount"] == 1234


def test_body_wins_when_it_supplies_the_key():
    out = assemble_spec(
        {**_dashboard_body(), "planner": {"topology": "partitioned"}},
        DOMAIN, previous=_full_previous(),
    )
    assert out["planner"] == {"topology": "partitioned"}


def test_presence_not_truthiness_so_a_client_can_clear_a_field():
    """PRESENCE-based lookup. A truthiness test would make clearing impossible — the
    bug the obvious fix introduces."""
    previous = _full_previous()
    for key, cleared in (("planner", None), ("overrides", None), ("earlyOrderCount", None),
                         ("solverParams", None)):
        out = assemble_spec({**_dashboard_body(), key: cleared}, DOMAIN, previous=previous)
        assert out[key] is None, f"{key}: an explicit clear was overridden by the carry"


def test_falsy_but_meaningful_values_are_carried_not_treated_as_absent():
    """`0` and `{}` are values, not absence."""
    previous = {**_full_previous(), "earlyOrderCount": 0, "overrides": {}}
    out = assemble_spec(_dashboard_body(), DOMAIN, previous=previous)
    assert out["earlyOrderCount"] == 0
    assert out["overrides"] == {}


def test_no_previous_is_byte_identical_to_reconstruction():
    """With `previous=None` the registry must reproduce the historical behaviour
    exactly — that is what keeps every existing round-trip test valid."""
    body = _dashboard_body()
    out = assemble_spec(body, DOMAIN)
    assert out["planner"] is None
    assert out["solverParams"] is None
    assert out["overrides"] is None
    assert out["earlyOrderCount"] is None
    assert set(out) == set(SPEC_KEYS)


def test_output_contains_exactly_the_declared_keys():
    """A key that is not registered is not emitted at all — a loud failure rather
    than a silent null."""
    out = assemble_spec(_dashboard_body(), DOMAIN, previous=_full_previous())
    assert set(out) == set(SPEC_KEYS)
    assert list(out) == list(SPEC_KEYS), "key order drifted; written spec.json is not comparable"


def test_alias_spellings_count_as_supplied():
    """`simulation_days` / `roleProfiles` / the legacy count keys are the same key."""
    previous = _full_previous()
    body = {k: v for k, v in _dashboard_body().items() if k != "simulationDays"}
    out = assemble_spec({**body, "simulation_days": 3}, DOMAIN, previous=previous)
    assert out["simulationDays"] == 3, "alias spelling was ignored and the old value carried"
    # ...and with NEITHER spelling present, the saved value is inherited.
    assert assemble_spec(body, DOMAIN, previous=previous)["simulationDays"] == 7
    out2 = assemble_spec({**_dashboard_body(), "roleProfiles": {"truck": {"speed": 1}}},
                         DOMAIN, previous=previous)
    assert out2["roleSettings"] == {"truck": {"speed": 1}}


def _uncompilable_sentinels():
    """Overrides applied to the sentinel spec before COMPILING it.

    The sentinel values exist to discriminate a dropped carry in `assemble_spec`, which
    is a pure dict operation. Two of them cannot survive a real compile: `name`/`slug`
    must be the fixture's, and `facilityRulesWorld` is a FABRICATED digest that by
    design cannot match a freshly generated facility world. Dropping the rules pair here
    scopes these two arms to what they are about — the `planner` carry — and does not
    weaken them; the rules keys' own carry, including the compiled consequence, is
    covered by `tests/test_facility_rules_spec_keys.py`.
    """
    return {"name": "x", "slug": "x", "facilityRules": None, "facilityRulesWorld": None}


def test_planner_carry_prevents_the_silent_partitioned_flip_end_to_end():
    """The compiled consequence, not just the dict: a pooled scenario must still
    compile to `pooled` after a save that never mentions `planner`."""
    from apps.container_logistics.datagen.preprocess import Preprocessor

    previous = _full_previous()
    saved = assemble_spec(_dashboard_body(), DOMAIN, previous=previous)
    compiled = Preprocessor.compile({**saved, **_uncompilable_sentinels()},
                                    domain="container_logistics")
    assert compiled.recipe["planner"]["topology"] == "pooled"
    assert compiled.recipe["planner"]["market"]["max_rounds"] == 7


# --- the cheap detector for the same bug (plan §14.7 item 5) ------------------

def test_pools_declared_but_partitioned_resolved_emits_a_warning(caplog):
    """The arm must always be NAMED, never inherited silently.

    A scenario carrying cooperation pools that resolves to `partitioned` runs the
    LEGACY planner with its pools sitting visibly intact in the editor — exactly the
    state CRITICAL-1 leaves behind. This warning detects that even when the cause is
    something other than a dropped key.
    """
    import logging

    from apps.container_logistics.datagen.preprocess import Preprocessor

    spec = assemble_spec(_dashboard_body(), DOMAIN)  # no previous => planner is None
    with caplog.at_level(logging.WARNING):
        compiled = Preprocessor.compile({**spec, **_uncompilable_sentinels()},
                                        domain="container_logistics")
    assert compiled.recipe["planner"]["topology"] == "partitioned"
    assert any("shared-pool planning is OFF" in r.getMessage() for r in caplog.records), \
        "a pools-declaring scenario resolved to partitioned with no warning"


def test_no_warning_when_the_scenario_actually_resolves_pooled(caplog):
    import logging

    from apps.container_logistics.datagen.preprocess import Preprocessor

    spec = assemble_spec(_dashboard_body(), DOMAIN, previous=_full_previous())
    with caplog.at_level(logging.WARNING):
        compiled = Preprocessor.compile({**spec, **_uncompilable_sentinels()},
                                        domain="container_logistics")
    assert compiled.recipe["planner"]["topology"] == "pooled"
    assert not any("shared-pool planning is OFF" in r.getMessage() for r in caplog.records)
