"""Phase 1 of ``docs/facility_rules_plan.md`` — the PURE resolver (T1–T12).

Every test below states, in its own body, the fixture property that makes the
assertion *capable of failing* (the plan's §0.2 / F3 lesson: a mutation list guards
the code under test, nothing guards the fixture). A precedence test whose two arms
set the same value proves nothing.

Mutation proofs these back (§14.2): M1 order-independence, M2 max-not-min,
M3 per-key-not-per-rule, M4 equal-rank conflict, M5 zero-match, M6
presence-not-truthiness, M11 the allow-list.
"""

from __future__ import annotations

import itertools
import json

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.facility_rules import FacilityRulesError

CODES = ["CT", "CU", "MT"]


def _sites():
    """Six CT + two CU + two MT, mirroring the real generator's dense, positional
    name space (``port_000 …``) so the zero-match hint has something to show."""
    out = []
    for i in range(6):
        out.append({
            "name": f"port_{i:03d}", "code": "CT", "facility_type": "Port",
            "lat": 1.20 + i / 1000, "lon": 103.80 + i / 1000,
            "gate_count": 1, "service_time": 1800,
        })
    for i in range(2):
        out.append({
            "name": f"customer_{i:03d}", "code": "CU", "facility_type": "Warehouse",
            "lat": 1.30 + i / 1000, "lon": 103.90 + i / 1000,
            "gate_count": 1, "service_time": 1800,
        })
    for i in range(2):
        out.append({
            "name": f"depot_{i:03d}", "code": "MT", "facility_type": "Depot",
            "lat": 1.40 + i / 1000, "lon": 103.70 + i / 1000,
            "gate_count": 1, "service_time": 1800,
        })
    return out


def _schedule(amount=40.0, currency="credit"):
    return {
        "currency": currency,
        "resolution": "hour",
        "points": [{"hour": h, "amount": amount if h < 6 else 0.0} for h in range(24)],
    }


def _resolve(rules, sites=None, blanket=None):
    sites = sites if sites is not None else _sites()
    blanket = blanket if blanket is not None else {}
    fr.validate_facility_rules(rules, sites, CODES, slug="t")
    res = fr.resolve_facility_rules(rules, sites, blanket, slug="t")
    return sites, res


def _by_name(sites, res):
    return {s["name"]: v for s, v in zip(sites, res.per_site)}


# --------------------------------------------------------------------------- #
# T1 / T2 — the rank ladder
# --------------------------------------------------------------------------- #

def test_name_beats_code():
    # FIXTURE PROPERTY: the two rules set the SAME key to DIFFERENT values on the
    # SAME facility. Equal values would make the test pass under `min` too (M2).
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"name": "port_003"}, "set": {"gate_count": 12}},
    ]
    sites, res = _resolve(rules)
    got = _by_name(sites, res)
    assert got["port_003"]["gate_count"] == 12, "the rank-30 name rule must win"
    assert got["port_000"]["gate_count"] == 6, "unnamed CT facilities keep the code rule"


def test_code_beats_scenario_wide():
    # FIXTURE PROPERTY: the blanket value (rank 0) differs from the rule's value.
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": 6}}]
    sites, res = _resolve(rules, blanket={"gate_count": 2, "service_time": 1800})
    got = _by_name(sites, res)
    assert got["port_000"]["gate_count"] == 6
    assert got["customer_000"]["gate_count"] == 2, "CU has no rule -> blanket wins"
    assert got["customer_000"]["service_time"] == 1800


# --------------------------------------------------------------------------- #
# T3 — resolution is per KEY, not per rule (§4.2 / §18.4)
# --------------------------------------------------------------------------- #

def test_name_rule_overrides_only_the_keys_it_sets():
    # FIXTURE PROPERTY: the code rule sets TWO keys, the name rule sets ONE. Under
    # per-RULE resolution the winning name rule would replace the whole `set` and
    # port_003 would silently lose its rebate — a mis-target with no error, from two
    # rules that are each individually correct.
    sched = _schedule()
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6, "rebate": sched}},
        {"match": {"name": "port_003"}, "set": {"gate_count": 12}},
    ]
    sites, res = _resolve(rules)
    got = _by_name(sites, res)
    assert got["port_003"]["gate_count"] == 12
    assert got["port_003"]["rebate"] is not None, (
        "per-KEY resolution: the un-set key must survive the more specific rule"
    )
    assert got["port_003"]["rebate"] == got["port_000"]["rebate"]


# --------------------------------------------------------------------------- #
# T4 — order-independence, proved over all 24 permutations (§4.3)
# --------------------------------------------------------------------------- #

