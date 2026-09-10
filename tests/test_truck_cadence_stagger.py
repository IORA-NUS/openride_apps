"""
Tests for the OPT-IN truck cadence phase stagger
(``apps/container_logistics/truck/agent.py``, ``TRUCK_CADENCE_STAGGER``).

Why it exists: the action gate is ``current_time_step % steps_per_action == 0``
against a GLOBAL counter, so every truck on the same cadence fires on the SAME
step. Measured on run_20260907_092435 (1000 trucks): 17% of steps carried 82% of
wall time. Staggering spreads the same work across the whole cycle.

The two things these tests defend:

1. **Default OFF is byte-identical.** The flag ships off, and with it off the gate
   must reduce to the original expression for every truck and every step. Anything
   else would silently cross a comparability boundary.
2. **The gate and the nap agree.** ``_cadence_steps_to_next_action`` derives the nap
   length from the same modulo the gate uses. If the phase were applied to one and
   not the other, a truck would nap straight THROUGH its action step and silently
   stop working — the failure this file is really here to catch.
"""
import unittest
import unittest.mock as mock

from apps.container_logistics.truck import agent as truck_agent_mod
from apps.container_logistics.truck.agent import TruckAgent


SPA = 6


def _truck(uid, time_step=0, spa=SPA):
    """A TruckAgent with only the attributes the cadence path touches."""
    a = TruckAgent.__new__(TruckAgent)
    a.unique_id = uid
    a.behavior = {"steps_per_action": spa}
    a.current_time_step = time_step
    return a


def _gate_open(a):
    """The real gate expression from ``TruckAgent.step``."""
    spa = a.behavior.get("steps_per_action", 1)
    return (a.current_time_step + a._cadence_phase(spa)) % spa == 0


class TestStaggerDefaultOff(unittest.TestCase):
    def test_flag_defaults_off(self):
        self.assertFalse(
            truck_agent_mod.TRUCK_CADENCE_STAGGER,
            "the stagger must ship OFF: enabling it is a comparability boundary",
        )

    def test_phase_is_zero_when_off(self):
        with mock.patch.object(truck_agent_mod, "TRUCK_CADENCE_STAGGER", False):
            for i in range(200):
                self.assertEqual(_truck(f"truck_{i:06d}")._cadence_phase(SPA), 0)

    def test_gate_identical_to_original_when_off(self):
        """With the flag off the gate must equal the pre-change expression exactly."""
        with mock.patch.object(truck_agent_mod, "TRUCK_CADENCE_STAGGER", False):
            for i in range(50):
                for t in range(40):
                    a = _truck(f"truck_{i:06d}", time_step=t)
                    self.assertEqual(_gate_open(a), t % SPA == 0)

    def test_nap_identical_to_original_when_off(self):
        with mock.patch.object(truck_agent_mod, "TRUCK_CADENCE_STAGGER", False):
            for t in range(40):
                a = _truck("truck_000001", time_step=t)
                remainder = t % SPA
                expected = SPA if remainder == 0 else SPA - remainder
                self.assertEqual(a._cadence_steps_to_next_action(), expected)


class TestStaggerOn(unittest.TestCase):
    def setUp(self):
        self.patcher = mock.patch.object(truck_agent_mod, "TRUCK_CADENCE_STAGGER", True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_phase_is_deterministic_across_calls(self):
        """Same id -> same phase. A hash()-based phase would fail this across processes."""
        for i in range(100):
            uid = f"truck_{i:06d}"
            self.assertEqual(_truck(uid)._cadence_phase(SPA), _truck(uid)._cadence_phase(SPA))

    def test_phase_in_range(self):
        for i in range(500):
            self.assertIn(_truck(f"truck_{i:06d}")._cadence_phase(SPA), range(SPA))

    def test_work_spreads_across_the_whole_cycle(self):
        """The point of the feature: no single step carries the whole fleet."""
        fleet = [f"truck_{i:06d}" for i in range(1000)]
        per_step = {p: 0 for p in range(SPA)}
        for uid in fleet:
            for t in range(SPA):
                if _gate_open(_truck(uid, time_step=t)):
                    per_step[t] += 1
        self.assertEqual(sum(per_step.values()), len(fleet), "each truck acts exactly once per cycle")
        worst = max(per_step.values())
        # Un-staggered this is 1000 on one step and 0 on the rest.
        self.assertLess(worst, len(fleet) / 3, f"herd not broken up: {per_step}")

    def test_each_truck_still_acts_once_per_cycle(self):
        """Cadence is preserved per truck — only its phase moves."""
        for i in range(60):
            uid = f"truck_{i:06d}"
            opens = [t for t in range(SPA * 4) if _gate_open(_truck(uid, time_step=t))]
            self.assertEqual(len(opens), 4, uid)
            self.assertTrue(all(b - a == SPA for a, b in zip(opens, opens[1:])), uid)

    def test_nap_lands_exactly_on_the_next_action_step(self):
        """The invariant that keeps the nap behaviour-preserving.

        A truck naps ``_cadence_steps_to_next_action()`` steps; the gate must be open
        when it wakes, and closed on every step it slept through.
        """
        for i in range(60):
            uid = f"truck_{i:06d}"
            for t in range(SPA * 3):
                nap = _truck(uid, time_step=t)._cadence_steps_to_next_action()
                self.assertGreater(nap, 0, uid)
                self.assertTrue(
                    _gate_open(_truck(uid, time_step=t + nap)),
                    f"{uid} woke at {t + nap} with the gate shut (napped {nap} from {t})",
                )
                for skipped in range(t + 1, t + nap):
                    self.assertFalse(
                        _gate_open(_truck(uid, time_step=skipped)),
                        f"{uid} napped through its action step {skipped}",
                    )


if __name__ == "__main__":
    unittest.main()
