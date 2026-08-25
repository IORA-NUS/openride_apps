"""WP4 wiring: the flag-gated agent config, behavior synthesis, and agent-source shape.

The load-bearing invariant here is that with the flag off (or absent) NOTHING changes: the
agents-mode config is the exact five-role dict it has always been, and an agent source built
from it registers every order agent exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from apps.simulation.batched_agent_source import BatchedPrecomputedAgentSource
from apps.simulation.container_logistics_wiring import (
    DEFAULT_ORDER_LIFECYCLE_BEHAVIOR,
    ensure_order_lifecycle_behavior,
    get_agent_config,
)

REFERENCE_TIME = datetime(2026, 7, 29, 0, 0, 0)

AGENTS_MODE_ROLES = {
    "truck": "apps.container_logistics.truck.TruckAgent",
    "order": "apps.container_logistics.order.OrderAgent",
    "facility": "apps.container_logistics.facility.FacilityAgent",
    "assignment": "apps.container_logistics.assignment.AssignmentAgent",
    "analytics": "apps.container_logistics.analytics.AnalyticsAgentIndie",
}
AGENTS_MODE_SCHEDULERS = {
    "truck": "agent",
    "order": "agent",
    "facility": "agent",
    "assignment": "service",
    "analytics": "service",
}


def _comparable(config):
    """Drop the per-call ``extra_fields`` lambdas so two configs can be compared."""
    return {
        role: {k: v for k, v in cfg.items() if k != "extra_fields"}
        for role, cfg in config.items()
    }


class FakeScenarioManager:
    def __init__(self, collections):
        self.collections = dict(collections)

    def get_agent_collection(self, key):
        return self.collections.get(key, {})


def _behaviors(prefix, n, **extra):
    return {
        f"{prefix}_{i}": {"email": f"{prefix}_{i}@test.com", "password": "password", **extra}
        for i in range(n)
    }


# --------------------------------------------------------------------------- agent config


def test_default_config_is_the_unchanged_five_role_agents_mode_config():
    default = _comparable(get_agent_config())
    assert default == _comparable(get_agent_config("agents"))
    assert set(default) == set(AGENTS_MODE_ROLES)
    for role, cls in AGENTS_MODE_ROLES.items():
        assert default[role]["agent_class"] == cls
        assert default[role]["scheduler_key"] == AGENTS_MODE_SCHEDULERS[role]
        assert default[role]["init_time_step_key"] is None
    # The order role keeps its demand-curve init_time_step hook.
    assert get_agent_config()["order"]["extra_fields"]("o1", {"request_time_step": 42}, None) == {
        "init_time_step": 42
    }


def test_service_config_drops_orders_and_adds_the_lifecycle_service_role():
    service = _comparable(get_agent_config("service"))
    assert "order" not in service
    assert set(service) == {"truck", "facility", "assignment", "analytics", "order_lifecycle"}
    assert service["order_lifecycle"] == {
        "scheduler_key": "service",
        "agent_class": "apps.container_logistics.order_lifecycle.OrderLifecycleAgent",
        "init_time_step_key": None,
    }
    # Everything else is untouched.
    for role in ("truck", "facility", "assignment", "analytics"):
        assert service[role] == _comparable(get_agent_config("agents"))[role]


def test_unknown_mode_is_not_silently_treated_as_service():
    assert "order" in get_agent_config("nonsense")


# --------------------------------------------------------------------------- agent source


def _source(mode, scenario_manager):
    return BatchedPrecomputedAgentSource(
        scenario_manager=scenario_manager,
        agent_config=get_agent_config(mode),
        run_id="run_test",
        reference_time=REFERENCE_TIME,
        project_path="/tmp",
        context=SimpleNamespace(datahub_dir="/tmp"),
    )


def _collections():
    return {
        "truck": _behaviors("truck", 3, shift_start_time=0),
        "order": _behaviors("order", 5, request_time_step=0),
        "facility": _behaviors("facility", 2),
        "assignment": _behaviors("assignment", 1),
        "analytics": _behaviors("analytics", 1),
        "order_lifecycle": {"order_lifecycle_main": dict(DEFAULT_ORDER_LIFECYCLE_BEHAVIOR)},
    }


def test_agents_mode_source_still_registers_every_order_agent():
    source = _source("agents", FakeScenarioManager(_collections()))
    step0 = source.agents_for_step(0)
    assert sum(1 for i in step0 if i["role"] == "order") == 5
    assert {i["role"] for i in source.bootstrap_agents()} == {"assignment", "analytics"}


def test_service_mode_source_registers_zero_orders_and_bootstraps_the_lifecycle_agent():
    source = _source("service", FakeScenarioManager(_collections()))
    for step in range(3):
        assert [i for i in source.agents_for_step(step) if i["role"] == "order"] == []
    bootstrap = source.bootstrap_agents()
    assert {i["role"] for i in bootstrap} == {"assignment", "analytics", "order_lifecycle"}
    lifecycle = next(i for i in bootstrap if i["role"] == "order_lifecycle")
    assert lifecycle["scheduler_key"] == "service"
    assert lifecycle["agent_class"] == "apps.container_logistics.order_lifecycle.OrderLifecycleAgent"
    # Trucks/facilities are untouched by the mode switch.
    assert sum(1 for i in source.agents_for_step(0) if i["role"] == "truck") == 3


# --------------------------------------------------------------------------- behavior synthesis


def test_ensure_injects_the_default_behavior_when_the_bundle_has_none():
    sm = FakeScenarioManager({"truck": {}, "order": {}})
    collection = ensure_order_lifecycle_behavior(sm)

    assert set(collection) == {"order_lifecycle_main"}
    assert collection["order_lifecycle_main"] == DEFAULT_ORDER_LIFECYCLE_BEHAVIOR
    assert sm.get_agent_collection("order_lifecycle") == collection
    # A deep copy: mutating the injected behavior must not poison the module constant.
    collection["order_lifecycle_main"]["profile"]["sweep_interval_steps"] = 999
    assert DEFAULT_ORDER_LIFECYCLE_BEHAVIOR["profile"]["sweep_interval_steps"] == 30


def test_ensure_preserves_a_bundle_provided_behavior_and_never_double_injects():
    authored = {"order_lifecycle_main": {"email": "custom@test.com", "profile": {"sweep_interval_steps": 7}}}
    sm = FakeScenarioManager({"order_lifecycle": authored})

    assert ensure_order_lifecycle_behavior(sm) == authored

    sm2 = FakeScenarioManager({})
    first = ensure_order_lifecycle_behavior(sm2)
    first["order_lifecycle_main"]["marker"] = True
    second = ensure_order_lifecycle_behavior(sm2)
    assert second is first
    assert len(second) == 1


def test_ensure_syncs_the_scenario_manager_role_attribute_when_present():
    sm = FakeScenarioManager({})
    sm.order_lifecycle_collection = {}
    ensure_order_lifecycle_behavior(sm)
    assert sm.order_lifecycle_collection == sm.collections["order_lifecycle"]


# --------------------------------------------------------------------------- drift pin


def test_default_behavior_matches_what_datagen_generates():
    from apps.container_logistics.datagen.agents.engine import (
        OrderLifecycleAgent as DatagenOrderLifecycleAgent,
    )

    agent = DatagenOrderLifecycleAgent.__new__(DatagenOrderLifecycleAgent)
    agent.spec = SimpleNamespace(domain=DEFAULT_ORDER_LIFECYCLE_BEHAVIOR["persona"]["domain"])

    generated = agent.generate(1)

    assert set(generated) == {"order_lifecycle_main"}
    assert generated["order_lifecycle_main"] == DEFAULT_ORDER_LIFECYCLE_BEHAVIOR
