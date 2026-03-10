# agent_core Streamlining Backlog

1. **Create Agent Factory/Builder**
   - Design and implement a factory or builder class that wires together AgentRuntimeBase, RoleAppBase, manager, trip manager, and interaction plugin.
   - Provide a simple API for instantiating new agents with minimal boilerplate.

2. **Declarative Callback Registration**
   - Add support for registering message/state callbacks via decorators or a configuration dictionary.
   - Update CallbackRouterInteractionPlugin to support declarative registration.

3. **Centralized Agent Configuration**
   - Define a configuration schema (YAML, JSON, or Python dict) for agent parameters, state transitions, and message types.
   - Refactor agent setup to load and use this configuration.

4. **Documentation and Example Templates**
   - Write detailed docstrings and usage examples for each base class in agent_core.
   - Add a template directory with ready-to-use agent skeletons and sample configs.

5. **Enhanced Typing and Protocols**
   - Add or refine type annotations and Protocols for all agent_core interfaces.
   - Use mypy or similar tools to enforce interface contracts.

6. **Refactor Existing Agents to Use New Patterns**
   - Migrate at least one existing agent (e.g., DriverAgent or PassengerAgent) to use the new factory, declarative callbacks, and config-driven setup.
   - Document the migration process and lessons learned.

7. **Review and Iterate**
   - Collect feedback from new agent implementations.
   - Refine the factory, configuration, and documentation based on real-world usage.
