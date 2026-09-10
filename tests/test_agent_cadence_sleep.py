"""
Cadence-nap tests for TruckAgent / FacilityAgent (see the sleep-protocol notes in
``apps/container_logistics/openride_agent.py`` and in each agent).

Both agents gate their real work on ``current_time_step % steps_per_action == 0``.
The nap they announce covers exactly the steps that gate closes, so it must be
behaviour-identical. These tests assert that literally: the equivalence harness
at the bottom drives the REAL ``OpenRideAgent.handle_orsim_agent_message`` sleep
gate over a step range twice — once with napping enabled, once with the hint
forced to 0 — and requires the recorded app-side side effects to match exactly.

Agents are built via ``__new__`` with stub attributes (no MQTT/Celery), the same
convention as tests/test_agent_sleep_protocol.py.
"""
import json
import unittest
import unittest.mock as mock
from datetime import datetime, timedelta

from orsim.utils import time_to_str

from apps.simulation.terminations import HorizonDrainTermination

from apps.container_logistics.facility.agent import FacilityAgent
from apps.container_logistics.truck.agent import TruckAgent


REFERENCE_TIME = datetime(2020, 1, 1, 8, 0, 0)
STEP_INTERVAL = 240
HORIZON = 2520


class _RecordingApp:
    """
    Minimal stand-in for TruckApp/FacilityApp that records every side effect the
    agent performs on it, in order. Mirrors the real ``update_current`` contract
    (ORSimApp sets latest_sim_clock + latest_loc; FacilityApp additionally sets
    current_time/current_time_str) closely enough to compare runs.
    """

    def __init__(self, *, facility_style=False):
        self.calls = []
        self.message_queue = []
        self.exited_market = False
        self.current_loc = {"type": "Point", "coordinates": [103.8, 1.3]}
        self.latest_loc = self.current_loc
        self.latest_sim_clock = None
        self.current_time = None
        self.current_time_str = None
        self.manager = mock.Mock()
        self.manager.queue_controller.queue = []
        self._gate_service_ends = {}
        self._facility_style = facility_style
        self.trip = None

    def update_current(self, sim_clock):
        self.latest_sim_clock = sim_clock
        self.latest_loc = self.current_loc
        if self._facility_style:
            self.current_time_str = sim_clock
        self.calls.append(("update_current", sim_clock))

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        self.current_time = current_time
        self.calls.append(("execute_step_actions", current_time))

    def launch(self, sim_clock):
        self.calls.append(("launch", sim_clock))

    def close(self, sim_clock):
        self.exited_market = True
        self.calls.append(("close", sim_clock))

    def get_trip(self):
        return self.trip

    def has_pending_gate_work(self):
        return bool(self.message_queue) or bool(self._gate_service_ends)


def _base_agent(cls, *, behavior, active, current_time_step, app=None, orsim_settings=None):
    a = cls.__new__(cls)
    a.unique_id = f"{cls.__name__}_x"
    a.run_id = "run"
    a.scheduler_id = "agent_scheduler"
    a.reference_time = REFERENCE_TIME
    a.orsim_settings = orsim_settings or {
        "STEP_INTERVAL": STEP_INTERVAL,
        "SIMULATION_LENGTH_IN_STEPS": HORIZON,
    }
    a.behavior = behavior
    a._active = active
    a._shutdown = False
    a._sleep_until_step = None
    a._broadcast_topic = "run/agent_scheduler/ORSimAgent"
    a._broadcast_subscribed = True
    a._app_topic = "run/app_x"
    a.start_time = 0.0
    a.end_time = 0.0
    a.message_processing_active = False
    a.messenger = mock.Mock()
    a.step_log = {}
    a.failure_count = 0
    a.failure_log = {}
    a.prev_time_step = 0
    a.current_time_step = current_time_step
    a.elapsed_duration_steps = 0
    a.current_time = REFERENCE_TIME + timedelta(seconds=current_time_step * STEP_INTERVAL)
    a.next_event_time = a.current_time
    a.app = app if app is not None else _RecordingApp()
    return a


def make_truck(
    *,
    current_time_step=0,
    steps_per_action=6,
    shift_start=0,
    shift_end=HORIZON,
    active=True,
    app=None,
    horizon=HORIZON,
    # Every production scenario ships step_only_on_events=True (36060 behaviours
    # in the consortium bundle); the equivalence tests parametrise over both.
    step_only_on_events=True,
):
    behavior = {
        "steps_per_action": steps_per_action,
        "response_rate": 1.0,
        "step_only_on_events": step_only_on_events,
        "shift_start_time": shift_start,
        "shift_end_time": shift_end,
    }
    return _base_agent(
        TruckAgent,
        behavior=behavior,
        active=active,
        current_time_step=current_time_step,
        app=app,
        orsim_settings={
            "STEP_INTERVAL": STEP_INTERVAL,
            "SIMULATION_LENGTH_IN_STEPS": horizon,
        },
    )


