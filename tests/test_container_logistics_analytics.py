import pytest
from openride_apps.apps.container_logistics.analytics.app import AnalyticsApp
from openride_apps.apps.container_logistics.analytics.agent import AnalyticsAgent
from openride_apps.apps.container_logistics.analytics.manager import AnalyticsManager

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

def test_analytics_app_init():
    app = AnalyticsApp(run_id="run1", sim_clock=DummySimClock(), messenger=DummyMessenger(), persona=DummyPersona())
    assert isinstance(app, AnalyticsApp)

def test_analytics_agent_init():
    agent = AnalyticsAgent(unique_id="agent1", run_id="run1", reference_time=0, init_time_step=0, scheduler=DummyScheduler(), behavior=DummyBehavior())
    assert isinstance(agent, AnalyticsAgent)

def test_analytics_manager_init():
    manager = AnalyticsManager(run_id="run1", sim_clock=DummySimClock(), user="user1", persona=DummyPersona())
    assert isinstance(manager, AnalyticsManager)
