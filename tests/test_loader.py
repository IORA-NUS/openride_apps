import pytest
from apps.agent_core.config.loader import load_agent_config


def test_load_agent_config_default():
    config = load_agent_config()
    assert isinstance(config, dict)
    assert 'runtime' in config
    assert 'app' in config
    assert 'manager' in config
    assert isinstance(config['runtime'], dict)
    assert isinstance(config['app'], dict)
    assert isinstance(config['manager'], dict)


def test_agent_factory_integration():
    # Dummy classes for test (replace with real ones in actual tests)
    class DummyRuntime:
        def __init__(self, app, manager=None, interaction_plugin=None, **kwargs):
            self.app = app
            self.manager = manager
            self.interaction_plugin = interaction_plugin
            self.extra = kwargs
    class DummyApp:
        def __init__(self, **kwargs):
            self.params = kwargs
    class DummyManager:
        def __init__(self, **kwargs):
            self.params = kwargs
    from apps.agent_core.agent_factory import AgentFactory
    config = load_agent_config()
    agent = AgentFactory(
        runtime_cls=DummyRuntime,
        app_cls=DummyApp,
        manager_cls=DummyManager,
        runtime_kwargs=config["runtime"],
        app_kwargs=config["app"],
        manager_kwargs=config["manager"]
    ).create()
    assert isinstance(agent, DummyRuntime)
    assert isinstance(agent.app, DummyApp)
    assert isinstance(agent.manager, DummyManager)
    # Check that config values are passed through
    for k, v in config["runtime"].items():
        if k not in ("app", "manager", "interaction_plugin"):
            assert agent.extra.get(k) == v