def make_facility(
    *,
    current_time_step=0,
    steps_per_action=5,
    active=True,
    app=None,
    horizon=HORIZON,
    step_only_on_events=True,
):
    behavior = {
        "steps_per_action": steps_per_action,
        "response_rate": 1.0,
        "step_only_on_events": step_only_on_events,
    }
    return _base_agent(
        FacilityAgent,
        behavior=behavior,
        active=active,
        current_time_step=current_time_step,
        app=app if app is not None else _RecordingApp(facility_style=True),
        orsim_settings={
            "STEP_INTERVAL": STEP_INTERVAL,
            "SIMULATION_LENGTH_IN_STEPS": horizon,
        },
    )


# ── 1 + 5. The nap never skips a cadence step (property style) ──────────────
class CadenceNapArithmeticTests(unittest.TestCase):
    def test_truck_nap_lands_exactly_on_next_cadence_step(self):
        for spa in range(2, 13):
            for ts in range(0, 4 * spa + 3):
                with self.subTest(spa=spa, ts=ts):
                    a = make_truck(current_time_step=ts, steps_per_action=spa)
                    hint = a.sleep_steps_hint()
                    self.assertGreater(hint, 0)
                    # Wake step is the NEXT multiple of steps_per_action.
                    self.assertEqual((ts + hint) % spa, 0)
                    # ...and no multiple is skipped on the way there. The gate in
                    # OpenRideAgent swallows steps ts+1 .. ts+hint-1.
                    for skipped in range(ts + 1, ts + hint):
                        self.assertNotEqual(skipped % spa, 0)

    def test_facility_nap_lands_exactly_on_next_cadence_step(self):
        for spa in range(2, 13):
            for ts in range(0, 4 * spa + 3):
                with self.subTest(spa=spa, ts=ts):
                    a = make_facility(current_time_step=ts, steps_per_action=spa)
                    hint = a.sleep_steps_hint()
                    self.assertGreater(hint, 0)
                    self.assertEqual((ts + hint) % spa, 0)
                    for skipped in range(ts + 1, ts + hint):
                        self.assertNotEqual(skipped % spa, 0)

    def test_cadence_of_one_never_naps(self):
        self.assertEqual(make_truck(steps_per_action=1, current_time_step=7).sleep_steps_hint(), 0)
        self.assertEqual(make_facility(steps_per_action=1, current_time_step=7).sleep_steps_hint(), 0)

    def test_unusable_steps_per_action_never_naps(self):
        for bad in (0, -3, None, "six"):
            with self.subTest(bad=bad):
                a = make_truck(current_time_step=7)
                a.behavior["steps_per_action"] = bad
                self.assertEqual(a.sleep_steps_hint(), 0)


# ── 2. Truck lifecycle clamps ───────────────────────────────────────────────
class TruckShiftBoundaryTests(unittest.TestCase):
    def test_inactive_truck_naps_exactly_onto_shift_start(self):
        # entering_market fires on the EXACT step == shift_start_time; a nap that
        # crossed it would leave the truck never launched.
        for ts in range(0, 40):
            with self.subTest(ts=ts):
                a = make_truck(
                    current_time_step=ts, shift_start=40, steps_per_action=6, active=False
                )
                hint = a.sleep_steps_hint()
                self.assertEqual(ts + hint, 40)

    def test_inactive_truck_at_or_past_shift_start_stays_awake(self):
        for ts in (40, 41, 100):
            with self.subTest(ts=ts):
                a = make_truck(current_time_step=ts, shift_start=40, active=False)
                self.assertEqual(a.sleep_steps_hint(), 0)

    def test_active_truck_nap_never_crosses_shift_end(self):
        shift_end = 100
        for ts in range(90, shift_end + 1):
            with self.subTest(ts=ts):
                a = make_truck(
                    current_time_step=ts, steps_per_action=6, shift_end=shift_end
                )
                hint = a.sleep_steps_hint()
                # The truck must be AWAKE at shift_end (not merely waking at
                # shift_end + 1): exiting_market() runs past the shift end, and
                # the drain check reads the sleep ledger before it is pruned.
                self.assertLessEqual(ts + hint, shift_end)

    def test_truck_at_or_past_shift_end_stays_awake(self):
        for ts in (100, 101, 150):
            with self.subTest(ts=ts):
                self.assertEqual(
                    make_truck(current_time_step=ts, shift_end=100).sleep_steps_hint(), 0
                )

    def test_non_integer_shift_bounds_never_nap(self):
        a = make_truck(current_time_step=7)
        a.behavior["shift_start_time"] = None
        self.assertEqual(a.sleep_steps_hint(), 0)

        b = make_truck(current_time_step=7)
        del b.behavior["shift_end_time"]
        self.assertEqual(b.sleep_steps_hint(), 0)

    def test_shutdown_or_missing_app_never_naps(self):
        a = make_truck(current_time_step=7)
        a._shutdown = True
        self.assertEqual(a.sleep_steps_hint(), 0)

        b = make_truck(current_time_step=7)
        b.app = None
        self.assertEqual(b.sleep_steps_hint(), 0)


