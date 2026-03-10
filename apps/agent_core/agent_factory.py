"""
AgentFactory: Compose new agents with minimal boilerplate and maximum flexibility.

Usage Example:

from agent_core.agent_factory import AgentFactory

agent = AgentFactory(
    runtime_cls=MyAgentRuntime,
    app_cls=MyApp,
    manager_cls=MyManager,  # Optional
    plugin_cls=MyInteractionPlugin,  # Optional
    runtime_kwargs={...},
    app_kwargs={...},
    manager_kwargs={...},
    plugin_kwargs={...},
    extra_components={  # Optional, for agent-specific dependencies
        "trip_manager": MyTripManager(...),
        # ...any other custom components
    }
).create()
"""

class AgentFactory:
    def __init__(
        self,
        runtime_cls,
        app_cls,
        manager_cls=None,
        plugin_cls=None,
        runtime_kwargs=None,
        app_kwargs=None,
        manager_kwargs=None,
        plugin_kwargs=None,
        extra_components=None,
    ):
        self.runtime_cls = runtime_cls
        self.app_cls = app_cls
        self.manager_cls = manager_cls
        self.plugin_cls = plugin_cls
        self.runtime_kwargs = runtime_kwargs or {}
        self.app_kwargs = app_kwargs or {}
        self.manager_kwargs = manager_kwargs or {}
        self.plugin_kwargs = plugin_kwargs or {}
        self.extra_components = extra_components or {}

    def create(self):
        app = self.app_cls(**self.app_kwargs)
        manager = self.manager_cls(**self.manager_kwargs) if self.manager_cls else None
        plugin = self.plugin_cls(**self.plugin_kwargs) if self.plugin_cls else None
        # Compose all components for the runtime
        runtime_args = dict(app=app)
        if manager is not None:
            runtime_args["manager"] = manager
        if plugin is not None:
            runtime_args["interaction_plugin"] = plugin
        runtime_args.update(self.extra_components)
        runtime_args.update(self.runtime_kwargs)
        runtime = self.runtime_cls(**runtime_args)
        return runtime
