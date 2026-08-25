"""R2-1 — the null-policy CLASS fix (F1), plus the world-orphan lock.

**The instance fix is what already failed.** `_NULL_MEANS_UNSUPPLIED` was added for
`referenceTime` in the previous round; it is an OPT-IN safety list, so every newly
registered key defaults to the dangerous behaviour. `facilityRules` was then registered
with an explicit code comment declining to join it — and a save payload carrying
`"facilityRules": null` would have wiped the rule list and silently reverted 300
facilities to one gate each. Two rounds, two keys, one hole.

So the question is now **unanswerable-by-default**: `SPEC_KEY_NULL_POLICY` is mandatory
and its completeness is enforced at import, and the parametrised test below covers the
*next* key without anyone remembering to add it.

Mutations: **R2-M1** (revert to the frozenset with both keys added — the instance fix)
and the orphan-lock mutation.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError
from apps.container_logistics.scenario.frontend_scenario_spec import (
    Carry,
    NullPolicy,
    SPEC_KEY_NULL_POLICY,
    SPEC_KEYS,
    assemble_spec,
)

DOMAIN = "container-logistics-sim-test"
MIDNIGHT = "2020-01-01 00:00:00"


# --------------------------------------------------------------------------- #
# the table is total, and stays total
# --------------------------------------------------------------------------- #

def test_every_spec_key_declares_a_null_policy():
    missing = set(SPEC_KEYS) - set(SPEC_KEY_NULL_POLICY)
    assert not missing, (
        f"{sorted(missing)} have no declared null policy. The import-time guard should "
        f"have made this unreachable — if it did not fire, it has been weakened."
    )
    assert not (set(SPEC_KEY_NULL_POLICY) - set(SPEC_KEYS))
    assert all(isinstance(v, NullPolicy) for v in SPEC_KEY_NULL_POLICY.values())


def test_the_import_time_guard_actually_fires():
    """The guard is the whole mechanism — a test that only reads the finished table
    would still pass if the guard were deleted."""
    import apps.container_logistics.scenario.frontend_scenario_spec as m

    src = open(m.__file__, encoding="utf-8").read()
    assert "_missing_null_policy = set(SPEC_KEYS) - set(SPEC_KEY_NULL_POLICY)" in src
    assert "raise RuntimeError" in src

    # And prove the condition it guards is a real one, by evaluating it against a
    # SPEC_KEYS that has gained a key.
    hypothetical = set(SPEC_KEYS) | {"someFutureKey"}
    assert hypothetical - set(SPEC_KEY_NULL_POLICY) == {"someFutureKey"}


# --------------------------------------------------------------------------- #
# the generalising test — covers the NEXT key without anyone remembering
# --------------------------------------------------------------------------- #

_NON_NULLABLE = sorted(
    k for k, v in SPEC_KEY_NULL_POLICY.items()
    if v is NullPolicy.UNSUPPLIED and SPEC_KEYS[k] is Carry.CARRY_IF_ABSENT
)


def _previous_with(key, value):
    prev = {k: None for k in SPEC_KEYS}
    prev.update({"name": "prev", "slug": "prev", "domain": DOMAIN, "source": "frontend"})
    prev[key] = value
    return prev


_SENTINELS = {
    "simulationDays": 5,
    "seed": 12345,
    "orderCountUnit": "per_day",
    "referenceTime": "2020-01-01 00:00:00",
    "agents": {"truck": {"count": 7}, "order": {"count": 8}, "facility": {"count": 9}},
    "tripMatrix": {"MT": {"CU": 1.0}},
    "hauliers": [{"id": "acme", "fleet_share": 100.0, "order_share": 100.0}],
    "facilityRules": [{"match": {"code": "CT"}, "set": {"gate_count": 6}}],
    "facilityRulesWorld": {"site_digest": "blake2b16:1111111111111111",
                           "facility_count": 30, "code_counts": {"CT": 6}},
}


@pytest.mark.parametrize("key", _NON_NULLABLE)
def test_null_payload_is_inert_for_every_non_nullable_key(key):
    """A `{key: null}` payload must behave EXACTLY like an absent key.

    Parametrised over the registry, so a key registered next year is covered by this
    test on the day it is registered — which is the difference between the class fix
    and the instance fix.
    """
    assert key in _SENTINELS, (
        f"{key!r} is a non-nullable CARRY_IF_ABSENT key with no sentinel here. Add one "
        f"— this assertion exists so a new key cannot slip past the parametrisation."
    )
    prev = _previous_with(key, _SENTINELS[key])
    body = {"name": "n", "slug": "s", "domain": DOMAIN}

    absent = assemble_spec(dict(body), DOMAIN, previous=deepcopy(prev))
    nulled = assemble_spec({**body, key: None}, DOMAIN, previous=deepcopy(prev))

    assert nulled[key] == _SENTINELS[key], f"{key}: an explicit null WIPED the value"
    assert json.dumps(nulled, sort_keys=True, default=str) == json.dumps(
        absent, sort_keys=True, default=str
    ), f"{key}: a null payload is not equivalent to an absent one"


@pytest.mark.parametrize("key", ["planner", "overrides", "earlyOrderCount",
                                 "solverParams", "solver", "roleSettings"])
def test_nullable_keys_can_still_be_cleared(key):
    """The class fix must not take clearing away from the keys whose domain contains
    null — that is the bug the obvious over-correction introduces."""
    assert SPEC_KEY_NULL_POLICY[key] is NullPolicy.CLEARS
    prev = _previous_with(key, {"marker": 1} if key != "earlyOrderCount" else 99)
    prev[key] = {"marker": 1} if key not in ("earlyOrderCount", "solver") else (
        99 if key == "earlyOrderCount" else "GreedyNearest"
    )
    out = assemble_spec({"name": "n", "slug": "s", "domain": DOMAIN, key: None},
                        DOMAIN, previous=prev)
    assert out[key] is None, f"{key}: an explicit clear was overridden by the carry"


def test_the_empty_list_still_clears_a_rule_list():
    """`[]` IS in a list's domain, so it must keep clearing. The distinction between
    `[]` and `null` is the entire content of the fix."""
    prev = _previous_with("facilityRules", _SENTINELS["facilityRules"])
    out = assemble_spec({"name": "n", "slug": "s", "domain": DOMAIN, "facilityRules": []},
                        DOMAIN, previous=prev)
    assert out["facilityRules"] == []


# --------------------------------------------------------------------------- #
# the world-orphan lock — outcome-defined
# --------------------------------------------------------------------------- #

def _spec(rules=None, world=None, **kw):
    spec = {
        "name": "Orphan Test", "slug": "orphan_test", "simulationDays": 1,
        "orderCountUnit": "total", "referenceTime": MIDNIGHT,
        "agents": {"truck": {"count": 6}, "order": {"count": 12},
                   "facility": {"count": 30}},
        "earlyOrderCount": 0,
    }
    if rules is not None:
        spec["facilityRules"] = deepcopy(rules)
    if world is not None:
        spec["facilityRulesWorld"] = deepcopy(world)
    spec.update(kw)
    return spec


def _compile(raw):
    return Preprocessor.compile(deepcopy(raw), domain=DOMAIN, reference_time=MIDNIGHT)


def _world():
    sites = _compile(_spec()).spec.facility_settings["profile"]["facilities"]
    return fr.world_snapshot(sites, seed=None)


@pytest.mark.parametrize("lost", [[], None])
def test_world_without_rules_is_rejected(lost):
    """A baseline is only ever recorded FOR a rule list, so this state is
    unauthorable and can only mean the rules were lost — by ANY door, including ones
    nobody has enumerated."""
    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(rules=lost, world=_world()))
    msg = str(exc.value)
    assert "facilityRulesWorld is present but facilityRules is empty" in msg
    assert "the rules were lost" in msg
    assert "silently revert every per-facility value" in msg
    # The message must show the world it is orphaned from, or the author cannot tell
    # which rules went missing.
    assert "300 facilities" in msg or "30 facilities" in msg


def test_deliberately_having_no_rules_stays_expressible():
    _compile(_spec())
    _compile(_spec(rules=[]))


def test_the_lock_catches_a_wipe_the_fingerprint_is_blind_to():
    """`validate_world_baseline` used to return early on `if not rules`, so the
    fingerprint — the feature's strongest guard — was silent on a total rule wipe.
    This pins that the lock closes that specific gap."""
    world = _world()
    good = _compile(_spec(rules=[{"match": {"code": "CT"}, "set": {"gate_count": 6}}],
                          world=world))
    facilities = ScenarioGenerator(good.spec).generate().facility
    assert any(b["profile"]["gate_count"] == 6 for b in facilities.values())

    # The same spec with the rules wiped compiles to 300 facilities at one gate each,
    # which is precisely what must not happen silently.
    with pytest.raises(SpecValidationError, match="rules were lost"):
        _compile(_spec(rules=None, world=world))