# ── 3. A queued message keeps the agent awake / wakes it ────────────────────
class QueuedMessageTests(unittest.TestCase):
    def test_truck_queued_message_forces_zero_and_wakes(self):
        a = make_truck(current_time_step=7)
        self.assertGreater(a.sleep_steps_hint(), 0)
        self.assertFalse(a.wake_signal())

        a.app.message_queue.append({"action": "order_workflow_event"})
        self.assertEqual(a.sleep_steps_hint(), 0)
        self.assertTrue(a.wake_signal())
        self.assertTrue(a.wake_signal_for_step(8))

    def test_facility_queued_message_forces_zero_and_wakes(self):
        a = make_facility(current_time_step=7)
        self.assertGreater(a.sleep_steps_hint(), 0)
        self.assertFalse(a.wake_signal())

        a.app.message_queue.append({"action": "facility_workflow_event"})
        self.assertEqual(a.sleep_steps_hint(), 0)
        self.assertTrue(a.wake_signal())
        self.assertTrue(a.wake_signal_for_step(8))

    def test_queued_message_wakes_a_sleeping_truck_through_the_real_gate(self):
        a = make_truck(current_time_step=6, steps_per_action=6)
        a._sleep_until_step = 12
        a.app.message_queue.append({"action": "order_workflow_event"})
        a.message_processing_active = True

        a.handle_orsim_agent_message({"action": "step", "time_step": 7})

        self.assertIsNone(a._sleep_until_step)
        self.assertEqual(a.messenger.client.publish.call_count, 1)


# ── 4. Facility gate / queue clamps ────────────────────────────────────────
class FacilityGateTests(unittest.TestCase):
    def test_non_empty_queue_controller_queue_forces_zero(self):
        a = make_facility(current_time_step=7)
        a.app.manager.queue_controller.queue = ["truck_1"]
        self.assertEqual(a.sleep_steps_hint(), 0)

    def test_nap_never_passes_the_soonest_gate_service_end(self):
        # steps_per_action=5, ts=6 -> the plain cadence nap would be 4 (wake at 10).
        for gate_steps_away, expected_cap in ((1, 1), (2, 2), (3, 3), (4, 4), (9, 4)):
            with self.subTest(gate_steps_away=gate_steps_away):
                a = make_facility(current_time_step=6, steps_per_action=5)
                a.app._gate_service_ends = {
                    0: a.current_time + timedelta(seconds=gate_steps_away * STEP_INTERVAL),
                    1: a.current_time + timedelta(seconds=50 * STEP_INTERVAL),
                }
                hint = a.sleep_steps_hint()
                self.assertEqual(hint, expected_cap)
                # The wake step is never later than the step that reaches the end.
                self.assertLessEqual(a.current_time_step + hint, 6 + gate_steps_away)

    def test_gate_service_already_due_forces_zero(self):
        a = make_facility(current_time_step=6, steps_per_action=5)
        a.app._gate_service_ends = {0: a.current_time - timedelta(seconds=1)}
        self.assertEqual(a.sleep_steps_hint(), 0)

    def test_bad_gate_end_values_force_zero(self):
        a = make_facility(current_time_step=6, steps_per_action=5)
        a.app._gate_service_ends = {0: "not-a-datetime"}
        self.assertEqual(a.sleep_steps_hint(), 0)

    def test_nap_never_crosses_the_horizon(self):
        # exiting_market() shuts a facility down on ANY step >= horizon.
        for ts in range(HORIZON - 12, HORIZON):
            with self.subTest(ts=ts):
                a = make_facility(current_time_step=ts, steps_per_action=5)
                self.assertLessEqual(ts + a.sleep_steps_hint(), HORIZON)

    def test_facility_at_or_past_horizon_stays_awake(self):
        for ts in (HORIZON, HORIZON + 1, HORIZON + 30):
            with self.subTest(ts=ts):
                self.assertEqual(make_facility(current_time_step=ts).sleep_steps_hint(), 0)

    def test_inactive_facility_never_naps(self):
        self.assertEqual(make_facility(current_time_step=7, active=False).sleep_steps_hint(), 0)


