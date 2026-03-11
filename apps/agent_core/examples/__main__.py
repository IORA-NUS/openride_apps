# Example entry point for running the loader and factory integration
from .loader import load_agent_config
from ..agent_factory import AgentFactory

if __name__ == "__main__":
    config = load_agent_config()
    print("Loaded config:", config)
    # Dummy classes for demonstration
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
    agent = AgentFactory(
        runtime_cls=DummyRuntime,
        app_cls=DummyApp,
        manager_cls=DummyManager,
        runtime_kwargs=config["runtime"],
        app_kwargs=config["app"],
        manager_kwargs=config["manager"]
    ).create()
    print("Created agent:", agent)
