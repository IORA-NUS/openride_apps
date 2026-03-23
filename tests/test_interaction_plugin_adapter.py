from orsim.messenger.interaction import (
    CallbackRouterPlugin,
    InteractionContext,
)


def test_plugin_adapter_dispatches_registered_message_handler():
    plugin = CallbackRouterPlugin()
    calls = []

    def _handler(payload=None, data=None, **_kwargs):
        calls.append((payload, data))

    plugin.register_message("driver_workflow_event", "driver_confirmed_trip", _handler)

    handled = plugin.on_message(
        InteractionContext(
            action="driver_workflow_event",
            event="driver_confirmed_trip",
            payload={"driver_id": "d1"},
            data={"estimated_time_to_arrive": 10},
        )
    )

    assert handled is True
    assert len(calls) == 1


def test_plugin_adapter_dispatches_registered_state_handler():
    plugin = CallbackRouterPlugin()
    calls = []

    def _handler(**kwargs):
        calls.append(kwargs)

    plugin.register_state("driver_pickedup", _handler)

    handled = plugin.on_state(
        InteractionContext(
            state="driver_pickedup",
            extra={"time_since_last_event": 12.5},
        )
    )

    assert handled is True
    assert calls[0]["time_since_last_event"] == 12.5


def test_plugin_adapter_returns_false_for_unregistered_entries():
    plugin = CallbackRouterPlugin()

    message_handled = plugin.on_message(
        InteractionContext(action="x", event="y", payload={}, data={})
    )
    state_handled = plugin.on_state(InteractionContext(state="unknown_state"))

    assert message_handled is False
    assert state_handled is False