# ── Clock advance while napping ─────────────────────────────────────────────
class SleepingClockAdvanceTests(unittest.TestCase):
    def test_truck_clock_tracks_the_swallowed_broadcast(self):
        a = make_truck(current_time_step=6, steps_per_action=6)
        a._sleep_until_step = 12

        for ts in (7, 8, 9, 10, 11):
            a.message_processing_active = True
            a.handle_orsim_agent_message({"action": "step", "time_step": ts})
            self.assertEqual(a.current_time_step, ts)
            self.assertEqual(
                a.app.latest_sim_clock,
                time_to_str(REFERENCE_TIME + timedelta(seconds=ts * STEP_INTERVAL)),
            )
        # ...without ever replying to the scheduler.
        a.messenger.client.publish.assert_not_called()

    def test_facility_clock_tracks_the_swallowed_broadcast(self):
        a = make_facility(current_time_step=5, steps_per_action=5)
        a._sleep_until_step = 10

        for ts in (6, 7, 8, 9):
            a.message_processing_active = True
            a.handle_orsim_agent_message({"action": "step", "time_step": ts})
            self.assertEqual(a.current_time_step, ts)
            self.assertEqual(
                a.app.current_time_str,
                time_to_str(REFERENCE_TIME + timedelta(seconds=ts * STEP_INTERVAL)),
            )
        a.messenger.client.publish.assert_not_called()

    def test_clock_advance_never_goes_backwards_and_never_raises(self):
        a = make_truck(current_time_step=10)
        a._advance_clock_while_sleeping(5)
        self.assertEqual(a.current_time_step, 10)
        a._advance_clock_while_sleeping("nonsense")
        self.assertEqual(a.current_time_step, 10)

        b = make_truck(current_time_step=10)
        b.app.update_current = mock.Mock(side_effect=RuntimeError("boom"))
        b._advance_clock_while_sleeping(11)  # must not propagate

    def test_early_wake_clock_update_is_idempotent(self):
        # On an early wake the clock is advanced by the gate hook and again by
        # step(); both write the same value.
        a = make_truck(current_time_step=6, steps_per_action=6)
        a._sleep_until_step = 12
        a.app.message_queue.append({"action": "order_workflow_event"})
        a.message_processing_active = True

        a.handle_orsim_agent_message({"action": "step", "time_step": 7})

        clocks = [c[1] for c in a.app.calls if c[0] == "update_current"]
        self.assertEqual(set(clocks), {time_to_str(REFERENCE_TIME + timedelta(seconds=7 * STEP_INTERVAL))})


# ── The equivalence harness: napping vs never napping ──────────────────────
def _drive(agent, steps):
    """Feed `steps` step-broadcasts through the REAL sleep gate."""
    for ts in steps:
        agent.message_processing_active = True
        agent.handle_orsim_agent_message({"action": "step", "time_step": ts})
    return agent


