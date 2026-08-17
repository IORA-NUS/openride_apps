"""
Agent-side tests for the step-barrier sleep protocol (OpenRideAgent/OrderAgent).

Agents are built via __new__ with stub attributes — no MQTT/Celery — and the
overridden handle_orsim_agent_message is exercised directly with the payload
dicts the scheduler broadcasts.
"""
import json
import unittest
import unittest.mock as mock

from apps.container_logistics.openride_agent import OpenRideAgent
from apps.container_logistics.order.agent import OrderAgent


class _SleepyAgent(OpenRideAgent):
    """Concrete OpenRideAgent whose hooks are settable per test."""

    def __init__(self):  # bypass ORSimAgent.__init__ entirely
        pass

    process_payload_on_init = False

    def entering_market(self, time_step):
        return True

    def step(self, time_step):
        return True

    def exiting_market(self):
        return False

    def _create_app(self):
        return None

    def estimate_next_event_time(self):
        return None

    def logout(self):
        return None


def _make_agent(*, sleep_hint=0, wake=False):
    a = _SleepyAgent.__new__(_SleepyAgent)
    a.unique_id = "agent_x"
    a.run_id = "run"
    a.scheduler_id = "agent_scheduler"
    a.orsim_settings = {"STEP_INTERVAL": 240}
    a._shutdown = False
    a._sleep_until_step = None
    a._broadcast_topic = "run/agent_scheduler/ORSimAgent"
    a._broadcast_subscribed = True
    a._app_topic = "run/oid_x"
    a.start_time = 0.0
    a.message_processing_active = True
    a.messenger = mock.Mock()
    a.step_log = []
    a.add_step_log = lambda msg: a.step_log.append(msg)
    a.bootstrap_step = mock.Mock(side_effect=lambda ts: setattr(a, "current_time_step", ts))
    a.process_payload = mock.Mock(return_value=True)
    a.estimate_next_event_time = mock.Mock(return_value=None)
    a.shutdown = mock.Mock()
    a.sleep_steps_hint = lambda: sleep_hint
    a.wake_signal = lambda: wake
    return a


def _published_payloads(agent):
    return [
        json.loads(call.args[1])
        for call in agent.messenger.client.publish.call_args_list
    ]


