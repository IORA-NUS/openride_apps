"""
Unit tests for HorizonDrainTermination.

(Relocated from orsim/tests/ — the condition now lives in the app at
apps/simulation/terminations.py so orsim stays pristine.)
"""
from apps.simulation.terminations import HorizonDrainTermination


class _Source:
    def __init__(self, exhausted: bool):
        self._exhausted = exhausted

    def is_exhausted(self) -> bool:
        return self._exhausted


class _Scheduler:
    def __init__(self, agent_ids):
        self.agent_collection = {agent_id: {} for agent_id in agent_ids}


def test_horizon_drain_continues_past_horizon_until_domain_drained():
    term = HorizonDrainTermination(10)
    source = _Source(exhausted=True)
    schedulers = {
        "agent": _Scheduler(["facility_1"]),
        "service": _Scheduler(["assignment", "analytics"]),
    }

    assert term.should_terminate(9, schedulers, source) is False
    assert term.is_final_step(9, schedulers, source) is False

    assert term.should_terminate(10, schedulers, source) is False
    assert term.is_final_step(10, schedulers, source) is False


def test_horizon_drain_final_step_clears_service_agents():
    term = HorizonDrainTermination(10)
    source = _Source(exhausted=True)
    schedulers = {
        "agent": _Scheduler([]),
        "service": _Scheduler(["assignment", "analytics"]),
    }

    assert term.should_terminate(12, schedulers, source) is False
    assert term.is_final_step(12, schedulers, source) is True


def test_drain_cap_forces_termination_with_agents_still_registered():
    """Backstop: once horizon + max_drain_steps is reached, the run must end even if the
    agent scheduler never drained (a stuck agent must not hang the sim forever)."""
    term = HorizonDrainTermination(10, max_drain_steps=5)
    source = _Source(exhausted=True)
    schedulers = {
        "agent": _Scheduler(["stuck_order_1", "stuck_order_2"]),  # never drains
        "service": _Scheduler(["assignment", "analytics"]),
    }

    # Before the cap: still draining, not yet forced.
    assert term._ready_for_final_shutdown(14, schedulers, source) is False
    assert term.is_final_step(14, schedulers, source) is False
    assert term.should_terminate(14, schedulers, source) is False

    # At the cap (step >= 10 + 5): force the final-shutdown sequence despite live agents.
    assert term._ready_for_final_shutdown(15, schedulers, source) is True
    assert term.is_final_step(15, schedulers, source) is True   # service still has agents
    assert term.should_terminate(15, schedulers, source) is False

    # After the final shutdown step clears service agents, the run ends.
    schedulers["service"] = _Scheduler([])
    assert term.should_terminate(16, schedulers, source) is True


def test_no_cap_means_pure_drain_to_zero():
    """max_drain_steps=None (default) keeps the original drain-to-zero behavior — no forced end."""
    term = HorizonDrainTermination(10)
    source = _Source(exhausted=True)
    schedulers = {"agent": _Scheduler(["facility_1"]), "service": _Scheduler([])}
    assert term.should_terminate(9999, schedulers, source) is False


def test_horizon_drain_terminates_after_service_shutdown():
    term = HorizonDrainTermination(10)
    source = _Source(exhausted=True)
    schedulers = {
        "agent": _Scheduler([]),
        "service": _Scheduler([]),
    }

    assert term.is_final_step(15, schedulers, source) is False
    assert term.should_terminate(15, schedulers, source) is True