class BehaviourEquivalenceTests(unittest.TestCase):
    """
    The load-bearing test. The claim being defended is that a cadence nap is
    behaviour-identical, so the recorded app-side side effects (every
    update_current clock, every execute_step_actions sim time, every launch)
    must be byte-identical between a napping run and a never-napping run over
    the same step range.
    """

    def _compare(self, factory, steps):
        napping = _drive(factory(), steps)
        with mock.patch.object(type(factory()), "sleep_steps_hint", lambda self: 0):
            awake = _drive(factory(), steps)
        self.assertEqual(napping.app.calls, awake.app.calls)
        self.assertEqual(napping.current_time_step, awake.current_time_step)
        self.assertEqual(napping.current_time, awake.current_time)
        self.assertEqual(napping.app.latest_sim_clock, awake.app.latest_sim_clock)
        self.assertEqual(napping._shutdown, awake._shutdown)
        return napping, awake

    def test_truck_active_run_is_identical(self):
        for soe in (False, True):
            with self.subTest(step_only_on_events=soe):
                napping, awake = self._compare(
                    lambda: make_truck(
                        current_time_step=0, steps_per_action=6, step_only_on_events=soe
                    ),
                    range(0, 120),
                )
                # Sanity: the nap really was engaged (far fewer replies than steps).
                self.assertLess(napping.messenger.client.publish.call_count, 40)
                self.assertEqual(awake.messenger.client.publish.call_count, 120)
                if not soe:
                    # ...and every step that did real work still did it.
                    worked = [
                        c for c in napping.app.calls if c[0] == "execute_step_actions"
                    ]
                    self.assertEqual(len(worked), 20)  # steps 0,6,...,114

    def test_truck_launch_at_shift_start_is_identical(self):
        for soe in (False, True):
            with self.subTest(step_only_on_events=soe):
                napping, _ = self._compare(
                    lambda: make_truck(
                        current_time_step=0,
                        steps_per_action=6,
                        shift_start=37,
                        active=False,
                        step_only_on_events=soe,
                    ),
                    range(0, 80),
                )
                launches = [c for c in napping.app.calls if c[0] == "launch"]
                self.assertEqual(len(launches), 1, "truck must still launch exactly once")
                self.assertTrue(napping.active)

    def test_truck_shift_end_shutdown_is_identical(self):
        for soe in (False, True):
            with self.subTest(step_only_on_events=soe):
                napping, awake = self._compare(
                    lambda: make_truck(
                        current_time_step=0,
                        steps_per_action=6,
                        shift_end=53,
                        step_only_on_events=soe,
                    ),
                    range(0, 80),
                )
                self.assertTrue(napping._shutdown)
                self.assertTrue(awake._shutdown)

    def test_facility_run_is_identical(self):
        for soe in (False, True):
            with self.subTest(step_only_on_events=soe):
                napping, awake = self._compare(
                    lambda: make_facility(
                        current_time_step=0,
                        steps_per_action=5,
                        horizon=90,
                        step_only_on_events=soe,
                    ),
                    range(0, 95),
                )
                self.assertLess(napping.messenger.client.publish.call_count, 40)
                self.assertTrue(napping._shutdown)
                self.assertTrue(awake._shutdown)


class RngStreamTests(unittest.TestCase):
    """
    ``step`` reads ``random()`` only inside ``current_time_step % spa == 0 and
    random() <= response_rate`` — Python short-circuits, so an off-cadence step
    draws nothing. A nap must therefore consume no randomness, or two arms of a
    policy comparison would diverge for a reason that is not the policy.
    """

    def _run_counting_draws(self, module, factory, steps):
        draws = []

        def _counting_random():
            draws.append(1)
            return 0.0

        with mock.patch.object(module, "random", _counting_random):
            _drive(factory(), steps)
        return len(draws)

    def test_truck_nap_draws_no_randomness(self):
        from apps.container_logistics.truck import agent as truck_module

        def factory():
            a = make_truck(current_time_step=0, steps_per_action=6)
            a.behavior["response_rate"] = 0.5
            return a

        napping = self._run_counting_draws(truck_module, factory, range(0, 120))
        with mock.patch.object(TruckAgent, "sleep_steps_hint", lambda self: 0):
            awake = self._run_counting_draws(truck_module, factory, range(0, 120))
        self.assertEqual(napping, awake)
        self.assertEqual(napping, 20)  # one draw per cadence step only

    def test_facility_nap_draws_no_randomness(self):
        from apps.container_logistics.facility import agent as facility_module

        def factory():
            a = make_facility(current_time_step=0, steps_per_action=5, horizon=10_000)
            a.behavior["response_rate"] = 0.5
            return a

        napping = self._run_counting_draws(facility_module, factory, range(0, 100))
        with mock.patch.object(FacilityAgent, "sleep_steps_hint", lambda self: 0):
            awake = self._run_counting_draws(facility_module, factory, range(0, 100))
        self.assertEqual(napping, awake)
        self.assertEqual(napping, 20)


