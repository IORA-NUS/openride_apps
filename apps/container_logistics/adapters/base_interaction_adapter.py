from apps.agent_core.interaction import CallbackRouterInteractionPlugin, InteractionContext


class BaseInteractionAdapter:
    """Lightweight adapter shell for container-logistics interaction handling."""

    def __init__(self):
        self._interaction_plugin = CallbackRouterInteractionPlugin()
        self._interaction_callbacks = self._interaction_plugin.router

    def register_message(self, action, event, callback):
        self._interaction_plugin.register_message(action, event, callback)

    def register_state(self, state, callback):
        self._interaction_plugin.register_state(state, callback)

    def dispatch_message(self, action, event, payload):
        data = payload.get("data") if isinstance(payload, dict) else None
        return self._interaction_plugin.on_message(
            InteractionContext(
                action=action,
                event=event,
                payload=payload,
                data=data,
            )
        )

    def dispatch_state(self, state, **extra):
        return self._interaction_plugin.on_state(
            InteractionContext(
                state=state,
                extra=extra,
            )
        )
