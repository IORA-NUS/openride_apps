"""
Async trip-geo publish (AnalyticsAgentIndie) — the publish runs off the step
barrier in a spawned greenlet so the analytics tick no longer blocks the run
for the paged haul fetch + OSRM fills + Kafka emits (~1-3s at 1000-truck scale).

Agents are built via __new__ with stub attributes — no MQTT/Celery — and the
spawn/guard/join helpers are exercised directly.
"""
import unittest
import unittest.mock as mock

from apps.container_logistics.analytics.agent import AnalyticsAgentIndie


def _bare_agent():
    agent = AnalyticsAgentIndie.__new__(AnalyticsAgentIndie)
    agent.unique_id = "analytics_000"
    agent.run_id = "run_test"
    return agent


class TestSpawnTripGeoPublish(unittest.TestCase):
    def test_spawn_captures_clock_and_publishes(self):
        """The sim clock is captured at spawn time and handed to the publish."""
        agent = _bare_agent()
        agent.get_current_time_str = mock.Mock(return_value="Mon, 01 Jan 2026 00:00:00 GMT")
        agent._publish_trip_geo_kafka = mock.Mock()

        agent._spawn_trip_geo_publish()
        agent._join_trip_geo_publish()

        agent._publish_trip_geo_kafka.assert_called_once_with("Mon, 01 Jan 2026 00:00:00 GMT")

    def test_overlap_guard_skips_while_inflight(self):
        """A tick whose previous publish is still running spawns nothing."""
        agent = _bare_agent()
        agent.get_current_time_str = mock.Mock(return_value="clock")
        agent._publish_trip_geo_kafka = mock.Mock()
        agent._trip_geo_greenlet = mock.Mock(dead=False)

        agent._spawn_trip_geo_publish()

        agent._publish_trip_geo_kafka.assert_not_called()
        agent.get_current_time_str.assert_not_called()

    def test_dead_greenlet_does_not_block_next_publish(self):
        agent = _bare_agent()
        agent.get_current_time_str = mock.Mock(return_value="clock")
        agent._publish_trip_geo_kafka = mock.Mock()
        agent._trip_geo_greenlet = mock.Mock(dead=True)

        agent._spawn_trip_geo_publish()
        agent._join_trip_geo_publish()

        agent._publish_trip_geo_kafka.assert_called_once_with("clock")

    def test_publish_exception_is_contained(self):
        """A failing publish never propagates out of the greenlet."""
        agent = _bare_agent()
        agent.get_current_time_str = mock.Mock(return_value="clock")
        agent._publish_trip_geo_kafka = mock.Mock(side_effect=RuntimeError("boom"))

        agent._spawn_trip_geo_publish()
        agent._join_trip_geo_publish()  # must not raise

        agent._publish_trip_geo_kafka.assert_called_once()

    def test_logout_joins_inflight_publish_before_close(self):
        """Run teardown waits (bounded) for an in-flight publish, then closes."""
        agent = _bare_agent()
        agent.get_current_time_str = mock.Mock(return_value="clock")
        calls = []
        agent._publish_trip_geo_kafka = lambda sim_clock: calls.append(sim_clock)
        agent.app = mock.Mock()

        agent._spawn_trip_geo_publish()
        agent.logout()

        self.assertEqual(calls, ["clock"])
        agent.app.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