def test_rule_order_does_not_matter():
    # FIXTURE PROPERTY: 4 rules spanning BOTH ranks with at least one overlap
    # (rules 0/2 both bear on port_003's gate_count at different ranks), so a
    # last-wins implementation genuinely produces different answers per ordering.
    base = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"code": "CT"}, "set": {"rebate": _schedule()}},
        {"match": {"name": "port_003"}, "set": {"gate_count": 12}},
        {"match": {"code": "MT"}, "set": {"service_time": 900}},
    ]
    assert len(list(itertools.permutations(base))) == 24

    canonical = None
    for perm in itertools.permutations(base):
        sites, res = _resolve(list(perm))
        blob = json.dumps(_by_name(sites, res), sort_keys=True, default=str)
        if canonical is None:
            canonical = blob
        assert blob == canonical, f"permutation changed the outcome: {perm}"


def test_matcher_ranks_are_unique():
    # A future matcher added at a rank that TIES an existing one silently
    # re-introduces order-dependence, which no other test would catch.
    ranks = fr.MATCHER_RANKS
    assert len(set(ranks.values())) == len(ranks), f"ranks must be a total order: {ranks}"
    assert fr.BLANKET_RANK not in set(ranks.values())


# --------------------------------------------------------------------------- #
# T5 / T6 / T7 — equal specificity (§4.4)
# --------------------------------------------------------------------------- #

def test_equal_specificity_conflict_is_rejected():
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"code": "CT"}, "set": {"gate_count": 8}},
    ]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    msg = str(exc.value)
    assert "equal specificity" in msg
    assert "gate_count" in msg and "port_000" in msg
    assert "rank 10" in msg


def test_equal_specificity_conflict_is_rejected_even_for_identical_values():
    # FIXTURE PROPERTY: the values are IDENTICAL. Deliberate (§4.4): a "harmless"
    # duplicate is how a later edit to one of the two becomes a silent divergence.
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
    ]
    with pytest.raises(FacilityRulesError, match="equal specificity"):
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")


def test_equal_rank_rules_setting_disjoint_keys_are_legal():
    # Composing rules by concern (gates here, prices there) is INTENDED; the naive
    # "duplicate matcher => error" check would forbid it.
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"code": "CT"}, "set": {"rebate": _schedule()}},
    ]
    sites, res = _resolve(rules)
    got = _by_name(sites, res)
    assert got["port_000"]["gate_count"] == 6
    assert got["port_000"]["rebate"] is not None


# --------------------------------------------------------------------------- #
# T8 / T9 — a rule that bites nothing (§4.5)
# --------------------------------------------------------------------------- #

def test_rule_matching_nothing_is_rejected():
    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6}},
        {"match": {"code": "CT"}, "set": {"rebate": _schedule()}},
        {"match": {"name": "port_007"}, "set": {"gate_count": 12}},
    ]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    msg = str(exc.value)
    assert "facilityRules[2] matches no facility" in msg
    # The message must show what DOES exist — a bare "no match" leaves the author
    # guessing at a name space they never chose.
    assert "6 facilities with the 'port' prefix" in msg
    assert "port_000" in msg and "port_005" in msg
    assert "GENERATED" in msg


def test_unknown_code_rule_is_rejected_listing_available():
    rules = [{"match": {"code": "XX"}, "set": {"gate_count": 6}}]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    assert "unknown code 'XX'" in str(exc.value)
    assert "['CT', 'CU', 'MT']" in str(exc.value)


def test_yd_code_rule_is_rejected():
    # YD is in datagen defaults' EXCLUDED_CODES, so it never reaches the active code
    # set — the same unknown-code check catches it for free, no special case needed.
    from apps.container_logistics.datagen import defaults as D

    assert "YD" in D.EXCLUDED_CODES
    assert "YD" not in CODES
    rules = [{"match": {"code": "YD"}, "set": {"gate_count": 6}}]
    with pytest.raises(FacilityRulesError, match="unknown code 'YD'"):
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")


# --------------------------------------------------------------------------- #
# T10 — presence, not truthiness (§4.2)
# --------------------------------------------------------------------------- #

def test_explicit_null_beats_a_lower_rank_rule():
    # FIXTURE PROPERTY: the scenario-wide rebate is set (rank 0) AND is truthy, so a
    # truthiness test on the rule's value (`if rule["set"].get("rebate")`) would fall
    # through to it and MT would wrongly keep a schedule.
    rules = [{"match": {"code": "MT"}, "set": {"rebate": None}}]
    sites, res = _resolve(rules, blanket={"rebate": _schedule(amount=5.0)})
    got = _by_name(sites, res)
    assert got["depot_000"]["rebate"] is None, (
        "an explicit null at rank 10 must beat the scenario-wide schedule at rank 0"
    )
    assert got["port_000"]["rebate"] is not None, "CT still inherits the blanket schedule"

    # And the stamping step must OMIT the key rather than write a null.
    fr.apply_resolution(sites, res)
    depot = next(s for s in sites if s["name"] == "depot_000")
    assert "rebate" not in depot
    port = next(s for s in sites if s["name"] == "port_000")
    assert port["rebate"]["currency"] == "credit"


