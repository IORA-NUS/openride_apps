"""AgentSource with order-spawn batching for container logistics simulations."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Callable

from orsim.runtime import AgentSource


class BatchedPrecomputedAgentSource(AgentSource):
    """
    Wraps scenario_manager + agent_config like PrecomputedAgentSource, but tags
    each item with its role and caps order-agent spawns per step.
    """

    def __init__(
        self,
        scenario_manager,
        agent_config: dict,
        run_id: str,
        reference_time: datetime,
        project_path: str,
        context=None,
        order_spawn_max_per_step: int = 40,
        order_role: str = "order",
    ):
        self._registry: dict[int, list[dict]] = {}
        self._bootstrap: list[dict] = []
        self._max_step = 0
        self._current_step = -1
        self._order_spawn_max_per_step = max(1, order_spawn_max_per_step)
        self._order_role = order_role
        self._order_backlog: deque[dict] = deque()

        for role, cfg in agent_config.items():
            collection = scenario_manager.get_agent_collection(role)
            scheduler_key: str = cfg["scheduler_key"]
            agent_class: str = cfg["agent_class"]
            init_time_step_key: str | None = cfg.get("init_time_step_key")
            extra_fields_fn: Callable | None = cfg.get("extra_fields")

            for agent_id, behavior in collection.items():
                init_time_step = (
                    behavior.get("profile", {}).get(init_time_step_key, 0)
                    if init_time_step_key
                    else 0
                )
                spec = {
                    "unique_id": agent_id,
                    "run_id": run_id,
                    "reference_time": datetime.strftime(reference_time, "%Y%m%d%H%M%S"),
                    "init_time_step": init_time_step,
                    "behavior": behavior,
                }
                if extra_fields_fn:
                    spec.update(extra_fields_fn(agent_id, behavior, context))

                item = {
                    "scheduler_key": scheduler_key,
                    "spec": spec,
                    "project_path": project_path,
                    "agent_class": agent_class,
                    "role": role,
                }

                if scheduler_key == "agent":
                    step_key = spec["init_time_step"]
                    self._registry.setdefault(step_key, []).append(item)
                    self._max_step = max(self._max_step, step_key)
                else:
                    self._bootstrap.append(item)

    def bootstrap_agents(self) -> list[dict]:
        return list(self._bootstrap)

    def agents_for_step(self, step: int) -> list[dict]:
        self._current_step = step
        due = list(self._registry.get(step, []))
        if self._order_backlog:
            due = list(self._order_backlog) + due
            self._order_backlog.clear()

        orders = [item for item in due if item.get("role") == self._order_role]
        others = [item for item in due if item.get("role") != self._order_role]

        launched = list(others)
        cap = self._order_spawn_max_per_step
        launched.extend(orders[:cap])
        overflow = orders[cap:]
        if overflow:
            self._order_backlog.extend(overflow)

        return launched

    def is_exhausted(self) -> bool:
        return self._current_step >= self._max_step and not self._order_backlog