# ── elapsed_duration_steps / prev_time_step fidelity ───────────────────────
class StepDeltaFidelityTests(unittest.TestCase):
    """
    ``_advance_clock_while_sleeping`` runs ``bootstrap_step``; on an early wake
    ``handle_orsim_agent_message`` runs it again, which would leave
    ``prev_time_step == current_time_step`` and ``elapsed_duration_steps == 0``.
    Neither field has a reader today, so this is latent — but it must still
    match the awake case.
    """

    def test_normal_wake_keeps_elapsed_duration_of_one(self):
        a = make_truck(current_time_step=6, steps_per_action=6)
        a._sleep_until_step = 12
        for ts in (7, 8, 9, 10, 11, 12):
            a.message_processing_active = True
            a.handle_orsim_agent_message({"action": "step", "time_step": ts})
            self.assertEqual(a.prev_time_step, ts - 1)
            self.assertEqual(a.elapsed_duration_steps, 1)

    def test_early_wake_keeps_elapsed_duration_of_one(self):
        a = make_truck(current_time_step=6, steps_per_action=6)
        a._sleep_until_step = 12
        a.app.message_queue.append({"action": "order_workflow_event"})
        a.message_processing_active = True

        a.handle_orsim_agent_message({"action": "step", "time_step": 7})

        self.assertIsNone(a._sleep_until_step)
        self.assertEqual(a.prev_time_step, 6)
        self.assertEqual(a.elapsed_duration_steps, 1)

    def test_facility_early_wake_keeps_elapsed_duration_of_one(self):
        a = make_facility(current_time_step=5, steps_per_action=5)
        a._sleep_until_step = 10
        a.app.message_queue.append({"action": "facility_workflow_event"})
        a.message_processing_active = True

        a.handle_orsim_agent_message({"action": "step", "time_step": 6})

        self.assertEqual(a.prev_time_step, 5)
        self.assertEqual(a.elapsed_duration_steps, 1)


# ── Horizon clamp (post-horizon drain) ─────────────────────────────────────
class HorizonClampTests(unittest.TestCase):
    def test_truck_is_awake_from_horizon_minus_one(self):
        horizon = 60
        for ts in range(horizon - 1, horizon + 20):
            with self.subTest(ts=ts):
                a = make_truck(
                    current_time_step=ts,
                    steps_per_action=6,
                    shift_end=horizon - 1,
                    horizon=horizon,
                )
                self.assertEqual(a.sleep_steps_hint(), 0)

    def test_truck_nap_never_wakes_later_than_horizon_minus_one(self):
        horizon = 60
        for ts in range(0, horizon):
            with self.subTest(ts=ts):
                a = make_truck(
                    current_time_step=ts,
                    steps_per_action=6,
                    shift_end=horizon - 1,
                    horizon=horizon,
                )
                self.assertLessEqual(ts + a.sleep_steps_hint(), horizon - 1)

    def test_horizon_clamp_holds_even_when_shift_end_equals_horizon(self):
        # datagen guarantees shift_end <= horizon-1 (simulation_end_step is
        # simulation_length_in_steps - 1), but a hand-edited scenario need not.
        horizon = 60
        for ts in range(0, horizon + 10):
            with self.subTest(ts=ts):
                a = make_truck(
                    current_time_step=ts,
                    steps_per_action=6,
                    shift_end=horizon,
                    horizon=horizon,
                )
                hint = a.sleep_steps_hint()
                # Either no nap at all, or one that wakes by horizon-1.
                self.assertTrue(hint == 0 or ts + hint <= horizon - 1, f"{ts=} {hint=}")

    def test_facility_is_awake_from_horizon_minus_one(self):
        horizon = 60
        for ts in range(horizon - 1, horizon + 20):
            with self.subTest(ts=ts):
                a = make_facility(
                    current_time_step=ts, steps_per_action=5, horizon=horizon
                )
                self.assertEqual(a.sleep_steps_hint(), 0)

    def test_facility_nap_never_wakes_later_than_horizon_minus_one(self):
        horizon = 60
        for ts in range(0, horizon):
            with self.subTest(ts=ts):
                a = make_facility(
                    current_time_step=ts, steps_per_action=5, horizon=horizon
                )
                self.assertLessEqual(ts + a.sleep_steps_hint(), horizon - 1)

    def test_hint_is_never_negative(self):
        # OpenRideAgent reads sleep_steps < 0 as an INDEFINITE nap, which would
        # park the agent for the rest of the run.
        for ts in range(0, 80):
            for spa in (2, 5, 6, 11):
                self.assertGreaterEqual(
                    make_truck(
                        current_time_step=ts, steps_per_action=spa, shift_end=59, horizon=60
                    ).sleep_steps_hint(),
                    0,
                )
                self.assertGreaterEqual(
                    make_facility(
                        current_time_step=ts, steps_per_action=spa, horizon=60
                    ).sleep_steps_hint(),
                    0,
                )