class OpenRideAgentSleepGateTests(unittest.TestCase):
    def test_step_reply_carries_sleep_hint(self):
        a = _make_agent(sleep_hint=5)
        a.handle_orsim_agent_message({"action": "step", "time_step": 10})

        replies = _published_payloads(a)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["action"], "completed")
        self.assertEqual(replies[0]["sleep_steps"], 5)
        self.assertEqual(a._sleep_until_step, 15)

    def test_sleeping_agent_swallows_steps_without_reply(self):
        a = _make_agent(sleep_hint=5)
        a.handle_orsim_agent_message({"action": "step", "time_step": 10})
        a.messenger.client.publish.reset_mock()
        a.process_payload.reset_mock()

        for ts in (11, 12, 13, 14):
            a.message_processing_active = True
            a.handle_orsim_agent_message({"action": "step", "time_step": ts})

        a.messenger.client.publish.assert_not_called()
        a.process_payload.assert_not_called()
        # The busy flag is released so the heartbeat never sees sleep as a stall.
        self.assertFalse(a.message_processing_active)

    def test_agent_wakes_at_wake_step(self):
        a = _make_agent(sleep_hint=0)
        a._sleep_until_step = 15
        a.handle_orsim_agent_message({"action": "step", "time_step": 15})

        self.assertIsNone(a._sleep_until_step)
        self.assertEqual(len(_published_payloads(a)), 1)

    def test_queued_event_wakes_agent_early(self):
        a = _make_agent(sleep_hint=0, wake=True)
        a._sleep_until_step = 100
        a.handle_orsim_agent_message({"action": "step", "time_step": 11})

        self.assertIsNone(a._sleep_until_step)
        self.assertEqual(len(_published_payloads(a)), 1)

    def test_shutdown_broadcast_is_never_swallowed(self):
        a = _make_agent()
        a._sleep_until_step = 100
        a.handle_orsim_agent_message({"action": "shutdown", "time_step": 12})

        replies = _published_payloads(a)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["action"], "shutdown")
        a.shutdown.assert_called_once()

    def test_indefinite_sleep_unsubscribes_and_reports_wake_topic(self):
        a = _make_agent(sleep_hint=-1)
        a.handle_orsim_agent_message({"action": "step", "time_step": 10})

        replies = _published_payloads(a)
        self.assertEqual(replies[0]["sleep_steps"], -1)
        self.assertEqual(replies[0]["wake_topic"], "run/oid_x")
        self.assertEqual(a._sleep_until_step, float("inf"))
        a.messenger.client.unsubscribe.assert_called_once_with(a._broadcast_topic)
        self.assertFalse(a._broadcast_subscribed)

        # Even far-future broadcasts stay swallowed until an event wakes it.
        a.messenger.client.publish.reset_mock()
        a.handle_orsim_agent_message({"action": "step", "time_step": 5000})
        a.messenger.client.publish.assert_not_called()

    def test_zombie_step_after_shutdown_is_ignored(self):
        a = _make_agent()
        a._shutdown = True
        a.handle_orsim_agent_message({"action": "step", "time_step": 42})

        a.messenger.client.publish.assert_not_called()
        a.process_payload.assert_not_called()
        self.assertFalse(a.message_processing_active)

    def test_scheduler_direct_notice_wakes_and_processes(self):
        a = _make_agent()
        a._sleep_until_step = float("inf")
        a._broadcast_subscribed = False
        handled = []
        a.handle_orsim_agent_message = lambda p: handled.append(p)
        wrapper = a._wrap_app_topic_handler(mock.Mock())

        wrapper({"action": "shutdown", "time_step": 99, "ctrl": "scheduler_direct"})

        self.assertTrue(a._broadcast_subscribed)
        self.assertEqual(handled[0]["ctrl"], "scheduler_direct")

    def test_heartbeat_liveness_resubscribe_while_sleeping(self):
        import time as _time
        a = _make_agent()
        a.orsim_settings["ORPHAN_TIMEOUT"] = 180
        a.orsim_settings["STEP_TIMEOUT"] = 30
        a._sleep_until_step = float("inf")
        a._broadcast_subscribed = False
        a.message_processing_active = False
        # Past the half-deadline: must resubscribe for a liveness refresh, NOT shut down.
        a._last_message_time = _time.time() - 100
        a.handle_heartbeat_failure()
        self.assertTrue(a._broadcast_subscribed)
        a.shutdown.assert_not_called()

    def test_heartbeat_orphan_teardown_still_fires_when_run_dead(self):
        import time as _time
        a = _make_agent()
        a.orsim_settings["ORPHAN_TIMEOUT"] = 180
        a.orsim_settings["STEP_TIMEOUT"] = 30
        a._sleep_until_step = float("inf")
        a._broadcast_subscribed = True  # resubscribed earlier, but no broadcast came
        a.message_processing_active = False
        a._last_message_time = _time.time() - 200
        a.handle_heartbeat_failure()
        a.shutdown.assert_called_once()

    def test_swallowed_liveness_broadcast_reunsubscribes(self):
        a = _make_agent()
        a._sleep_until_step = float("inf")
        a._broadcast_subscribed = True
        a.handle_orsim_agent_message({"action": "step", "time_step": 500})
        a.messenger.client.publish.assert_not_called()
        self.assertFalse(a._broadcast_subscribed)
        a.messenger.client.unsubscribe.assert_called_once_with(a._broadcast_topic)

    def test_app_event_resubscribes_sleeping_agent(self):
        a = _make_agent()
        a._sleep_until_step = float("inf")
        a._broadcast_subscribed = False
        inner = mock.Mock()
        wrapper = a._wrap_app_topic_handler(inner)

        payload = {"action": "ORDER_WORKFLOW_EVENT", "data": {"event": "ORDER_ASSIGNED_TO_TRUCK"}}
        wrapper(payload)

        inner.assert_called_once_with(payload)
        self.assertTrue(a._broadcast_subscribed)
        a.messenger.client.subscribe.assert_called_once_with(a._broadcast_topic, qos=0)