def test_explicit_null_is_rejected_for_a_non_nullable_key():
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": None}}]
    with pytest.raises(FacilityRulesError, match="null is not a legal value"):
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")


# --------------------------------------------------------------------------- #
# T11 — the allow-list (§6)
# --------------------------------------------------------------------------- #

def test_non_allow_listed_key_is_rejected():
    for key in ("facility_type", "fifo_queue_policy", "location", "name"):
        rules = [{"match": {"code": "CT"}, "set": {key: "whatever"}}]
        with pytest.raises(FacilityRulesError) as exc:
            fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
        msg = str(exc.value)
        assert repr(key) in msg
        assert "['gate_count', 'rebate', 'service_time']" in msg, (
            f"the allow-list must be named in the {key!r} rejection"
        )

    # A DEAD key must say so, not just "not allowed" — otherwise the honest reading
    # is "this is an oversight, add it", which is how the P11 defect gets built.
    rules = [{"match": {"code": "CT"}, "set": {"fifo_queue_policy": True}}]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    msg = str(exc.value)
    assert "nothing reads" in msg
    assert "solver_boundary_audit" in msg
    assert "QUEUE_DISCIPLINE" in msg


def test_allow_list_is_exactly_three_keys():
    # Pins §6.1. Adding a key here is a deliberate act that must come with a named
    # live consumer; this test is the tripwire that forces that conversation.
    assert sorted(fr.SETTABLE_KEYS) == ["gate_count", "rebate", "service_time"]
    assert fr.SETTABLE_KEYS["rebate"].nullable is True
    assert fr.SETTABLE_KEYS["gate_count"].nullable is False
    assert fr.SETTABLE_KEYS["service_time"].nullable is False


# --------------------------------------------------------------------------- #
# schema shape (§5)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rules, needle", [
    ({"match": {"code": "CT"}}, "must be a list"),
    (["nope"], "must be an object"),
    ([{"set": {"gate_count": 2}}], "missing required key 'match'"),
    ([{"match": {"code": "CT"}}], "missing required key 'set'"),
    ([{"match": {"code": "CT"}, "set": {}, "note": "hi"}], "unknown key(s)"),
    ([{"match": "CT", "set": {"gate_count": 2}}], "match: must be an object"),
    ([{"match": {}, "set": {"gate_count": 2}}], "EXACTLY ONE matcher key"),
    ([{"match": {"code": "CT", "name": "port_000"}, "set": {"gate_count": 2}}],
     "EXACTLY ONE matcher key"),
    ([{"match": {"facility_type": "Port"}, "set": {"gate_count": 2}}], "unknown matcher"),
    ([{"match": {"code": ""}, "set": {"gate_count": 2}}], "non-empty string"),
    ([{"match": {"code": "CT"}, "set": []}], "set: must be an object"),
    ([{"match": {"code": "CT"}, "set": {}}], "must set at least one key"),
    ([{"match": {"code": "CT"}, "set": {"gate_count": 0}}], "positive integer"),
    ([{"match": {"code": "CT"}, "set": {"gate_count": 2.5}}], "positive integer"),
    ([{"match": {"code": "CT"}, "set": {"service_time": -1}}], "positive integer"),
])
def test_schema_rejections(rules, needle):
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    assert needle in str(exc.value)


def test_bool_gate_count_is_rejected_before_the_int_check():
    # `isinstance(True, int)` is True in Python, so without an explicit bool guard
    # `"gate_count": true` compiles to ONE gate and the author never learns.
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": True}}]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    assert "boolean" in str(exc.value)


def test_unknown_matcher_lists_the_available_ones():
    rules = [{"match": {"name_prefix": "port"}, "set": {"gate_count": 2}}]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    assert "['code', 'name']" in str(exc.value)


def test_invalid_rebate_schedule_is_rejected_by_the_shared_parser():
    # The rule list changes WHERE a schedule attaches, not what a valid schedule is —
    # so this must be the rebate module's own error text, not a re-implementation.
    rules = [{"match": {"code": "CT"}, "set": {"rebate": {"currency": "credit",
              "points": [{"hour": 99, "amount": 1.0}]}}}]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_facility_rules(rules, _sites(), CODES, slug="x")
    assert "hour" in str(exc.value)


# --------------------------------------------------------------------------- #
# the no-op warning (§5, last row) and the zero-RNG property (§12.1)
# --------------------------------------------------------------------------- #

