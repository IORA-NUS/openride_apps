import asyncio
import unittest
import unittest.mock as mock

from apps.simulation.openride_scheduler import OpenRideScheduler


class _SchedulerHarness(OpenRideScheduler):
    """Minimal concrete scheduler for unit tests (no MQTT/Celery).

    pending_boot / barrier / reaping behavior now lives in the app-side
    OpenRideScheduler (orsim is pristine), so test against that.
    """

    def __init__(self):
        with mock.patch("orsim.core.orsim_scheduler.ORSimEnv") as env:
            env.messenger_settings = {"MQTT_BROKER": "localhost"}
            env.validate_orsim_settings = lambda s: s
            with mock.patch("orsim.core.orsim_scheduler.Messenger"):
                super().__init__(
                    run_id="run_test",
                    scheduler_id="agent_scheduler",
                    orsim_settings={
                        "SIMULATION_LENGTH_IN_STEPS": 10,
                        "STEP_TIMEOUT": 60,
                        "STEP_TIMEOUT_TOLERANCE": 0.1,
                    },
                )


class SchedulerPendingBootTests(unittest.TestCase):
    def test_pending_boot_agents_do_not_block_confirm(self):
        sched = _SchedulerHarness()
        sched.time = 3
        sched.agent_collection = {
            "booting_order": {
                "pending_boot": True,
                "step_response": {
                    3: {"reaction": "waiting", "did_step": False, "run_time": 0},
                },
            },
            "live_truck": {
                "pending_boot": False,
                "step_response": {},
            },
        }

        sched._begin_step_barrier()
        sched._handle_reply(
            {"agent_id": "live_truck", "time_step": 3, "action": "completed",
             "did_step": True, "run_time": 0.2}
        )
        waiting = sched._update_agent_stat()

        self.assertEqual(waiting, 0)
        self.assertEqual(sched.agent_stat[3]["booting"], 1)
        self.assertEqual(sched.agent_stat[3]["completed"], 1)

    def test_first_mqtt_reply_clears_pending_boot(self):
        sched = _SchedulerHarness()
        sched.time = 2
        sched.agent_collection = {
            "order_1": {
                "pending_boot": True,
                "step_response": {
                    2: {"reaction": "waiting", "did_step": False, "run_time": 0},
                },
            }
        }

        message = mock.Mock()
        message.topic = "run_test/agent_scheduler/ORSimScheduler"
        message.payload = (
            b'{"agent_id":"order_1","time_step":-1,"action":"ready","did_step":false,"run_time":0.1}'
        )

        sched.on_receive_message(None, None, message)

        self.assertFalse(sched.agent_collection["order_1"]["pending_boot"])
        self.assertEqual(
            sched.agent_collection["order_1"]["step_response"][2]["reaction"],
            "ready",
        )


    def test_step_does_not_reap_still_booting_agents(self):
        """Regression: an agent mid async-boot must survive the step's removal sweep.

        Its reaction is still the step-start default 'waiting' (no MQTT reply yet); the
        old reaper removed it the same step it was added, so no agent ever got to run.
        """
        sched = _SchedulerHarness()
        sched.time = 0
        sched.agent_collection = {
            "booting_truck": {
                "pending_boot": True,
                "step_response": {
                    0: {"reaction": "waiting", "did_step": False, "run_time": 0},
                },
            }
        }

        # Only pending_boot agents → confirm_responses sees waiting==0 and returns at once.
        asyncio.run(sched.step(is_final=False))

        self.assertIn("booting_truck", sched.agent_collection)
        self.assertTrue(sched.agent_collection["booting_truck"]["pending_boot"])

    def test_step_reaps_unresponsive_live_agent(self):
        """A booted agent that goes silent (reaction stays 'waiting') is still reaped."""
        sched = _SchedulerHarness()
        sched.orsim_settings["STEP_TIMEOUT"] = 0  # force the wait to time out immediately
        sched.time = 0
        sched.agent_collection = {
            "dead_truck": {
                "pending_boot": False,
                "step_response": {
                    0: {"reaction": "waiting", "did_step": False, "run_time": 0},
                },
            }
        }

        asyncio.run(sched.step(is_final=False))

        self.assertNotIn("dead_truck", sched.agent_collection)


if __name__ == "__main__":
    unittest.main()