class OrderAgentSleepHintTests(unittest.TestCase):
    def _make_order(self, *, state="unassigned", queue=(), active=True,
                    current_step=100, horizon=2520, sleep_setting=-1):
        o = OrderAgent.__new__(OrderAgent)
        o.orsim_settings = {
            "UNASSIGNED_ORDER_SLEEP_STEPS": sleep_setting,
            "SIMULATION_LENGTH_IN_STEPS": horizon,
        }
        o.current_time_step = current_step
        o.active = active
        app = mock.Mock()
        app.message_queue = list(queue)
        app.manager.as_dict.return_value = {"state": state}
        o.app = app
        return o

    def test_unassigned_idle_order_sleeps_indefinitely_by_default(self):
        o = self._make_order()
        self.assertEqual(o.sleep_steps_hint(), -1)

    def test_timed_sleep_setting_still_works(self):
        o = self._make_order(sleep_setting=120)
        self.assertEqual(o.sleep_steps_hint(), 120)

    def test_timed_sleep_capped_at_horizon(self):
        o = self._make_order(current_step=2480, sleep_setting=120)
        self.assertEqual(o.sleep_steps_hint(), 40)

    def test_no_timed_sleep_past_horizon(self):
        o = self._make_order(current_step=2520, sleep_setting=120)
        self.assertEqual(o.sleep_steps_hint(), 0)

    def test_no_sleep_when_events_queued(self):
        o = self._make_order(queue=[{"action": "x"}])
        self.assertEqual(o.sleep_steps_hint(), 0)

    def test_no_sleep_when_terminal(self):
        for state in ("completed", "cancelled"):
            o = self._make_order(state=state)
            self.assertEqual(o.sleep_steps_hint(), 0, state)

    def test_in_flight_orders_sleep_event_driven_only(self):
        # Assigned/in-flight orders are pure event listeners: indefinite nap
        # even when a positive timed-nap setting is configured for unassigned.
        for state in ("created", "assigned", "in_transit", "pickup_in_progress"):
            o = self._make_order(state=state, sleep_setting=120)
            self.assertEqual(o.sleep_steps_hint(), -1, state)

    def test_no_sleep_before_launch(self):
        o = self._make_order(active=False)
        self.assertEqual(o.sleep_steps_hint(), 0)

    def test_wake_signal_reflects_queue(self):
        self.assertFalse(self._make_order().wake_signal())
        self.assertTrue(self._make_order(queue=[{"a": 1}]).wake_signal())

    def _make_waiting_order(self, *, request_step=100, now=100, max_wait=720, state="unassigned"):
        o = self._make_order(state=state, current_step=now)
        o.orsim_settings["UNASSIGNED_ORDER_MAX_WAIT_STEPS"] = max_wait
        o.behavior = {"request_time_step": request_step}
        return o

    def test_bounded_wait_wakes_overdue_unassigned_order(self):
        o = self._make_waiting_order(request_step=100)
        self.assertFalse(o.wake_signal_for_step(100 + 720))      # exactly at limit: not yet
        self.assertTrue(o.wake_signal_for_step(100 + 721))       # past limit: wake to cancel

    def test_bounded_wait_ignores_non_unassigned_and_disabled(self):
        self.assertFalse(self._make_waiting_order(state="assigned").wake_signal_for_step(5000))
        self.assertFalse(self._make_waiting_order(max_wait=0).wake_signal_for_step(5000))

    def test_overdue_order_cancels_and_exits(self):
        o = self._make_waiting_order(request_step=0, now=800)
        o.orsim_settings["SIMULATION_LENGTH_IN_STEPS"] = 2520
        o.shutdown = mock.Mock()
        o.get_current_time_str = mock.Mock(return_value="Mon, 01 Jan 2026 00:00:00 GMT")
        self.assertTrue(o.exiting_market())
        o.app.manager.cancel.assert_called_once()
        o.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
