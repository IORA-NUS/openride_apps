import pytest
from openride_apps.apps.container_logistics.assignment.app import AssignmentApp
from openride_apps.apps.container_logistics.assignment.agent import AssignmentAgent
from openride_apps.apps.container_logistics.assignment.manager import AssignmentManager

class DummySimClock:
    pass

class DummyMessenger:
    pass

class DummyPersona:
    pass

class DummyScheduler:
    pass

class DummyBehavior:
    pass

class DummyUser:
    pass

def test_assignment_app_init():
    app = AssignmentApp(run_id="run1", sim_clock=DummySimClock(), messenger=DummyMessenger(), persona=DummyPersona())
    assert isinstance(app, AssignmentApp)

def test_assignment_agent_init():
    agent = AssignmentAgent(unique_id="agent1", run_id="run1", reference_time=0, init_time_step=0, scheduler=DummyScheduler(), behavior=DummyBehavior())
    assert isinstance(agent, AssignmentAgent)

def test_assignment_manager_init():
    manager = AssignmentManager(run_id="run1", sim_clock=DummySimClock(), user=DummyUser(), persona=DummyPersona())
    assert isinstance(manager, AssignmentManager)
