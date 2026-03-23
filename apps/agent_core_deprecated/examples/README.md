# AgentFactory & Config Examples

This folder contains usage examples for agent configuration and instantiation.

- `agent_config.json`: Example config for a driver agent
- `loader.py`: Utility to load agent config from JSON
- `test_loader.py`: Pytest-based test and integration example
- `__main__.py`: Example entry point for running the loader and factory integration

## Usage

```python
from apps.agent_core.examples.loader import load_agent_config
from apps.agent_core.agent_factory import AgentFactory

config = load_agent_config()

# Dummy classes for demonstration
class DummyRuntime: ...
class DummyApp: ...
class DummyManager: ...

agent = AgentFactory(
    runtime_cls=DummyRuntime,
    app_cls=DummyApp,
    manager_cls=DummyManager,
    runtime_kwargs=config["runtime"],
    app_kwargs=config["app"],
    manager_kwargs=config["manager"]
).create()
```

See `test_loader.py` for a full testable example.

To run the integration example directly:

```bash
python -m apps.agent_core.examples
```
