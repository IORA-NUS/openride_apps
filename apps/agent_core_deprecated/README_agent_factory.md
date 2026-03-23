# AgentFactory for agent_core

## Overview

`AgentFactory` provides a flexible, boilerplate-free way to assemble agents from generic and agent-specific components. It is designed to keep agent_core decoupled from domain-specific logic, making it suitable for use as a library in any agent-based system.

## Features
- Compose agents from runtime, app, manager, and plugin classes.
- Pass agent-specific dependencies (e.g., trip_manager) via `extra_components`.
- Clean, extensible API for new agent types.

## Usage Example

### Minimal Example
```python
from agent_core.agent_factory import AgentFactory

agent = AgentFactory(
    runtime_cls=MyAgentRuntime,
    app_cls=MyApp,
).create()
```

### With Optional Components
```python
agent = AgentFactory(
    runtime_cls=MyAgentRuntime,
    app_cls=MyApp,
    manager_cls=MyManager,
    plugin_cls=MyInteractionPlugin,
    runtime_kwargs={...},
    app_kwargs={...},
    manager_kwargs={...},
    plugin_kwargs={...},
    extra_components={
        "custom_component": MyCustomComponent(...),
    }
).create()
```

---

## Concrete Example: Driver Agent

```python
from agent_core.agent_factory import AgentFactory
from apps.ride_hail.driver.agent import DriverAgentIndie
from apps.ride_hail.driver.app import DriverApp
from apps.ride_hail.driver.manager import DriverManager
from apps.ride_hail.driver.trip_manager import DriverTripManager
from agent_core.interaction.plugin import CallbackRouterInteractionPlugin

# Instantiate agent-specific components
trip_manager = DriverTripManager(
    run_id="run-001",
    sim_clock="2026-03-10T00:00:00Z",
    user=None,
    messenger=None,
)

runtime_kwargs = {
    "unique_id": "driver-123",
    "run_id": "run-001",
    "reference_time": "2026-03-10T00:00:00Z",
    "init_time_step": 0,
    "scheduler": None,
    "behavior": {
        "init_loc": {"type": "Point", "coordinates": [103.85, 1.29]},
        "profile": {},
        "email": "driver@example.com",
        "password": "secret",
    },
}
app_kwargs = {
    "run_id": "run-001",
    "sim_clock": "2026-03-10T00:00:00Z",
    "current_loc": {"type": "Point", "coordinates": [103.85, 1.29]},
    "credentials": {"email": "driver@example.com", "password": "secret"},
    "profile": {},
    "messenger": None,
}
manager_kwargs = {
    "run_id": "run-001",
    "sim_clock": "2026-03-10T00:00:00Z",
    "user": None,
    "profile": {},
}
plugin_kwargs = {}

driver_agent = AgentFactory(
    runtime_cls=DriverAgentIndie,
    app_cls=DriverApp,
    manager_cls=DriverManager,
    plugin_cls=CallbackRouterInteractionPlugin,
    runtime_kwargs=runtime_kwargs,
    app_kwargs=app_kwargs,
    manager_kwargs=manager_kwargs,
    plugin_kwargs=plugin_kwargs,
    extra_components={
        "trip_manager": trip_manager,
    }
).create()
```

---

## Notes
- `extra_components` lets you inject any additional dependencies required by your agent, without polluting the agent_core API.
- All component classes and kwargs are optional except for `runtime_cls` and `app_cls`.
- The factory does not assume any domain-specific structure, so it is suitable for a wide range of agent architectures.
