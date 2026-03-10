from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .callback_router import InteractionCallbackRouter


@dataclass
class InteractionContext:
    """Context passed to interaction plugins during dispatch."""

    action: str | None = None
    event: str | None = None
    state: str | None = None
    payload: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None


class InteractionPlugin(Protocol):
    """Minimal interface that ORSim could host in the future."""

    def on_message(self, context: InteractionContext) -> bool:
        ...

    def on_state(self, context: InteractionContext) -> bool:
        ...


class CallbackRouterInteractionPlugin:
    """Adapter that exposes callback routing behind plugin-style hooks."""

    def __init__(self, router: InteractionCallbackRouter | None = None) -> None:
        self.router = router or InteractionCallbackRouter()

    def register_message(self, action: str, event: str, callback) -> None:
        self.router.register_message(action, event, callback)

    def register_state(self, state: str, callback) -> None:
        self.router.register_state(state, callback)

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
