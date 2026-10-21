"""Locks the single policy catalog (plan §4 P0.8; round 2 §5 C1).

``policy_catalog()`` and ``component_catalog()`` are the only sources the CLI and
dashboard read slot, policy and component lists from, so they must declare every slot
and component kind, be JSON-serialisable, carry a schema and defaults for every policy
and component, and read the component names live rather than from a copy. Dispatch
policies here are dummies registered under ``isolated_registry``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from apps.container_logistics.policies.framework import SLOTS, PolicyParams, param, policy
from apps.container_logistics.policies.framework.catalog import component_catalog, policy_catalog
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan


class _TestPolicyBase(DispatchPolicy):
    """Dummy dispatch policies subclass this: the dispatch slot requires DispatchPolicy."""

    def decide(self, ctx):
        return Plan(chains={})


EXPECTED_SLOTS = {
    "dispatch": ("operator", "implemented"),
    "idle": ("operator", "planned"),
    "queue": ("operator", "planned"),
    "world.shedding": ("world", "planned"),
    "world.duration": ("world", "planned"),
}


def _register_dummies():
    @dataclass(frozen=True)
    class Params(PolicyParams):
        candidates: str = param("spatial", choices=("spatial", "sampled"), help="Candidates.")
        bonus_km: float = param(5, ge=0, help="Bonus.")

    @dataclass(frozen=True)
    class NoParams(PolicyParams):
        pass

    policy(slot="dispatch", information_model="central", name="TestCatalogA", version="3", description="Dummy A.",
           order_sensitive="shuffles on purpose",
           arm_budgets_ms={"candidates=sampled": 900})(type("TestCatalogA", (_TestPolicyBase,), {"Params": Params}))
    policy(slot="dispatch", information_model="central", name="TestCatalogB", version="1", description="Dummy B.",
           requires=("definitely_not_a_module_xyz",))(type("TestCatalogB", (_TestPolicyBase,), {"Params": NoParams}))


def test_all_five_slots_declared_with_kind_and_status():
    """The table and the catalog both hold exactly the five slots, in table order.

    Verified to have teeth: renaming the ``world.duration`` row in ``slots._TABLE``
    failed the equality; flipping ``world.shedding`` to kind 'operator' failed it too.
    """
    assert {n: (s.kind, s.status) for n, s in SLOTS.items()} == EXPECTED_SLOTS
    catalog = policy_catalog()
    assert list(catalog) == list(SLOTS), "catalog must keep SLOTS order"
    assert {n: (e["kind"], e["status"]) for n, e in catalog.items()} == EXPECTED_SLOTS
    dispatch = catalog["dispatch"]
    assert dispatch["default"] == "Partitioned", "round 2: the slot default is the decomposition"
    assert dispatch["timings"] == ["online", "plan"] and dispatch["executable_timings"] == ["online"]
    for name in ("idle", "queue", "world.shedding", "world.duration"):
        assert catalog[name] == {"kind": EXPECTED_SLOTS[name][0], "status": "planned", "lands_in": "P5"}


def test_components_list_three_kinds_with_live_names_schema_and_defaults():
    """``component_catalog()`` holds the three market component kinds, names read live.

    Verified to have teeth: caching a ``dict(...)`` snapshot of the registry in
    ``components.live_registry`` failed the live-mutation assertion; renaming the
    ``market.arbitration`` kind in ``components._KINDS`` failed the kind equality.
    """
    from apps.container_logistics.assignment.arbitration import ARBITRATION_REGISTRY
    from apps.container_logistics.assignment.policy.claim import CLAIM_REGISTRY
    from apps.container_logistics.assignment.policy.offer import OFFER_REGISTRY

    components = component_catalog()
    assert list(components) == ["market.offer", "market.claim", "market.arbitration"]
    assert [components[k]["default"] for k in components] == ["OfferAll", "ClaimAllPlanned", "LowestCost"]
    for kind, registry in (("market.offer", OFFER_REGISTRY), ("market.claim", CLAIM_REGISTRY),
                           ("market.arbitration", ARBITRATION_REGISTRY)):
        entry = components[kind]
        assert entry["description"], kind
        assert [p["name"] for p in entry["policies"]] == sorted(registry), kind
        for p in entry["policies"]:
            assert set(p) == {"name", "version", "description", "params_schema", "defaults"}, p
            assert p["version"] == "1" and p["description"], p["name"]
    spare = next(p for p in components["market.offer"]["policies"] if p["name"] == "OfferSpare")
    assert spare["defaults"] == {"keep_below_km": None, "only_when_short": True}
    assert spare["params_schema"]["properties"]["keep_below_km"]["type"] == ["number", "null"]
    assert json.loads(json.dumps(components)) == components
    OFFER_REGISTRY["TestLiveOffer"] = OFFER_REGISTRY["OfferAll"]
    try:
        assert "TestLiveOffer" in [p["name"] for p in component_catalog()["market.offer"]["policies"]]
    finally:
        del OFFER_REGISTRY["TestLiveOffer"]


def test_catalog_is_json_serialisable_and_has_schema_per_dispatch_policy():
    """Every dispatch policy entry carries schema, validated defaults and availability.

    Verified to have teeth: emitting ``spec.params_cls`` instead of its schema broke
    ``json.dumps``; computing ``available`` as ``True`` failed the TestCatalogB assertion.
    """
    with isolated_registry():
        _register_dummies()
        catalog = policy_catalog()
    roundtrip = json.loads(json.dumps(catalog))
    assert roundtrip == catalog
    entries = {p["name"]: p for p in catalog["dispatch"]["policies"]}
    a, b = entries["TestCatalogA"], entries["TestCatalogB"]
    assert a == {
        "name": "TestCatalogA", "version": "3", "description": "Dummy A.",
        "requires": [], "missing": [], "available": True,
        "information_model": "central", "composite": False, "inner_param": None,
        "share_tag_format": "audited",
        "module_rng": None, "order_sensitive": "shuffles on purpose",
        "arm_budgets_ms": {"candidates=sampled": 900},
        "params_schema": a["params_schema"],
        "defaults": {"candidates": "spatial", "bonus_km": 5.0},
    }
    assert list(a["params_schema"]["properties"]) == ["candidates", "bonus_km"]
    assert a["params_schema"]["additionalProperties"] is False
    assert b["missing"] == ["definitely_not_a_module_xyz"] and b["available"] is False
    assert b["params_schema"]["properties"] == {} and b["defaults"] == {}
    names = [p["name"] for p in catalog["dispatch"]["policies"]]
    assert names == sorted(names), "policies are listed by name"
    for entry in catalog["dispatch"]["policies"]:
        assert {"params_schema", "defaults"} <= set(entry)


def test_known_policies_command_keeps_datagen_keys_and_adds_slots_and_components():
    """``known-policies`` keeps its datagen payload untouched and gains ``slots`` and
    ``components``.

    The frontend and ``openride scenario`` read ``policies``/``defaults`` as datagen
    role policies (plan §1.2 item 1), so they must survive verbatim.
    Verified to have teeth: dropping ``**known_policies()`` from ``cmd_known_policies``
    failed the datagen-keys assertion; dropping ``"slots"`` failed the catalog one;
    dropping ``"components"`` failed the component one.
    """
    from apps.container_logistics.scenario.frontend_scenario_spec import known_policies
    from openride_control.scenario import cmd_known_policies

    payload = cmd_known_policies()
    assert payload["ok"] is True and payload["action"] == "known_policies"
    assert payload["policies"] == known_policies()["policies"]
    assert payload["defaults"] == known_policies()["defaults"]
    assert set(payload["policies"]) == {"truck", "order", "facility"}
    assert payload["slots"] == policy_catalog()
    assert payload["components"] == component_catalog()
    assert len(payload["slots"]) == 5 and len(payload["components"]) == 3
    json.dumps(payload)


def test_known_solvers_is_derived_from_the_registry():
    """``known_solvers()`` lists exactly the registered dispatch names, read live.

    Verified to have teeth: hardcoding ``["GreedyNearest", "RandomAssignment"]`` as
    ``known_solvers()["solvers"]`` failed the live-registration assertion.
    """
    from apps.container_logistics.policies.framework import list_policies
    from apps.container_logistics.scenario import scenario_overrides
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        known_solvers,
        normalize_solver,
    )

    info = known_solvers()
    assert info == {"solvers": [p.name for p in list_policies("dispatch")],
                    "default": "RandomAssignment"}
    with isolated_registry():
        _register_dummies()
        assert "TestCatalogA" in known_solvers()["solvers"]
        assert "TestCatalogA" in scenario_overrides.known_solvers()
        assert normalize_solver("TestCatalogA") == "TestCatalogA"
    assert "TestCatalogA" not in known_solvers()["solvers"]
    assert normalize_solver("TestCatalogA") == "RandomAssignment"


def test_legacy_solver_registry_agrees_with_policy_registry():
    """The frozen ``SOLVER_REGISTRY`` view maps each name to its registered class, and
    ``DEFAULT_SOLVER`` is the legacy LEAF default (the slot default is a decomposition).

    Verified to have teeth: swapping the two classes in ``SOLVER_REGISTRY`` failed
    the class identity assertion.
    """
    from apps.container_logistics.assignment.solver import DEFAULT_SOLVER, SOLVER_REGISTRY
    from apps.container_logistics.policies.dispatch.legacy import LEGACY_LEAF_DEFAULT
    from apps.container_logistics.policies.framework import get_policy_spec

    for name, cls in SOLVER_REGISTRY.items():
        assert get_policy_spec("dispatch", name).cls is cls, name
    assert DEFAULT_SOLVER == LEGACY_LEAF_DEFAULT == "RandomAssignment"
    assert DEFAULT_SOLVER in SOLVER_REGISTRY
