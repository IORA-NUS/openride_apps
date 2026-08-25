"""R2-2 (F3) — the fourth closed literal, and the legacy path's refusal.

`scenario_manager.generate_random_behaviors` builds its override payload from an
explicit dict literal that dropped `facilityRules`. The consequence was not a crash
and not a warning: the legacy path produced a **complete, internally consistent,
rules-free world** — 300 facilities at one gate each — for a feature that exists
because per-facility values were unexpressible.

**The two halves are ordered and the order is not optional.** `build_generation_spec`
cannot raise on a value the literal never passes it, so the literal is fixed first;
reversed, the refusal ships and does nothing, which is this feature's signature
failure mode.

The plan also asks for a *generalising* test — §19.2 item 2 — because "all three closed
literals" was an enumeration answering a question about *every* literal.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from apps.container_logistics.scenario import scenario_config
from apps.container_logistics.scenario.frontend_scenario_spec import (
    SPEC_KEYS,
    assemble_spec,
    frontend_scenario_config_override,
    normalize_generate_spec,
)
from apps.container_logistics.scenario.scenario_datagen import build_generation_spec

DOMAIN = "container-logistics-sim-test"

_RULES = [{"match": {"code": "CT"}, "set": {"gate_count": 6}}]
_WORLD = {"site_digest": "blake2b16:2222222222222222", "facility_count": 30,
          "code_counts": {"CT": 6, "CU": 15, "MT": 9}}


def _spec(**kw):
    spec = {
        "name": "Legacy Probe", "slug": "legacy_probe", "domain": DOMAIN,
        "simulationDays": 1,
        "agents": {"truck": {"count": 4}, "order": {"count": 8},
                   "facility": {"count": 12}},
        "earlyOrderCount": 0,
    }
    spec.update(kw)
    return spec


# --------------------------------------------------------------------------- #
# half 1 — the literal carries the key
# --------------------------------------------------------------------------- #

def test_generate_random_behaviors_carries_facility_rules():
    """The literal in `scenario_manager.generate_random_behaviors` — asserted against
    the AST so a future edit that drops a key again is caught structurally, not by a
    substring grep."""
    import ast
    import inspect

    from apps.container_logistics.scenario import scenario_manager

    src = inspect.getsource(scenario_manager.ScenarioManager.generate_random_behaviors)
    tree = ast.parse(src.lstrip().replace("\n    ", "\n"))
    literals = [
        {k.value for k in node.keys if isinstance(k, ast.Constant)}
        for node in ast.walk(tree) if isinstance(node, ast.Dict) and node.keys
    ]
    carrying = [lit for lit in literals if "simulationDays" in lit]
    assert carrying, "the override payload literal was not found — has it moved?"
    for lit in carrying:
        assert "facilityRules" in lit, (
            "the override payload dropped facilityRules again; the refusal downstream "
            "cannot fire on a value that never arrives"
        )
        assert "facilityRulesWorld" in lit


def test_normalize_generate_spec_carries_the_rules_through():
    out = normalize_generate_spec(_spec(facilityRules=deepcopy(_RULES),
                                        facilityRulesWorld=deepcopy(_WORLD)))
    assert out["facilityRules"] == _RULES
    assert out["facilityRulesWorld"] == _WORLD


# --------------------------------------------------------------------------- #
# half 2 — and now the refusal has something to refuse
# --------------------------------------------------------------------------- #

def test_legacy_generation_path_refuses_a_rules_carrying_scenario():
    normalized = normalize_generate_spec(_spec(facilityRules=deepcopy(_RULES)))
    with frontend_scenario_config_override(normalized):
        with pytest.raises(RuntimeError) as exc:
            build_generation_spec(DOMAIN)
    msg = str(exc.value)
    assert "cannot honour them" in msg
    assert "RULES-FREE" in msg
    # The message must name the escape route, or an operator's only option is to
    # delete the rules.
    assert "openride scenario compile" in msg
    assert "{'code': 'CT'}" in msg


def test_rules_less_scenario_still_generates_on_the_legacy_path():
    """13 of the 15 shipped scenarios carry no rules. The refusal must be invisible
    to every one of them."""
    normalized = normalize_generate_spec(_spec())
    with frontend_scenario_config_override(normalized):
        spec = build_generation_spec(DOMAIN)
    assert spec.num_trucks == 4
    assert spec.num_facilities == 12


@pytest.mark.parametrize("empty", [[], None])
def test_an_empty_rule_list_is_not_a_refusal(empty):
    normalized = normalize_generate_spec(_spec(facilityRules=empty))
    with frontend_scenario_config_override(normalized):
        build_generation_spec(DOMAIN)


def test_the_override_restores_the_module_attribute():
    """A leaked `FACILITY_RULES` would make the NEXT generation on this process
    refuse for a scenario that carries no rules — a cross-test failure that would
    read as flakiness."""
    before = getattr(scenario_config, "FACILITY_RULES", None)
    normalized = normalize_generate_spec(_spec(facilityRules=deepcopy(_RULES)))
    with frontend_scenario_config_override(normalized):
        assert scenario_config.FACILITY_RULES == _RULES
    assert getattr(scenario_config, "FACILITY_RULES", None) == before


# --------------------------------------------------------------------------- #
# the generalising test (§19.2 item 2) — a KEY added later is covered everywhere
# --------------------------------------------------------------------------- #

#: Keys a given literal deliberately does not carry, with the reason. An entry here
#: is a DECLARATION, not an exemption: it has to be written down and defended.
_DECLARED_UNSUPPORTED = {
    "frontend_scenario_config_override": {
        # These are authoring-surface / identity keys with no scenario_config analogue;
        # the legacy path is being retired, not extended.
        "name", "slug", "domain", "source", "seed", "referenceTime", "cooperation",
        "planner", "overrides", "orderCountUnit", "agents",
    },
}


def test_every_spec_key_survives_the_legacy_override_literal():
    """Parametrised over `SPEC_KEYS`, so a key registered next year is covered on the
    day it is registered.

    **Stated limit:** this covers every future KEY across the literals it knows about.
    A fifth *literal* added later is still uncovered, and enumerating literals is the
    shape that failed — so this narrows the class rather than closing it.
    """
    normalized_keys = set(normalize_generate_spec(_spec(
        facilityRules=deepcopy(_RULES), facilityRulesWorld=deepcopy(_WORLD),
        tripMatrix={"MT": {"CU": 1.0}}, hauliers=None, solver="GreedyNearest",
    )))
    unsupported = _DECLARED_UNSUPPORTED["frontend_scenario_config_override"]
    missing = [
        k for k in SPEC_KEYS
        if k not in normalized_keys and k not in unsupported
    ]
    assert not missing, (
        f"{missing} are registered in SPEC_KEYS but are silently dropped by "
        f"normalize_generate_spec. Either carry them, or add them to "
        f"_DECLARED_UNSUPPORTED with a reason — a silent drop is the F1/F3 defect."
    )