def test_a_rule_that_changes_nothing_warns(caplog):
    # A no-op rule is how someone believes a lever is engaged when it is not.
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": 1}}]
    sites = _sites()
    fr.validate_facility_rules(rules, sites, CODES, slug="x")
    with caplog.at_level("WARNING"):
        fr.resolve_facility_rules(rules, sites, {"gate_count": 1}, slug="x")
    assert any("changes no effective value" in r.getMessage() for r in caplog.records)
    assert any("NOT engaged" in r.getMessage() for r in caplog.records)


def test_resolution_draws_no_rng():
    """The ONLY test that catches a stray GLOBAL rng draw in the resolver.

    **Corrected (F13).** This comment used to read "datagen is seeded per role, so one
    stray draw inside the resolver would shift every downstream sample" — which is
    false, and it was the plan's own rationale for mutation M9. `_role_rng` returns
    `random.Random(f"{seed}:{role}")`, so **the global `random` stream is disjoint from
    every role stream**: a stray global draw perturbs no output at all and is invisible
    to every output-equality test, `test_rules_do_not_perturb_generation_...` included.

    That makes this assertion **not redundant with the inertness test — it is the only
    thing that catches that mutation.** The false rationale mattered: it put the only
    effective guard one plausible "this is already covered" cleanup away from deletion.

    The other stray-draw class — a draw from a ROLE rng threaded into generation — is
    what the inertness test catches, and it has its own mutation (M9b). Two classes,
    two guards, each with its own proof.
    """
    import random

    rules = [
        {"match": {"code": "CT"}, "set": {"gate_count": 6, "rebate": _schedule()}},
        {"match": {"name": "port_003"}, "set": {"service_time": 600}},
    ]
    sites = _sites()
    fr.validate_facility_rules(rules, sites, CODES, slug="x")
    random.seed(1234)
    before = random.getstate()
    fr.resolve_facility_rules(rules, sites, {}, slug="x")
    assert random.getstate() == before, "the resolver drew from the global RNG"


# --------------------------------------------------------------------------- #
# the site fingerprint (§7.1)
# --------------------------------------------------------------------------- #

def test_site_digest_is_order_sensitive_and_coordinate_sensitive():
    sites = _sites()
    base = fr.site_digest(sites)
    assert base.startswith("blake2b16:")
    assert fr.site_digest(list(sites)) == base

    # Order IS part of the binding: facility_000 <- sites[0].
    swapped = list(sites)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert fr.site_digest(swapped) != base

    # A moved site is a different world even at the same count.
    moved = [dict(s) for s in sites]
    moved[0]["lat"] = moved[0]["lat"] + 0.01
    assert fr.site_digest(moved) != base

    # ... but sub-micro-degree float noise must NOT change it (6 dp rounding).
    jittered = [dict(s) for s in sites]
    jittered[0]["lat"] = jittered[0]["lat"] + 1e-9
    assert fr.site_digest(jittered) == base


def test_world_baseline_accepts_the_world_it_recorded():
    sites = _sites()
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": 6}}]
    snap = fr.world_snapshot(sites, seed=701420930)
    assert snap["facility_count"] == 10
    assert snap["code_counts"] == {"CT": 6, "CU": 2, "MT": 2}
    fr.validate_world_baseline(snap, sites, rules, slug="x")  # must not raise


def test_world_baseline_rejects_a_changed_world():
    sites = _sites()
    rules = [{"match": {"name": "port_003"}, "set": {"gate_count": 12}}]
    snap = fr.world_snapshot(sites, seed=1)

    # Two extra ports: every original name still exists and every rule still
    # matches, so the zero-match rule (§4.5) sees nothing wrong. Only the recorded
    # baseline can catch it.
    grown = _sites()
    for i in (6, 7):
        grown.insert(i, {"name": f"port_{i:03d}", "code": "CT", "facility_type": "Port",
                         "lat": 1.5 + i / 1000, "lon": 103.5 + i / 1000,
                         "gate_count": 1, "service_time": 1800})
    fr.validate_facility_rules(rules, grown, CODES, slug="x")  # no zero-match error

    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_world_baseline(snap, grown, rules, slug="x")
    msg = str(exc.value)
    assert "different facility world" in msg
    assert "rules-baseline" in msg
    assert "match.name='port_003'" in msg


def test_rules_without_a_recorded_world_are_rejected():
    rules = [{"match": {"code": "CT"}, "set": {"gate_count": 6}}]
    with pytest.raises(FacilityRulesError) as exc:
        fr.validate_world_baseline(None, _sites(), rules, slug="x")
    assert "facilityRulesWorld is missing" in str(exc.value)
    assert "rules-baseline x" in str(exc.value)
    # No rules -> no baseline required; the guard is scoped to the feature, not to
    # every scenario in the tree.
    fr.validate_world_baseline(None, _sites(), [], slug="x")
