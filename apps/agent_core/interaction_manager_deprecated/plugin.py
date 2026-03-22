
"""
AgentCore Interaction Plugin System
==================================

This module provides a robust, extensible, and library-ready system for agent message/state event handling.
It supports both decorator-based and imperative callback registration, and is designed for use in agent-based
simulation frameworks and reusable agent libraries.

Key Components:
- Decorators: @message_handler, @state_handler for declarative callback registration
- InteractionCallbackRouter: Internal registry/dispatcher for message/state handlers
- InteractionContext: Standard context object for all handler calls
- CallbackRouterPlugin: Main plugin interface for agent event handling

Usage Example:
--------------
from agent_core.interaction.plugin import (
    CallbackRouterPlugin, InteractionContext, message_handler, state_handler
)

class MyHandlers:
    @message_handler("driver_workflow_event", "driver_confirmed_trip")
    def handle_driver_confirmed_trip(self, payload, data, **kwargs):
        ...
    @state_handler("driver_waiting_to_pickup")
    def handle_waiting_to_pickup(self, **kwargs):
        ...

plugin = CallbackRouterPlugin(handler_obj=MyHandlers())
plugin.on_message(InteractionContext(action="driver_workflow_event", event="driver_confirmed_trip", payload={}, data={}))

-------------------

Author: [Your Name]
Date: 2026-03-21
-------------------
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple
from collections import defaultdict

def message_handler(action: str, event: str):
    """Decorator to mark a method as a message handler for (action, event)."""
    def decorator(fn):
        fn._agentcore_message_handler = (action, event)
        return fn
    return decorator

def state_handler(state: str):
    """Decorator to mark a method as a state handler for a given state."""
    def decorator(fn):
        fn._agentcore_state_handler = state
        return fn
    return decorator

@dataclass
class InteractionContext:
    """
    Standard context object passed to all interaction handlers.

    Attributes:
        action: The action/event type (for message handlers)
        event: The event name (for message handlers)
        state: The state name (for state handlers)
        payload: The message payload (dict)
        data: Additional data (dict)
        extra: Any extra context (dict)
    """
    action: Optional[str] = None
    event: Optional[str] = None
    state: Optional[str] = None
    payload: Optional[dict] = None
    data: Optional[dict] = None
    extra: Optional[dict] = None

class InteractionPlugin(Protocol):
    """
    Protocol for agent interaction plugins.
    Implement on_message and on_state for message/state event handling.
    """
    def on_message(self, ctx: InteractionContext) -> bool:
        ...
    def on_state(self, ctx: InteractionContext) -> bool:
        ...

MessageKey = Tuple[str, str]
StateKey = str
Callback = Callable[..., None]

class InteractionCallbackRouter:
    """
    Internal registry/dispatcher for message and state callbacks.
    Not intended for direct use; use CallbackRouterPlugin instead.
    """
    def __init__(self) -> None:
        self._message_handlers: Dict[MessageKey, List[Callback]] = defaultdict(list)
        self._state_handlers: Dict[StateKey, List[Callback]] = defaultdict(list)

    def register_message(self, action: str, event: str, callback: Callback) -> None:
        self._message_handlers[(action, event)].append(callback)

    def register_state(self, state: str, callback: Callback) -> None:
        self._state_handlers[state].append(callback)

    def dispatch_message(self, action: str, event: str, **context) -> bool:
        handlers = self._message_handlers.get((action, event), [])
        for callback in handlers:
            try:
                callback(**context)
            except Exception as e:
                # Log or handle callback error
                pass
        return len(handlers) > 0

    def dispatch_state(self, state: str, **context) -> bool:
        handlers = self._state_handlers.get(state, [])
        for callback in handlers:
            try:
                callback(**context)
            except Exception as e:
                # Log or handle callback error
                pass
        return len(handlers) > 0

class CallbackRouterPlugin:
    """
    Main plugin for agent message/state event handling.
    Supports both decorator-based and imperative registration.

    Args:
        handler_obj: Optional object with decorated handler methods.
    """
    def __init__(self, handler_obj: Any = None) -> None:
        self.router = InteractionCallbackRouter()
        if handler_obj is not None:
            self._register_decorated_handlers(handler_obj)

    def register_message(self, action: str, event: str, callback: Callable[..., Any]) -> None:
        """Register a message handler for (action, event)."""
        self.router.register_message(action, event, callback)

    def register_state(self, state: str, callback: Callable[..., Any]) -> None:
        """Register a state handler for a given state."""
        self.router.register_state(state, callback)

    def _register_decorated_handlers(self, obj: Any) -> None:
        """Scan obj for methods decorated as message/state handlers and register them."""
        for attr_name in dir(obj):
            fn = getattr(obj, attr_name)
            if hasattr(fn, "_agentcore_message_handler"):
                action, event = fn._agentcore_message_handler
                self.register_message(action, event, fn)
            if hasattr(fn, "_agentcore_state_handler"):
                state = fn._agentcore_state_handler
                self.register_state(state, fn)

    def on_message(self, ctx: InteractionContext) -> bool:
        """
        Dispatch a message event to registered handlers.
        Returns True if any handler was called, False otherwise.
        """
        if ctx.action is None or ctx.event is None:
            return False
        extra = ctx.extra or {}
        try:
            return self.router.dispatch_message(
                action=ctx.action,
                event=ctx.event,
                payload=ctx.payload,
                data=ctx.data,
                **extra,
            )
        except Exception as e:
            # Optionally log or handle error
            return False

    def on_state(self, ctx: InteractionContext) -> bool:
        """
        Dispatch a state event to registered handlers.
        Returns True if any handler was called, False otherwise.
        """
        if ctx.state is None:
            return False
        extra = ctx.extra or {}
        try:
            return self.router.dispatch_state(state=ctx.state, **extra)
        except Exception as e:
            # Optionally log or handle error
            return False
