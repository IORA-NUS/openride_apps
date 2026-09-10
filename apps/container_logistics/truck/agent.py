import logging
import os
from hashlib import blake2b
from random import random

from dateutil.relativedelta import relativedelta

from ..openride_agent import OpenRideAgent
from .app import TruckApp

# ── Cadence phase stagger (OPT-IN, default OFF) ─────────────────────────────
# ``step``'s gate is ``current_time_step % steps_per_action == 0`` against a GLOBAL
# step counter, so every truck sharing a cadence fires on the SAME step. At 1000
# trucks that lands ~900 agents on every 6th step and leaves the other five nearly
# empty. Measured on run_20260907_092435: 17% of steps carried 82% of wall time,
# herd steps averaging 1838 ms against a 51 ms median elsewhere.
#
# Staggering gives each truck a deterministic offset into its own cycle, so the same
# total work spreads across all ``spa`` steps. Each truck still acts exactly once per
# ``spa`` steps -- its own cadence is untouched.
#
# DEFAULT OFF ON PURPOSE. Moving a truck's action to a different step changes when it
# moves relative to every other agent, and the ``random() <= response_rate`` draw in
# the gate moves with it, so the RNG stream diverges. That makes this a COMPARABILITY
# BOUNDARY in the sense of CLAUDE.md §6.3 / §6.16: runs before and after are NOT
# comparable. Enable it only as a deliberate, announced change -- never to make one
# run faster mid-experiment.
TRUCK_CADENCE_STAGGER = os.environ.get(
    "OPENRIDE_TRUCK_CADENCE_STAGGER", ""
).strip().lower() in ("1", "true", "yes", "on")


