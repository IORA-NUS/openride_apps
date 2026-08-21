"""The authoring surface survives a dashboard save (plan §4.3, G23).

The plan asserts that riding the existing ``overrides.facility`` key "sidesteps the
``SPEC_KEYS`` data-loss trap entirely". That is a claim about a *top-level* key, and
the rebate block is **nested** — so it is not automatically true and it is exactly the
kind of thing that fails silently. G23's mechanism is that ``assemble_spec`` ITERATES
``SPEC_KEYS``, so an unregistered top-level key vanishes on every save; the question
here is whether an unrecognised *nested* key inside a registered one survives too.

It does, and these tests pin it: ``overrides`` is ``Carry.CARRY_IF_ABSENT`` and is
``deepcopy``d verbatim, so nothing inspects its interior. If someone later replaces
that with a field-by-field rebuild of ``overrides.facility``, a rebate schedule would
be silently dropped on the next dashboard save and the only symptom would be
``meta.rebate.enabled == false`` on a scenario that visibly declares a schedule.
"""

import copy

from apps.container_logistics.scenario.frontend_scenario_spec import (
    SPEC_KEYS,
    Carry,
    assemble_spec,
)

DOMAIN = "container_logistics"

REBATE_OVERRIDES = {
    "facility": {
        "gate_count": 1,
        "rebate": {
            "currency": "credit",
            "resolution": "hour",
            "points": [{"hour": 22, "amount": 40.0}, {"hour": 8, "amount": -15.0}],
        },
        "rebate_by_code": {
            "CT": {"points": [{"hour": 22, "amount": 40.0}]},
            "MT": None,
        },
    }
}


def _previous():
    return {
        "name": "Flagship",
        "slug": "flagship",
        "domain": DOMAIN,
        "source": "frontend",
        "simulationDays": 7,
        "agents": {"truck": {"count": 500}, "order": {"count": 5000},
                   "facility": {"count": 300}},
        "overrides": copy.deepcopy(REBATE_OVERRIDES),
    }


def test_overrides_is_a_registered_carry_if_absent_spec_key():
    """The precondition the whole authoring choice rests on."""
    assert SPEC_KEYS["overrides"] is Carry.CARRY_IF_ABSENT


def test_a_save_that_omits_overrides_keeps_the_rebate_schedule():
    """The CRITICAL-1 mechanism: a dashboard save sends only some keys.

    A save that does not speak ``overrides`` at all must inherit the previous one
    intact — schedule included — rather than rewriting it to ``null``.
    """
    body = {"name": "Flagship", "slug": "flagship", "domain": DOMAIN,
            "cooperation": {"structures": []}}

    spec = assemble_spec(body, DOMAIN, previous=_previous())

    facility = spec["overrides"]["facility"]
    assert facility["rebate"]["points"][0] == {"hour": 22, "amount": 40.0}
    assert facility["rebate"]["currency"] == "credit"
    # The null entry must survive as an explicit null — "depots pay nothing" is not
    # the same statement as "depots were not mentioned".
    assert "MT" in facility["rebate_by_code"]
    assert facility["rebate_by_code"]["MT"] is None


def test_a_save_that_supplies_overrides_carries_the_nested_block_verbatim():
    """An unrecognised nested key must not be filtered out of a supplied ``overrides``."""
    body = {
        "name": "Flagship", "slug": "flagship", "domain": DOMAIN,
        "overrides": copy.deepcopy(REBATE_OVERRIDES),
    }

    spec = assemble_spec(body, DOMAIN, previous=None)

    assert spec["overrides"]["facility"]["rebate"] == REBATE_OVERRIDES["facility"]["rebate"]
    assert spec["overrides"]["facility"]["rebate_by_code"]["CT"]["points"] == [
        {"hour": 22, "amount": 40.0}
    ]


def test_assemble_spec_deep_copies_overrides_rather_than_aliasing():
    """A shared reference would let a later mutation of one spec corrupt another."""
    previous = _previous()
    spec = assemble_spec(
        {"name": "Flagship", "slug": "flagship", "domain": DOMAIN}, DOMAIN,
        previous=previous,
    )
    spec["overrides"]["facility"]["rebate"]["points"][0]["amount"] = 999.0
    assert previous["overrides"]["facility"]["rebate"]["points"][0]["amount"] == 40.0


def test_no_new_top_level_spec_key_was_introduced():
    """Plan §4.3: the rebate authoring surface adds NO top-level ``spec.json`` key.

    An unregistered top-level key vanishes on every dashboard save (G23), so this
    test is the guard against someone "simplifying" the authoring surface by hoisting
    ``rebate`` to the top level without registering it.
    """
    assert "rebate" not in SPEC_KEYS
    assert "rebate_by_code" not in SPEC_KEYS
