"""R2-6 (review F5) — mixed currency is unauthorable; plus the F7 flat-schedule warning.

`RebateBook.currency` reduces a set of labels to one by taking the alphabetically first,
and the haulier ledger publishes a single `rebate_credited` beside a single
`rebate_currency`. Two currencies in one bundle therefore sum unlike units into one
scalar and label it with whichever name sorts first. Rejecting the combination at
compile makes the runtime currency property TOTAL — which is why this lands before the
per-leg split (plan §18.6): a split bucket never has to decide its currency.

The flat-schedule warning is the F7 RE-SCOPE: the hour-invariant share of the first
run's ledger was an authored flat schedule, not a ledger defect.
"""

import copy
import logging

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container_logistics"
MIDNIGHT = "2020-01-01 00:00:00"


def _points(amount):
    return [{"hour": h, "amount": amount} for h in range(24)]


def _varied():
    return [{"hour": h, "amount": 40.0 if h < 6 else -5.0} for h in range(24)]


def _rule(code, block):
    """A rank-10 per-code rule — the surface that replaced `rebate_by_code`."""
    return {"match": {"code": code}, "set": {"rebate": copy.deepcopy(block)}}


def _spec(facility_overrides, rules=None):
    spec = {
        "name": "Currency probe", "slug": "currency_probe", "domain": DOMAIN,
        "simulationDays": 1, "seed": 99,
        "referenceTime": MIDNIGHT,
        "agents": {"truck": {"count": 6}, "order": {"count": 30},
                   "facility": {"count": 12}},
        "overrides": {"facility": copy.deepcopy(facility_overrides)},
    }
    if rules is not None:
        # A rule list is only meaningful against the world it was authored for, so
        # compile refuses one without a recorded fingerprint.
        spec["facilityRules"] = copy.deepcopy(rules)
        spec["facilityRulesWorld"] = copy.deepcopy(_world())
    return spec


def _compile(facility_overrides, rules=None):
    return Preprocessor.compile(_spec(facility_overrides, rules), domain=DOMAIN,
                                reference_time=MIDNIGHT)


_WORLD_CACHE: dict = {}


def _world():
    """This module's own `facilityRulesWorld` baseline, taken from a rule-less compile
    (what `openride scenario rules-baseline` does) so it cannot drift."""
    if "w" not in _WORLD_CACHE:
        compiled = Preprocessor.compile(_spec({}), domain=DOMAIN,
                                        reference_time=MIDNIGHT)
        sites = compiled.spec.facility_settings["profile"]["facilities"]
        _WORLD_CACHE["w"] = fr.world_snapshot(sites, seed=None)
    return _WORLD_CACHE["w"]


# ------------------------------------------------------------------ mixed currency


def test_mixed_currency_rejected_at_compile():
    with pytest.raises(SpecValidationError) as exc:
        _compile(
            {"rebate": {"currency": "credit", "points": _varied()}},
            rules=[_rule("CT", {"currency": "eur", "points": _varied()})],
        )
    msg = str(exc.value)
    assert "currencies" in msg
    assert "'credit'" in msg and "'eur'" in msg
    assert "sorts first" in msg  # names the actual failure mode, not just "invalid"


def test_mixed_currency_across_two_rules_is_also_rejected():
    """Two per-code rules disagreeing, with no scenario-wide default at all."""
    with pytest.raises(SpecValidationError, match="currencies"):
        _compile({}, rules=[
            _rule("CT", {"currency": "eur", "points": _varied()}),
            _rule("CU", {"currency": "usd", "points": _varied()}),
        ])


def test_a_single_currency_compiles():
    compiled = _compile(
        {"rebate": {"currency": "credit", "points": _varied()}},
        rules=[_rule("CT", {"currency": "credit", "points": _points(9.0)})],
    )
    assert compiled is not None


def _effective_currencies(compiled):
    """The currencies actually resolved onto the compiled sites.

    `assert compiled is not None` only says "compile did not raise", which a
    disconnected resolver also satisfies. The claim these two tests make is about
    the EFFECTIVE set, so assert on the effective set.
    """
    sites = compiled.spec.facility_settings["profile"]["facilities"]
    return {s["rebate"]["currency"] for s in sites if s.get("rebate")}


def test_the_check_is_over_the_effective_set_not_the_authored_blocks():
    """The authored default is in a different currency but NO facility resolves to it.

    Every active code is overridden by a rank-10 rule, so the effective bundle is
    single-currency and must compile even though 'eur' is authored right there in the
    spec. Judging the authored blocks instead would reject a valid scenario — the same
    effective-not-authored discipline the provenance stamp already follows.

    The check reads `site["rebate"]` *after* resolution, so this fixture is now a
    sharper statement of the same claim than it was under `rebate_by_code`: the
    authored 'eur' block really is parsed, really is rank 0, and really is beaten
    everywhere.
    """
    compiled = _compile(
        {"rebate": {"currency": "eur", "points": _varied()}},
        rules=[
            _rule("CT", {"currency": "credit", "points": _varied()}),
            _rule("CU", {"currency": "credit", "points": _varied()}),
            _rule("MT", {"currency": "credit", "points": _varied()}),
        ],
    )
    assert _effective_currencies(compiled) == {"credit"}, (
        "the authored 'eur' default must be beaten EVERYWHERE — if any facility still "
        "resolves to it, this scenario is mixed-currency and should have been refused"
    )


def test_a_null_rebate_rule_does_not_contribute_a_currency():
    """A suppressed code has no schedule, so it cannot make a bundle mixed-currency.

    The rank-0 default is 'eur' and MT is the ONLY code left resolving to it — until
    its `{"rebate": null}` rule suppresses it. Presence, not truthiness: if the
    resolver tested the null for truth instead of testing the key for membership, MT
    would fall back to the 'eur' default and the effective set would be mixed.
    """
    compiled = _compile(
        {"rebate": {"currency": "eur", "points": _varied()}},
        rules=[
            _rule("CT", {"currency": "credit", "points": _varied()}),
            _rule("CU", {"currency": "credit", "points": _varied()}),
            {"match": {"code": "MT"}, "set": {"rebate": None}},
        ],
    )
    assert _effective_currencies(compiled) == {"credit"}
    sites = compiled.spec.facility_settings["profile"]["facilities"]
    depots = [x for x in sites if x["code"] == "MT"]
    assert depots and not any("rebate" in x for x in depots), (
        "MT must resolve to NO schedule — a truthiness test on the null would let it "
        "fall back to the 'eur' default and quietly make the bundle mixed-currency"
    )


# ------------------------------------------------------- the F7 flat-schedule warning


def test_flat_schedule_warns_that_it_is_not_an_incentive(caplog):
    with caplog.at_level(logging.WARNING):
        _compile({"rebate": {"currency": "credit", "points": _points(5.0)}})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "FLAT" in text
    assert "participation payment" in text


def test_a_varied_schedule_does_not_warn_about_flatness(caplog):
    with caplog.at_level(logging.WARNING):
        _compile({"rebate": {"currency": "credit", "points": _varied()}})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "participation payment" not in text