class TruckAgent(OpenRideAgent):
    def _create_app(self):
        return TruckApp(
            run_id=self.run_id,
            sim_clock=self.get_current_time_str(),
            behavior=self.behavior,
            messenger=self.messenger,
            agent_helper=self,
        )

    @property
    def process_payload_on_init(self):
        return True

    def entering_market(self, time_step):
        app = getattr(self, "app", None)
        if (self.active is False) and (time_step == self.behavior.get("shift_start_time")):
            if app is None:
                return False
            app.launch(sim_clock=self.get_current_time_str())
            self.active = True
            return True
        return self.active

    def exiting_market(self):
        app = getattr(self, "app", None)
        if app is None:
            return False
        if app.exited_market:
            return False
        if self.current_time_step > self.behavior.get("shift_end_time", 0) and app.get_trip() is None:
            self.shutdown()
            return True
        return False

    def logout(self):
        app = getattr(self, "app", None)
        if app is not None:
            app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        try:
            app = self.app
        except Exception:
            return getattr(self, "current_time", None)
        if app is None:
            return getattr(self, "current_time", None)
        try:
            trip = app.get_trip()
        except Exception:
            return getattr(self, "current_time", None)
        if trip is None:
            # No active haul: idle ping only on steps_per_action cadence.
            interval = int(getattr(self, "orsim_settings", {}).get("STEP_INTERVAL", 30))
            spa = max(1, int(self.behavior.get("steps_per_action", 1)))
            return self.current_time + relativedelta(seconds=interval * spa)
        try:
            return app.trip.estimate_next_event_time(self.current_time)
        except Exception:
            return self.current_time

    # ── Sleep protocol (see OpenRideAgent) ──────────────────────────────────
    # A truck only ever does work on steps where
    # ``current_time_step % steps_per_action == 0`` (see ``step`` below); on the
    # other 5-of-6 steps ``step`` calls ``app.update_current`` and returns False.
    # With 1000 trucks + 60 facilities on LCM(5, 6) cadences, 65% of a run's
    # steps have *no* agent doing anything, yet every agent still paid a full
    # barrier round-trip on them (measured: 1688 of 2610 steps, 177 s = 25.5% of
    # a 699 s run). A truck therefore announces a TIMED nap that ends exactly on
    # its next cadence step, so the scheduler stops expecting a reply in between.
    #
    # The nap is behaviour-preserving, not an approximation:
    #   * ``step``'s body is unreachable on the skipped steps by construction —
    #     the nap length is derived from the same modulo the gate uses. The
    #     ``random()`` draw is short-circuited by that modulo, so a nap consumes
    #     no RNG and the stochastic stream is untouched.
    #   * ``app.consume_messages`` is only reachable from
    #     ``app.execute_step_actions`` (cadence-gated), so a queued message could
    #     not have been consumed on a skipped step either. We still wake on one
    #     (``wake_signal``) — deliberately conservative.
    #   * ``entering_market`` fires on the EXACT step ``shift_start_time`` and
    #     ``exiting_market`` can shut the truck down on ANY step past
    #     ``shift_end_time``; both are clamped below so a nap can never skip them.
    #   * the per-step clock advance that happens outside ``step``
    #     (``bootstrap_step`` + ``app.update_current``) is still performed while
    #     napping — see ``_advance_clock_while_sleeping``. It is cheap (no I/O,
    #     no reply) and keeps ``app.latest_sim_clock`` / ``app.latest_loc``
    #     identical to the awake case, which matters because
    #     ``TruckApp.handle_app_topic_messages`` stamps an inbound
    #     ASSIGNED_HAUL_TRIP with them INLINE, outside the step path.

    def _cadence_phase(self, spa) -> int:
        """This truck's deterministic offset into the ``steps_per_action`` cycle.

        Always 0 unless ``TRUCK_CADENCE_STAGGER`` is on, so the default build keeps
        ``(t + 0) % spa == t % spa`` -- byte-identical to the un-staggered gate.

        blake2b over ``unique_id``, never ``hash()``: str hashing is randomised per
        process by PYTHONHASHSEED, so ``hash()`` would give one truck a different
        phase in every Celery worker and on every run -- the nap length and the gate
        would disagree, and the truck would silently skip its action steps.
        """
        if not TRUCK_CADENCE_STAGGER:
            return 0
        try:
            spa = int(spa)
        except (TypeError, ValueError):
            return 0
        if spa <= 1:
            return 0
        uid = str(getattr(self, "unique_id", "") or "")
        if not uid:
            return 0
        return int.from_bytes(blake2b(uid.encode(), digest_size=8).digest(), "big") % spa

    def _cadence_steps_to_next_action(self) -> int:
        """
        Steps from ``current_time_step`` to the next step on which ``step``'s
        ``(current_time_step + phase) % steps_per_action == 0`` gate can open. 0 when
        no nap is possible (cadence of 1, or an unusable steps_per_action).

        MUST use the same ``_cadence_phase`` as the gate: the nap is only
        behaviour-preserving because its length is derived from the identical modulo
        (see the sleep-protocol note above).
        """
        try:
            spa = int(self.behavior.get("steps_per_action", 1))
        except (TypeError, ValueError):
            return 0
        if spa <= 1:
            return 0
        time_step = self.current_time_step
        if not isinstance(time_step, int):
            return 0
        remainder = (time_step + self._cadence_phase(spa)) % spa
        return spa if remainder == 0 else spa - remainder

    def sleep_steps_hint(self) -> int:
        if getattr(self, "_shutdown", False):
            return 0
        app = getattr(self, "app", None)
        if app is None:
            return 0
        time_step = self.current_time_step
        if not isinstance(time_step, int):
            return 0

        shift_start = self.behavior.get("shift_start_time")
        shift_end = self.behavior.get("shift_end_time")
        # exiting_market() defaults shift_end_time to 0 (i.e. "already past the
        # shift"), and entering_market() compares shift_start_time by exact
        # equality — a non-integer for either makes the lifecycle unpredictable,
        # so never nap.
        if not isinstance(shift_start, int) or not isinstance(shift_end, int):
            return 0

        if not self.active:
            # Not launched yet. process_payload skips step() entirely while
            # inactive, so the only per-step work is entering_market, which
            # fires on the EXACT step == shift_start_time. Land on it exactly.
            if time_step < shift_start:
                return shift_start - time_step
            # At/past shift_start and still inactive: the launch trigger can
            # never fire again — stay awake rather than make it worse.
            return 0

        # Latest step this truck may still be asleep AT. Two independent bounds,
        # and the nap must land on or before the tighter one:
        #
        #   * ``shift_end``: exiting_market() can shut the truck down on ANY step
        #     past shift_end_time, cadence or not, so the exit must not be
        #     delayed (the run has to drain).
        #
        #   * ``horizon - 1``: HorizonDrainTermination._awake_agent_count()
        #     subtracts EVERY sleeper (it was written for indefinitely-napping
        #     order agents), and SimulationRuntime._run_schedulers_for_step
        #     evaluates is_final_step(step) BEFORE scheduler.step() runs
        #     _begin_step_barrier — which is what expires naps (``wake <=
        #     self.time``). So at the is_final_step(H) check the ledger still
        #     holds every agent with wake >= H. A phase-aligned fleet asleep at
        #     H therefore reads as "scheduler drained" and the POST-HORIZON
        #     DRAIN IS SKIPPED — silently, with status=completed/ok=true, while
        #     in-flight trips get swept into the finalize bulk-cancel and move
        #     num_orders_completed / empty_ratio. Waking by H-1 means that
        #     step's own barrier already pruned us.
        #
        # datagen makes shift_end <= horizon-1 (simulation_end_step is
        # simulation_length_in_steps - 1), but do not depend on that invariant —
        # a hand-edited scenario could set shift_end == horizon.
        latest_wake = shift_end
        try:
            horizon = int(self.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            horizon = 0
        if horizon:
            latest_wake = min(latest_wake, horizon - 1)

        if time_step >= latest_wake:
            return 0

        # Conservative: a queued workflow event could not be consumed off-cadence
        # anyway, but stay awake for it (mirrors OrderAgent.wake_signal).
        if getattr(app, "message_queue", None):
            return 0

        nap = self._cadence_steps_to_next_action()
        if nap <= 0:
            return 0
        nap = min(nap, latest_wake - time_step)
        # Never announce a negative hint: OpenRideAgent reads sleep_steps < 0 as
        # an INDEFINITE nap, which would park the truck for the rest of the run.
        return max(0, nap)

    def wake_signal(self) -> bool:
        app = getattr(self, "app", None)
        return bool(app is not None and getattr(app, "message_queue", None))

    def wake_signal_for_step(self, time_step: int) -> bool:
        # Called by OpenRideAgent's sleep gate on every swallowed broadcast, and
        # it is the only hook a napping agent gets — use it to keep the clock in
        # step with the awake case (see the note above).
        #
        # Order matters: on an EARLY wake the gate falls through to
        # handle_orsim_agent_message, which runs bootstrap_step(time_step)
        # itself. Advancing here as well would leave prev_time_step ==
        # current_time_step and elapsed_duration_steps == 0 instead of 1. Both
        # fields have zero readers today, so that was latent rather than a live
        # bug — but check the wake first and only advance when we really are
        # staying asleep, so the two fields keep their awake-case values.
        if self.wake_signal():
            return True
        self._advance_clock_while_sleeping(time_step)
        return False

    def _advance_clock_while_sleeping(self, time_step) -> None:
        """
        Replay, while napping, the per-step clock advance that
        ``handle_orsim_agent_message`` + ``step`` would have done when awake:
        ``bootstrap_step`` (agent clock) and ``app.update_current``
        (``latest_sim_clock`` / ``latest_loc``). No I/O, no barrier reply —
        only the two assignments an off-cadence awake step performs.

        Must not raise: the sleep gate runs before
        handle_orsim_agent_message's try/except, so an exception here would
        escape into the paho callback.
        """
        try:
            if not isinstance(time_step, int) or time_step <= self.current_time_step:
                return
            self.bootstrap_step(time_step)
            # process_payload only reaches step() -- and therefore
            # app.update_current -- while the agent is active. An inactive agent
            # advances only its own clock, so mirror that exactly.
            app = getattr(self, "app", None)
            if app is not None and self.active:
                app.update_current(self.get_current_time_str())
        except Exception:
            logging.exception(
                f"TruckAgent {getattr(self, 'unique_id', '?')}: clock advance while sleeping failed"
            )

    def step(self, time_step):
        # Defensive: ORSim may attempt steps before app initialization.
        if getattr(self, "app", None) is None:
            return False
        self.app.update_current(self.get_current_time_str())
        step_only_on_events = bool(self.behavior.get("step_only_on_events", False))
        spa = self.behavior.get("steps_per_action", 1)
        if (
            (self.current_time_step + self._cadence_phase(spa)) % spa == 0
            and random() <= self.behavior.get("response_rate", 1.0)
        ):
            if step_only_on_events and self.estimate_next_event_time() > self.current_time:
                return False
            self.app.execute_step_actions(self.current_time)
            return True
        return False