# ── Post-horizon drain regression ──────────────────────────────────────────
class _Source:
    def __init__(self, exhausted=True):
        self._exhausted = exhausted

    def is_exhausted(self) -> bool:
        return self._exhausted


class _ServiceScheduler:
    def __init__(self, agent_ids):
        self.agent_collection = {a: {} for a in agent_ids}


class _SleepLedgerScheduler:
    """
    Stand-in for OpenRideScheduler's sleep bookkeeping, faithful on the two
    points the drain bug turns on:

      * ``_handle_reply``: a reply carrying ``sleep_steps: K`` records
        ``wake = reply_time_step + K``; a reply WITHOUT it clears the entry.
      * ``_begin_step_barrier``: naps expire DURING step S (``wake <= self.time``)
        — which is AFTER SimulationRuntime._run_schedulers_for_step has already
        evaluated ``is_final_step(S)``. ``sleeping_agent_count`` is the live dict
        length, so the check at step S sees every agent with ``wake >= S``.
    """

    def __init__(self, agents):
        self.agents = {a.unique_id: a for a in agents}
        self.agent_collection = {a.unique_id: {} for a in agents}
        self._sleeping_until = {}
        self.time = 0

    @property
    def sleeping_agent_count(self) -> int:
        return len(self._sleeping_until)

    def begin_step_barrier(self):
        for aid in [a for a, wake in self._sleeping_until.items() if wake <= self.time]:
            del self._sleeping_until[aid]

    def deliver(self, time_step):
        for aid, agent in list(self.agents.items()):
            if agent._shutdown:
                self.agent_collection.pop(aid, None)
                self._sleeping_until.pop(aid, None)
                continue
            agent.messenger.client.publish.reset_mock()
            agent.message_processing_active = True
            agent.handle_orsim_agent_message({"action": "step", "time_step": time_step})
            for call in agent.messenger.client.publish.call_args_list:
                reply = json.loads(call.args[1])
                sleep_steps = reply.get("sleep_steps")
                if sleep_steps:
                    self._sleeping_until[aid] = reply["time_step"] + int(sleep_steps)
                elif aid in self._sleeping_until:
                    del self._sleeping_until[aid]
            if agent._shutdown:
                self.agent_collection.pop(aid, None)
                self._sleeping_until.pop(aid, None)


