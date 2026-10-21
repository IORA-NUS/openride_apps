"""Locks the single policy catalog (plan §4 P0.8).

``policy_catalog()`` is the only source the CLI and dashboard read slot and policy
lists from, so it must declare every slot, be JSON-serialisable, carry a schema and
defaults for every dispatch policy, and read the legacy market names live rather than
from a copy. Dispatch policies here are dummies registered under ``isolated_registry``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from apps.container_logistics.policies.framework import SLOTS, PolicyParams, param, policy
from apps.container_logistics.policies.framework.catalog import policy_catalog
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan


class _TestPolicyBase(DispatchPolicy):
    """Dummy dispatch policies subclass this: the dispatch slot requires DispatchPolicy."""

    def decide(self, ctx):
        return Plan(chains={})


EXPECTED_SLOTS = {
    "dispatch": ("operator", "implemented"),
    "market.offer": ("operator", "legacy"),
    "market.claim": ("operator", "legacy"),
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

    policy(slot="dispatch", name="TestCatalogA", version="3", description="Dummy A.",
           order_sensitive="shuffles on purpose")(type("TestCatalogA", (_TestPolicyBase,), {"Params": Params}))
    policy(slot="dispatch", name="TestCatalogB", version="1", description="Dummy B.",
           requires=("definitely_not_a_module_xyz",))(type("TestCatalogB", (_TestPolicyBase,), {"Params": NoParams}))


def test_all_seven_slots_declared_with_kind_and_status():
    """The table and the catalog both hold exactly the seven slots, in table order.

    Verified to have teeth: renaming the ``world.duration`` row in ``slots._TABLE``
    failed the equality; flipping ``world.shedding`` to kind 'operator' failed it too.
    """
    assert {n: (s.kind, s.status) for n, s in SLOTS.items()} == EXPECTED_SLOTS
    catalog = policy_catalog()
    assert list(catalog) == list(SLOTS), "catalog must keep SLOTS order"
    assert {n: (e["kind"], e["status"]) for n, e in catalog.items()} == EXPECTED_SLOTS
    dispatch = catalog["dispatch"]
    assert dispatch["default"] == "RandomAssignment"
    assert dispatch["timings"] == ["online", "plan"] and dispatch["executable_timings"] == ["online"]
    for name in ("idle", "queue", "world.shedding", "world.duration"):
        assert catalog[name] == {"kind": EXPECTED_SLOTS[name][0], "status": "planned", "lands_in": "P5"}


def test_legacy_slots_list_live_registry_names_and_config_key():
    """Legacy names come from ``OFFER_REGISTRY``/``CLAIM_REGISTRY`` at call time.

    Verified to have teeth: hardcoding ``["OfferAll"]`` in ``_legacy_names`` failed
    both the equality and the live-mutation assertion.
    """
    from apps.container_logistics.assignment.policy.claim import CLAIM_REGISTRY
    from apps.container_logistics.assignment.policy.offer import OFFER_REGISTRY

    catalog = policy_catalog()
    assert catalog["market.offer"] == {
        "kind": "operator", "status": "legacy", "default": "OfferAll",
        "names": sorted(OFFER_REGISTRY), "configured_via": "planner.market.offer",
    }
    assert catalog["market.claim"]["names"] == sorted(CLAIM_REGISTRY)
    assert catalog["market.claim"]["configured_via"] == "planner.market.claim"
    OFFER_REGISTRY["TestLiveOffer"] = OFFER_REGISTRY["OfferAll"]
    try:
        assert "TestLiveOffer" in policy_catalog()["market.offer"]["names"]
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
        "order_sensitive": "shuffles on purpose",
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


def test_known_policies_command_keeps_datagen_keys_and_adds_slots():
    """``known-policies`` keeps its datagen payload untouched and gains ``slots``.

    The frontend and ``openride scenario`` read ``policies``/``defaults`` as datagen
    role policies (plan §1.2 item 1), so they must survive verbatim.
    Verified to have teeth: dropping ``**known_policies()`` from ``cmd_known_policies``
    failed the datagen-keys assertion; dropping ``"slots"`` failed the catalog one.
    """
    from apps.container_logistics.scenario.frontend_scenario_spec import known_policies
    from openride_control.scenario import cmd_known_policies

    payload = cmd_known_policies()
    assert payload["ok"] is True and payload["action"] == "known_policies"
    assert payload["policies"] == known_policies()["policies"]
    assert payload["defaults"] == known_policies()["defaults"]
    assert set(payload["policies"]) == {"truck", "order", "facility"}
    assert payload["slots"] == policy_catalog()
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
    """The frozen ``SOLVER_REGISTRY`` view maps each name to its registered class.

    Verified to have teeth: swapping the two classes in ``SOLVER_REGISTRY`` failed
    the class identity assertion.
    """
    from apps.container_logistics.assignment.solver import DEFAULT_SOLVER, SOLVER_REGISTRY
    from apps.container_logistics.policies.framework import get_policy_spec

    for name, cls in SOLVER_REGISTRY.items():
        assert get_policy_spec("dispatch", name).cls is cls, name
    assert DEFAULT_SOLVER == SLOTS["dispatch"].default
    assert DEFAULT_SOLVER in SOLVER_REGISTRY
