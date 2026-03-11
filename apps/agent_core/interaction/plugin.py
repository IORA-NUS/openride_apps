from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Callable, Optional

from .callback_router import InteractionCallbackRouter


@dataclass
class InteractionContext:
    """Context passed to interaction plugins during dispatch."""

    action: Optional[str] = None
    event: Optional[str] = None
    state: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    data: Optional[dict[str, Any]] = None
    extra: Optional[dict[str, Any]] = None


class InteractionPlugin(Protocol):
    """Minimal interface that ORSim could host in the future."""

    def on_message(self, context: InteractionContext) -> bool:
        ...

    def on_state(self, context: InteractionContext) -> bool:
        ...


class CallbackRouterInteractionPlugin:
    """Adapter that exposes callback routing behind plugin-style hooks.
    Supports both imperative and declarative (decorator-based) registration.
    """

    def __init__(self, router: Optional[InteractionCallbackRouter] = None, handler_obj: Any = None) -> None:
        self.router = router or InteractionCallbackRouter()
        if handler_obj is not None:
            self._register_decorated_handlers(handler_obj)

    def register_message(self, action: str, event: str, callback: Callable[..., Any]) -> None:
        self.router.register_message(action, event, callback)

    def register_state(self, state: str, callback: Callable[..., Any]) -> None:
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

    def on_message(self, context: InteractionContext) -> bool:
        if context.action is None or context.event is None:
            return False
        extra = context.extra or {}
        return self.router.dispatch_message(
            action=context.action,
            event=context.event,
            payload=context.payload,
            data=context.data,
            **extra,
        )

    def on_state(self, context: InteractionContext) -> bool:
        if context.state is None:
            return False
        extra = context.extra or {}
        return self.router.dispatch_state(state=context.state, **extra)