class PostHorizonDrainRegressionTests(unittest.TestCase):
    """
    HorizonDrainTermination._awake_agent_count() is
    ``len(agent_collection) - sleeping_agent_count`` — it subtracts EVERY
    sleeper, including a timed cadence nap. If a phase-aligned fleet is still in
    the sleep ledger when ``is_final_step(horizon)`` is evaluated, the scheduler
    reads as drained and the post-horizon drain is SKIPPED silently (the run
    still reports completed/ok), sweeping in-flight trips into the finalize
    bulk-cancel and moving num_orders_completed / empty_ratio.
    """

    HORIZON = 60

    def _fleet(self):
        # One truck per residue class mod 6 and one facility per residue mod 5,
        # so whatever the phase, some agent would be mid-nap at the horizon.
        # Trucks hold a trip, so exiting_market keeps them registered past the
        # shift end — the "truck mid-haul" case from the bug report.
        agents = []
        for phase in range(6):
            t = make_truck(
                current_time_step=0,
                steps_per_action=6,
                shift_end=self.HORIZON - 1,
                horizon=self.HORIZON,
            )
            t.unique_id = f"truck_{phase}"
            t.app.trip = {"state": "loaded_in_transit"}
            agents.append(t)
        for phase in range(5):
            f = make_facility(
                current_time_step=0, steps_per_action=5, horizon=self.HORIZON
            )
            f.unique_id = f"facility_{phase}"
            # Pending gate work keeps exiting_market from reaping it at the horizon.
            f.app._gate_service_ends = {0: REFERENCE_TIME + timedelta(days=365)}
            agents.append(f)
        return agents

    def _run(self, last_step, hint_override=None):
        """
        Drive the fleet through the real sleep gate, reproducing
        SimulationRuntime._run_schedulers_for_step's ordering exactly:
        ``is_final_step(step)`` is evaluated FIRST, then ``scheduler.step()``
        runs ``_begin_step_barrier`` (which expires naps), then agents step.
        Both the sleeper count and the real termination's verdict are recorded
        at that first moment — inspecting the scheduler after the loop would
        read a ledger that step's own replies have already re-populated.
        """
        scheduler = _SleepLedgerScheduler(self._fleet())
        term = HorizonDrainTermination(self.HORIZON)
        schedulers = {"agent": scheduler, "service": _ServiceScheduler(["assignment"])}
        source = _Source(exhausted=True)

        observed = {}
        verdict = {}
        ctx = []
        if hint_override is not None:
            ctx = [
                mock.patch.object(TruckAgent, "sleep_steps_hint", hint_override[0]),
                mock.patch.object(FacilityAgent, "sleep_steps_hint", hint_override[1]),
            ]
        for c in ctx:
            c.start()
        try:
            for step in range(0, last_step + 1):
                scheduler.time = step
                observed[step] = scheduler.sleeping_agent_count
                verdict[step] = (
                    term.is_final_step(step, schedulers, source),
                    term.should_terminate(step, schedulers, source),
                )
                scheduler.begin_step_barrier()
                scheduler.deliver(step)
        finally:
            for c in ctx:
                c.stop()
        return scheduler, observed, verdict

    def test_no_agent_is_asleep_when_the_drain_check_runs(self):
        # Only steps >= horizon matter: _ready_for_final_shutdown() gates on
        # _past_horizon(step), so a sleeper below the horizon cannot end the run.
        last = self.HORIZON + 20
        _, observed, _ = self._run(last)
        for step in range(self.HORIZON, last + 1):
            self.assertEqual(
                observed[step],
                0,
                f"step {step}: {observed[step]} agent(s) still in the sleep ledger when "
                f"is_final_step() is evaluated — the post-horizon drain would be skipped",
            )

    def test_real_termination_does_not_end_the_run_during_the_drain(self):
        last = self.HORIZON + 20
        scheduler, _, verdict = self._run(last)
        # Trucks are mid-haul and facilities hold gate work, so every agent is
        # still registered — the drain must keep running the whole way.
        self.assertEqual(len(scheduler.agent_collection), 11)
        for step in range(self.HORIZON, last + 1):
            self.assertEqual(
                verdict[step],
                (False, False),
                f"step {step}: run declared finished while {len(scheduler.agent_collection)} "
                f"agents were still working",
            )

    def test_control_the_pre_fix_clamp_would_have_ended_the_run(self):
        """
        Sensitivity control: with the ORIGINAL clamp arithmetic (truck waking at
        shift_end+1, facility waking AT the horizon) the fleet IS asleep at the
        horizon check and the real termination declares the run finished — which
        is exactly the defect. If this ever stops failing-over, the regression
        test above has lost its teeth.
        """

        def _old_truck_hint(self):
            spa = int(self.behavior["steps_per_action"])
            shift_end = int(self.behavior["shift_end_time"])
            ts = self.current_time_step
            if ts > shift_end:
                return 0
            nap = spa if ts % spa == 0 else spa - ts % spa
            return max(0, min(nap, (shift_end + 1) - ts))

        def _old_facility_hint(self):
            spa = int(self.behavior["steps_per_action"])
            horizon = int(self.orsim_settings["SIMULATION_LENGTH_IN_STEPS"])
            ts = self.current_time_step
            if ts >= horizon:
                return 0
            nap = spa if ts % spa == 0 else spa - ts % spa
            return max(0, min(nap, horizon - ts))

        scheduler, observed, verdict = self._run(
            self.HORIZON, hint_override=(_old_truck_hint, _old_facility_hint)
        )
        self.assertEqual(
            observed[self.HORIZON],
            11,
            "control: the whole fleet should be mid-nap at the horizon check",
        )
        # _awake_agent_count() == 0 => the run is declared finished at the
        # horizon and the 20-step drain above never happens.
        self.assertEqual(verdict[self.HORIZON], (True, False))

    def test_all_sleeping_reads_as_drained(self):
        """
        The termination's own sensitivity, stated directly: it subtracts every
        sleeper, so a fully-asleep agent scheduler looks drained. This is the
        property the agent-side clamps exist to avoid tripping.
        """
        term = HorizonDrainTermination(10)
        source = _Source(exhausted=True)

        class _S:
            def __init__(self, ids, sleeping):
                self.agent_collection = {i: {} for i in ids}
                self.sleeping_agent_count = sleeping

        schedulers = {
            "agent": _S(["truck_1", "truck_2"], sleeping=2),
            "service": _ServiceScheduler(["assignment"]),
        }
        self.assertTrue(term.is_final_step(10, schedulers, source))

        schedulers["agent"].sleeping_agent_count = 1
        self.assertFalse(term.is_final_step(10, schedulers, source))


if __name__ == "__main__":
    unittest.main()
