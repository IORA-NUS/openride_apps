"""
Agent Skeleton Template
----------------------

This template demonstrates how to build a new agent using AgentFactory, decorator-based callbacks, and config-driven setup.

Usage:
    1. Copy this file and rename classes as needed.
    2. Implement your agent logic in the runtime, app, and manager classes.
    3. Register message/state handlers using decorators.
    4. Use AgentFactory to assemble the agent.
    5. Load parameters from a config file (see agent_config.json).
"""

from apps.agent_core_deprecated.agent_factory import AgentFactory
from apps.agent_core.interaction_manager.decorators import message_handler, state_handler

# --- Agent Runtime ---
class MyAgentRuntime:
    def __init__(self, app, manager=None, interaction_plugin=None, **kwargs):
        self.app = app
        self.manager = manager
        self.interaction_plugin = interaction_plugin
        # ...other initialization...

# --- App ---
class MyApp:
    def __init__(self, **kwargs):
        # ...app-specific initialization...
        pass

# --- Manager (optional) ---
class MyManager:
    def __init__(self, **kwargs):
        # ...manager-specific initialization...
        pass

# --- Example Callback Registration ---
class MyInteractionPlugin:
    @message_handler('my_message_type')
    def handle_message(self, message):
        # ...handle message...
        pass

    @state_handler('my_state')
    def handle_state(self, state):
        # ...handle state...
        pass

# --- Agent Assembly ---
def build_agent(config):
    return AgentFactory(
        runtime_cls=MyAgentRuntime,
        app_cls=MyApp,
        manager_cls=MyManager,
        plugin_cls=MyInteractionPlugin,
        runtime_kwargs=config["runtime"],
        app_kwargs=config["app"],
        manager_kwargs=config["manager"]
    ).create()
