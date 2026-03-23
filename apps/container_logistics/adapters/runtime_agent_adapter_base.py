import logging

from apps.agent_core_deprecated.runtime import AgentRuntimeBase, RoleMessageQueueMixin

from .base_interaction_adapter import BaseInteractionAdapter


class RuntimeInteractionAgentAdapter(AgentRuntimeBase, RoleMessageQueueMixin, BaseInteractionAdapter):
    """Runtime-backed adapter base for container-logistics pilot agents."""

    def __init__(self, ingress_action, validator, state_machine, event_transition_map):
        BaseInteractionAdapter.__init__(self)
        self.message_queue = []
        self._step_logs = []
        self.active = False
        self.failure_count = 0
        self.failure_log = {}

        self._ingress_action = ingress_action
        self._validator = validator
        self._state_machine = state_machine
        self._event_transition_map = event_transition_map

        for event_name in event_transition_map:
            self.register_message(ingress_action, event_name, self._on_incoming_event)

    def add_step_log(self, message):
        self._step_logs.append(message)

    def entering_market(self, _time_step):
        self.active = True
        return True

    def is_active(self):
        return self.active

    def exiting_market(self):
        return False

    def step(self, _time_step):
        return self.consume_messages()

    def consume_messages(self):
        did_work = False
        payload = self.dequeue_message()
        while payload is not None:
            if payload.get("action") != self._ingress_action:
                payload = self.dequeue_message()
                continue

            if self._validator(payload) is False:
                payload = self.dequeue_message()
                continue

            event_name = payload.get("data", {}).get("event")
            if event_name is None:
                payload = self.dequeue_message()
                continue

            handled = self.dispatch_message(self._ingress_action, event_name, payload)
            did_work = did_work or bool(handled)
            payload = self.dequeue_message()

        return did_work

    def _on_incoming_event(self, payload, data):
        event_name = data.get("event") if isinstance(data, dict) else None
        transition_name = self._event_transition_map.get(event_name)
        if transition_name is None:
            logging.debug("No transition mapped for event: %s", event_name)
            return False

        getattr(self._state_machine, transition_name)()
        return True

    @property
    def state(self):
        state = self._state_machine.current_state
        return getattr(state, "id", getattr(state, "value", getattr(state, "identifier", str(state))))
