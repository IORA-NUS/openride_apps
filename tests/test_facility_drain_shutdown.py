"""Tests for post-horizon facility drain shutdown."""

from __future__ import annotations

from apps.container_logistics.facility.app import FacilityApp
from apps.container_logistics.facility.agent import FacilityAgent
from apps.container_logistics.statemachine import FacilityVisitType


class _QueueController:
    def __init__(self, queue=None, active_ids=None):
        self.queue = list(queue or [])
        self._active_ids = set(active_ids or [])

    def active_truck_ids(self):
        return set(self._active_ids)


class _Manager:
    def __init__(self, controller):
        self.queue_controller = controller


def test_has_pending_gate_work_detects_queue_and_gate_service():
    app = FacilityApp.__new__(FacilityApp)
    app.message_queue = None
    app._gate_service_ends = {}
    app.manager = _Manager(_QueueController())
    assert app.has_pending_gate_work() is False

    app.manager.queue_controller.queue.append("truck_1")
    assert app.has_pending_gate_work() is True

    app.manager.queue_controller.queue.clear()
    app.manager.queue_controller._active_ids.add("truck_1")
    assert app.has_pending_gate_work() is True

    app.manager.queue_controller._active_ids.clear()
    app._gate_service_ends = {0: "pending"}
    assert app.has_pending_gate_work() is True


def test_facility_agent_shuts_down_after_horizon_when_idle(monkeypatch):
    agent = FacilityAgent.__new__(FacilityAgent)
    agent.orsim_settings = {"SIMULATION_LENGTH_IN_STEPS": 100}
    agent.current_time_step = 100
    agent.app = FacilityApp.__new__(FacilityApp)
    agent.app.message_queue = None
    agent.app._gate_service_ends = {}
    agent.app.manager = _Manager(_QueueController())

    shutdown_calls = []

    def _shutdown():
        shutdown_calls.append(True)

    monkeypatch.setattr(agent, "shutdown", _shutdown)
    assert agent.exiting_market() is True
    assert shutdown_calls == [True]


def test_facility_agent_stays_active_while_queue_has_trucks(monkeypatch):
    agent = FacilityAgent.__new__(FacilityAgent)
    agent.orsim_settings = {"SIMULATION_LENGTH_IN_STEPS": 100}
    agent.current_time_step = 120
    agent.app = FacilityApp.__new__(FacilityApp)
    agent.app.message_queue = None
    agent.app._gate_service_ends = {}
    agent.app.manager = _Manager(
        _QueueController(queue=[("truck_1", FacilityVisitType.PICKUP)])
    )

    monkeypatch.setattr(agent, "shutdown", lambda: (_ for _ in ()).throw(AssertionError("unexpected shutdown")))
    assert agent.exiting_market() is False
