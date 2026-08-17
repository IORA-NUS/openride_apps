import unittest

from apps.utils.perf_metrics import (
    build_step_spans,
    collect_slow_agents,
    _normalize_agent_run_time_ms,
)
from apps.utils.perf_rollup import PerfRollup


class PerfRollupTests(unittest.TestCase):
    def test_observe_tracks_mean_and_max(self):
        rollup = PerfRollup()
        self.assertTrue(rollup.observe(0, 100.0))
        self.assertFalse(rollup.observe(1, 50.0))
        self.assertTrue(rollup.observe(2, 200.0))

        payload = rollup.to_dict()
        self.assertEqual(payload["step_count"], 3)
        self.assertEqual(payload["avg_step_ms"], 116.67)
        self.assertEqual(payload["max_step_ms"], 200.0)
        self.assertEqual(payload["max_step_index"], 2)

    def test_equal_max_updates_index_on_tie(self):
        rollup = PerfRollup()
        self.assertTrue(rollup.observe(1, 100.0))
        self.assertFalse(rollup.observe(3, 100.0))
        self.assertEqual(rollup.max_step_index, 3)


class SlowAgentTests(unittest.TestCase):
    class _Scheduler:
        def __init__(self, agents):
            self.time = 2
            self.agent_collection = agents

    def test_collect_slow_agents_ranks_by_run_time(self):
        schedulers = {
            "agent": self._Scheduler(
                {
                    "a1": {
                        "step_response": {
                            1: {"run_time": 0.5},
                        }
                    },
                    "a2": {
                        "step_response": {
                            1: {"run_time": 1.2},
                        }
                    },
                }
            )
        }
        rows = collect_slow_agents(schedulers, 1, top_n=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["agent_id"], "a2")
        self.assertEqual(rows[0]["run_time_ms"], 1200.0)

    def test_normalize_agent_run_time_ms(self):
        self.assertEqual(_normalize_agent_run_time_ms(1.5), 1500.0)
        self.assertEqual(_normalize_agent_run_time_ms(850.0), 850.0)


class StepSpanTests(unittest.TestCase):
    def test_build_step_spans_orders_desc(self):
        spans = build_step_spans(
            {"agent": 120.0, "service": 40.0},
            api_ms=10.0,
            spawn_ms=5.0,
        )
        self.assertEqual(spans[0]["name"], "scheduler.agent")
        self.assertEqual(spans[0]["ms"], 120.0)


if __name__ == "__main__":
    unittest.main()
