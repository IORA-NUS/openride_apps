"""
Decorator utilities for declarative callback registration in agent_core plugins.
"""

__all__ = ["message_handler", "state_handler"]

def message_handler(action, event):
    """Decorator to mark a method as a message handler for (action, event)."""
    def decorator(fn):
        fn._agentcore_message_handler = (action, event)
        return fn
    return decorator

def state_handler(state):
    """Decorator to mark a method as a state handler for a given state."""
    def decorator(fn):
        fn._agentcore_state_handler = state
        return fn
    return decorator
