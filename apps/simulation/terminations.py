"""
App-side termination conditions for container_logistics runs.

``HorizonDrainTermination`` lives here rather than in ``orsim/runtime/termination.py``
so the orsim library stays pristine (CLAUDE.md §7). It only depends on orsim's public
``TerminationCondition`` base and is consumed by ``ORSimRuntime`` via duck typing
(``is_final_step`` / ``should_terminate``), so no orsim change is needed.
"""

from orsim.runtime import TerminationCondition


class HorizonDrainTermination(TerminationCondition):
    """
    Run the configured horizon, then keep stepping until domain agents finish in-flight work.

    After ``horizon_steps`` and once the ``AgentSource`` is exhausted, the simulation
    continues until the agent scheduler has no registered agents (trucks, orders, and
    facilities shut down naturally). One final shutdown step clears persistent service
    agents (assignment, analytics, etc.).
    """

    def __init__(
        self,
        horizon_steps: int,
        *,
        agent_scheduler_key: str = "agent",
        max_drain_steps: int | None = None,
    ):
        self.horizon_steps = horizon_steps
        self.agent_scheduler_key = agent_scheduler_key
        # Hard cap on how far past the horizon the drain may run. Once reached, the run is
        # forced to terminate regardless of remaining agents — a backstop so a stuck agent
        # (e.g. an order that never terminalizes) can never hang the sim indefinitely.
        # None disables the cap (pure drain-to-zero).
        self.max_drain_steps = max_drain_steps

    def _past_horizon(self, step: int) -> bool:
        return step >= self.horizon_steps

    def _drain_cap_reached(self, step: int) -> bool:
        return (
            self.max_drain_steps is not None
            and step >= self.horizon_steps + self.max_drain_steps
        )

    @staticmethod
    def _awake_agent_count(scheduler) -> int:
        # Agents in an announced nap (sleep protocol, see OpenRideScheduler) are
        # pure event listeners with nothing left to do on their own — typically
        # never-assigned orders whose records the finalize bulk-cancel
        # terminalizes anyway. They must not hold the drain open; the final
        # shutdown step direct-notifies and deregisters them.
        return len(scheduler.agent_collection) - int(
            getattr(scheduler, "sleeping_agent_count", 0)
        )

    def _agent_scheduler_drained(self, schedulers: dict) -> bool:
        agent_sched = schedulers.get(self.agent_scheduler_key)
        if agent_sched is None:
            return all(self._awake_agent_count(s) <= 0 for s in schedulers.values())
        return self._awake_agent_count(agent_sched) <= 0

    def _service_scheduler_drained(self, schedulers: dict) -> bool:
        service_sched = schedulers.get("service")
        if service_sched is None:
            return True
        return len(service_sched.agent_collection) == 0

    def _ready_for_final_shutdown(self, step: int, schedulers: dict, source) -> bool:
        # Backstop: once the drain cap is hit, force the final-shutdown sequence even if agents
        # remain — the final shutdown step (is_final=True) reaps them and the run ends.
        if self._drain_cap_reached(step):
            return True
        return (
            self._past_horizon(step)
            and source.is_exhausted()
            and self._agent_scheduler_drained(schedulers)
        )

    def should_terminate(self, step: int, schedulers: dict, source) -> bool:
        if not self._ready_for_final_shutdown(step, schedulers, source):
            return False
        return self._service_scheduler_drained(schedulers)

    def is_final_step(self, step: int, schedulers: dict, source) -> bool:
        if not self._ready_for_final_shutdown(step, schedulers, source):
            return False
        return not self._service_scheduler_drained(schedulers)
