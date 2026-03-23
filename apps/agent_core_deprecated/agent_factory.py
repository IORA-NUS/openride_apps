"""
AgentFactory: Compose agents/services using role-based adapters and spec dicts.

Usage Example:

from apps.agent_core.agent_factory import AgentFactory
from apps.ride_hail.adapters.driver_adapter import RideHailDriverAdapter

adapters = {
    'driver': RideHailDriverAdapter(),
    # ... other roles
}
factory = AgentFactory(adapters)
spec = {
    'role': 'driver',
    'init_args': {
        'unique_id': 'driver_001',
        'run_id': 'run_123',
        'reference_time': '20260101',
        'init_time_step': 0,
        'behavior': {...},
    }
}
agent = factory.create_agent(spec)
"""

class AgentFactory:
    """
    Factory for creating agents and services using role-based adapters.
    Each adapter must provide a get_agent_class() method.
    """
    def __init__(self, adapters):
        self.adapters = adapters  # Dict: {'driver': DriverAdapter, ...}

    def create_agent(self, spec):
        """
        spec: dict with keys 'role' and 'init_args'.
        """
        role = spec.get('role')
        if role not in self.adapters:
            raise ValueError(f"No adapter registered for role '{role}'")
        adapter = self.adapters[role]
        agent_class = adapter.get_agent_class()
        return agent_class(**spec.get('init_args', {}))

    def create_service(self, spec):
        """
        spec: dict with keys 'role' and 'init_args'.
        """
        role = spec.get('role')
        if role not in self.adapters:
            raise ValueError(f"No adapter registered for role '{role}'")
        adapter = self.adapters[role]
        service_class = adapter.get_agent_class()
        return service_class(**spec.get('init_args', {}))
