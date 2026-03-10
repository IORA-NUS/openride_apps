from collections import defaultdict
from typing import Callable, Dict, List, Tuple


MessageKey = Tuple[str, str]
StateKey = str
Callback = Callable[..., None]


class InteractionCallbackRouter:
    """Simple registry/dispatcher for message and state callbacks."""

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
            callback(**context)
        return len(handlers) > 0

    def dispatch_state(self, state: str, **context) -> bool:
        handlers = self._state_handlers.get(state, [])
        for callback in handlers:
            callback(**context)
        return len(handlers) > 0
