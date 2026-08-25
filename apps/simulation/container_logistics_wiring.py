"""Shared wiring for container logistics simulation entrypoints."""

import os
from datetime import datetime

from apps.config import simulation_domains
from apps.container_logistics.statemachine import (
    GateStateMachine,
    HaulTripStateMachine,
    OrderStateMachine,
)
from orsim.utils import WorkflowStateMachine

from apps.simulation.batched_agent_source import BatchedPrecomputedAgentSource
from apps.simulation.terminations import HorizonDrainTermination


def get_datahub_dir():
    """Absolute path to the run/output tree, created on first use.

    ``datahub/`` is generated output (816 MB on the development box) and is
    correctly gitignored -- so it is absent from every fresh clone. It used to
    be *required* to pre-exist, which made `scenario compile` fail on a new
    machine with a message about an absolute path that told the reader nothing
    about the real fix. It is pure output, so creating it is always safe.

    The old ``isabs`` half of that guard could never fire: ``abspath`` returns
    an absolute path by construction.
    """
    datahub_dir = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "datahub")
    )
    os.makedirs(datahub_dir, exist_ok=True)
    if not os.path.isdir(datahub_dir):
        raise ValueError(f"datahub_dir exists but is not a directory. Got: {datahub_dir}")
    return datahub_dir


def get_domain():
    return simulation_domains["container_logistics"]


def get_statemachine_collection():
    return {
        "WorkflowStateMachine": WorkflowStateMachine,
        HaulTripStateMachine.__name__: HaulTripStateMachine,
        GateStateMachine.__name__: GateStateMachine,
        OrderStateMachine.__name__: OrderStateMachine,
    }


# Synthesized when a compiled bundle predates the order_lifecycle role (every existing
# scenario). Must stay byte-identical to what datagen's OrderLifecycleAgent generates —
# pinned by a drift test.
DEFAULT_ORDER_LIFECYCLE_BEHAVIOR = {
    "email": "order_lifecycle_main@test.com",
    "password": "password",
    "persona": {"role": "order_lifecycle", "domain": get_domain()},
    "steps_per_action": 1,
    "response_rate": 1.0,
    "step_only_on_events": False,
    "profile": {"haulier_filter": None, "sweep_interval_steps": 30},
}


def ensure_order_lifecycle_behavior(scenario_manager):
    """Inject the default order_lifecycle behavior when the bundle carries none.

    Requiring a recompile of every existing scenario to run in ``service`` mode would
    violate the "old bundles keep working" precedent, so the behavior is synthesized at run
    load (like ``apply_long_run_orsim_settings`` re-applies host knobs). A bundle-provided
    behavior always wins; this never double-injects.
    """
    from copy import deepcopy

    collection = scenario_manager.get_agent_collection("order_lifecycle")
    if collection:
        return collection
    collection = {"order_lifecycle_main": deepcopy(DEFAULT_ORDER_LIFECYCLE_BEHAVIOR)}
    # ``get_agent_collection`` reads ``scenario_manager.collections``; the CL ScenarioManager
    # additionally mirrors each role onto a ``*_collection`` attribute, so keep both in sync.
    scenario_manager.collections["order_lifecycle"] = collection
    if hasattr(scenario_manager, "order_lifecycle_collection"):
        scenario_manager.order_lifecycle_collection = collection
    return collection


def get_agent_config(order_lifecycle_mode: str = "agents"):
    if order_lifecycle_mode == "service":
        # Orders are pure data in this mode: no per-order agents are registered at all, and
        # one order_lifecycle service agent owns the whole order population.
        config = {k: v for k, v in _agent_config_agents_mode().items() if k != "order"}
        config["order_lifecycle"] = {
            "scheduler_key": "service",
            "agent_class": "apps.container_logistics.order_lifecycle.OrderLifecycleAgent",
            "init_time_step_key": None,
            "extra_fields": lambda agent_id, behavior, sim: {},
        }
        return config
    return _agent_config_agents_mode()


def _agent_config_agents_mode():
    return {
        "truck": {
            "scheduler_key": "agent",
            "agent_class": "apps.container_logistics.truck.TruckAgent",
            "init_time_step_key": None,
            "extra_fields": lambda agent_id, behavior, sim: {
                "init_time_step": behavior.get("shift_start_time", 0),
            },
        },
        "order": {
            "scheduler_key": "agent",
            "agent_class": "apps.container_logistics.order.OrderAgent",
            "init_time_step_key": None,
            "extra_fields": lambda agent_id, behavior, sim: {
                "init_time_step": behavior.get("request_time_step", 0),
            },
        },
        "facility": {
            "scheduler_key": "agent",
            "agent_class": "apps.container_logistics.facility.FacilityAgent",
            "init_time_step_key": None,
            "extra_fields": lambda agent_id, behavior, sim: {},
        },
        "assignment": {
            "scheduler_key": "service",
            "agent_class": "apps.container_logistics.assignment.AssignmentAgent",
            "init_time_step_key": None,
            "extra_fields": lambda agent_id, behavior, sim: {},
        },
        "analytics": {
            "scheduler_key": "service",
            "agent_class": "apps.container_logistics.analytics.AnalyticsAgentIndie",
            "init_time_step_key": None,
            "extra_fields": lambda agent_id, behavior, sim: {"datahub_dir": sim.datahub_dir},
        },
    }


def build_agent_source(scenario_manager, agent_config, run_id, reference_time, project_path, context):
    order_spawn_max = max(
        1, int(scenario_manager.orsim_settings.get("ORDER_SPAWN_MAX_PER_STEP", 40))
    )
    return BatchedPrecomputedAgentSource(
        scenario_manager=scenario_manager,
        agent_config=agent_config,
        run_id=run_id,
        reference_time=reference_time,
        project_path=project_path,
        context=context,
        order_spawn_max_per_step=order_spawn_max,
    )


def build_termination_condition(orsim_settings):
    horizon = orsim_settings["SIMULATION_LENGTH_IN_STEPS"]
    # Cap the post-horizon drain so a stuck agent can never hang the run (see CLAUDE.md §6.4).
    # Keep it >= POST_HORIZON_GRACE_STEPS so orders self-cancel cleanly before the cap fires.
    max_drain_steps = int(orsim_settings.get("POST_HORIZON_DRAIN_MAX_STEPS", 90))
    return HorizonDrainTermination(horizon, max_drain_steps=max_drain_steps)


def build_scheduler_config(run_id, orsim_settings):
    return {
        "agent": {
            "run_id": run_id,
            "scheduler_id": "agent_scheduler",
            "orsim_settings": orsim_settings,
        },
        "service": {
            "run_id": run_id,
            "scheduler_id": "service_scheduler",
            "orsim_settings": orsim_settings,
            "init_failure_handler": "hard",
        },
    }


def kafka_progress_listener(sim, step_index, total_steps):
    from apps.utils import kafka_utils

    run_status_topic = kafka_utils.resolve_topic("run_status")
    kafka_utils.push_run_status(
        run_status_topic,
        sim.run_id,
        "RUNNING",
        msg=f"step {step_index}/{total_steps}",
    )
    kafka_utils.flush_producer(2)


def new_run_id(prefix="run"):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
