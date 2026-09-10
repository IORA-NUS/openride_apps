import logging
from random import random

from dateutil.relativedelta import relativedelta

from ..openride_agent import OpenRideAgent
from .app import FacilityApp


class FacilityAgent(OpenRideAgent):
    def _create_app(self):
        return FacilityApp(
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
        if self.active is False:
            self.app.launch(sim_clock=self.get_current_time_str())
            self.active = True
        return True

    def exiting_market(self):
        horizon = int(self.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0))
        if self.current_time_step < horizon:
            return False
        app = getattr(self, "app", None)
        if app is None or app.has_pending_gate_work():
            return False
        self.shutdown()
        return True

    def logout(self):
        app = getattr(self, "app", None)
        if app is not None:
            try:
                app.close(self.get_current_time_str())
            except Exception:
                pass

    def estimate_next_event_time(self):
        app = getattr(self, "app", None)
        if app is None:
            return getattr(self, "current_time", None)

        # Real work pending right now → wake up this tick, otherwise the agent
        # gets parked forever by `step_only_on_events` (we wouldn't drain the
        # MQTT message queue and trucks stay stuck in queued_for_pickup).
        if getattr(app, "message_queue", None):
            return self.current_time
        manager = getattr(app, "manager", None)
        controller = getattr(manager, "queue_controller", None) if manager else None
        if controller is not None and getattr(controller, "queue", None):
            return self.current_time

        # A gate is in service → next event is the soonest service completion,
        # but never further than the regular cadence so freshly arriving trucks
        # (whose messages we may have missed between ticks) aren't starved.
        interval = int(getattr(self, "orsim_settings", {}).get("STEP_INTERVAL", 30))
        spa = max(1, int(self.behavior.get("steps_per_action", 1)))
        cadence_due = self.current_time + relativedelta(seconds=interval * spa)
        gate_ends = getattr(app, "_gate_service_ends", None) or {}
        if gate_ends:
            return min(min(gate_ends.values()), cadence_due)
        return cadence_due

    # ── Sleep protocol (see OpenRideAgent) ──────────────────────────────────
    # Same rationale as TruckAgent: a facility only does work on steps where
    # ``current_time_step % steps_per_action == 0``; on every other step ``step``
    # calls ``app.update_current`` and returns False, yet the agent still paid a
    # full barrier round-trip. It therefore announces a TIMED nap ending exactly
    # on its next cadence step.
    #
    # Facility-specific safety, on top of the cadence proof:
    #   * ``exiting_market`` shuts the facility down on ANY step at/after
    #     SIMULATION_LENGTH_IN_STEPS once no gate work is pending, so the nap is
    #     clamped to wake AT the horizon and disabled from the horizon on.
    #   * gate service completions are only ever harvested inside
    #     ``perform_workflow_actions`` (reached from ``execute_step_actions``,
    #     i.e. cadence-gated), so a cadence nap cannot delay one. We clamp on the
    #     soonest ``app._gate_service_ends`` anyway, and refuse to nap at all
    #     while the queue controller holds trucks or a message is queued — the
    #     same three signals ``estimate_next_event_time`` already keys on.
    #   * ``app.consume_messages`` is likewise only reachable from
    #     ``execute_step_actions``, so a message queued mid-nap could not have
    #     been consumed on a skipped step either.
    #   * the clock advance outside ``step`` is replayed while napping (see
    #     ``_advance_clock_while_sleeping``), keeping ``app.current_time`` /
    #     ``current_time_str`` / ``latest_sim_clock`` identical to the awake case.

    _NO_GATE_LIMIT = 1 << 30

    def _cadence_steps_to_next_action(self) -> int:
        """
        Steps from ``current_time_step`` to the next step on which ``step``'s
        ``current_time_step % steps_per_action == 0`` gate can open. 0 when no
        nap is possible.
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
        remainder = time_step % spa
        return spa if remainder == 0 else spa - remainder

    def _steps_to_next_gate_service_end(self) -> int:
        """
        Whole steps until the soonest in-flight gate service completes, floored
        (so we always wake at or before the step that would harvest it).
        ``_NO_GATE_LIMIT`` when no gate is in service.
        """
        app = getattr(self, "app", None)
        gate_ends = getattr(app, "_gate_service_ends", None) or {}
        if not gate_ends:
            return self._NO_GATE_LIMIT
        try:
            pending = [end for end in gate_ends.values() if end is not None]
            if not pending:
                return self._NO_GATE_LIMIT
            interval = int(self.orsim_settings.get("STEP_INTERVAL", 30) or 0)
            if interval <= 0:
                return 0
            remaining = (min(pending) - self.current_time).total_seconds()
            if remaining <= 0:
                return 0
            return int(remaining // interval)
        except Exception:
            logging.exception(
                f"FacilityAgent {getattr(self, 'unique_id', '?')}: gate-end nap clamp failed"
            )
            return 0

    def sleep_steps_hint(self) -> int:
        if getattr(self, "_shutdown", False) or not self.active:
            return 0
        app = getattr(self, "app", None)
        if app is None:
            return 0
        time_step = self.current_time_step
        if not isinstance(time_step, int):
            return 0

        try:
            horizon = int(self.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0) or 0)
        except (TypeError, ValueError):
            horizon = 0
        # Latest step this facility may still be asleep AT is horizon-1, for two
        # reasons:
        #   * exiting_market() can shut it down on ANY step at/after the horizon
        #     once no gate work is pending, so the exit must not be delayed.
        #   * HorizonDrainTermination._awake_agent_count() subtracts EVERY
        #     sleeper, and SimulationRuntime evaluates is_final_step(step) BEFORE
        #     scheduler.step() runs _begin_step_barrier (which is what expires
        #     naps, `wake <= self.time`). A facility still in the sleep ledger at
        #     step == horizon reads as "drained" and SKIPS the post-horizon
        #     drain silently. Waking by horizon-1 means that step's own barrier
        #     already pruned us. See the fuller note in TruckAgent.
        latest_wake = (horizon - 1) if horizon else None
        if latest_wake is not None and time_step >= latest_wake:
            return 0

        # Real work pending right now → stay awake (mirrors the three signals
        # estimate_next_event_time treats as "wake this tick").
        if getattr(app, "message_queue", None):
            return 0
        manager = getattr(app, "manager", None)
        controller = getattr(manager, "queue_controller", None) if manager is not None else None
        if controller is not None and getattr(controller, "queue", None):
            return 0

        nap = self._cadence_steps_to_next_action()
        if nap <= 0:
            return 0
        if latest_wake is not None:
            nap = min(nap, latest_wake - time_step)
        nap = min(nap, self._steps_to_next_gate_service_end())
        # Never announce a negative hint: OpenRideAgent reads sleep_steps < 0 as
        # an INDEFINITE nap, which would park the facility for the rest of the run.
        return max(0, nap)

    def wake_signal(self) -> bool:
        app = getattr(self, "app", None)
        return bool(app is not None and getattr(app, "message_queue", None))

    def wake_signal_for_step(self, time_step: int) -> bool:
        # Wake check FIRST: on an early wake the gate falls through to
        # handle_orsim_agent_message, which runs bootstrap_step itself, and
        # advancing here too would zero elapsed_duration_steps. See TruckAgent.
        if self.wake_signal():
            return True
        self._advance_clock_while_sleeping(time_step)
        return False

    def _advance_clock_while_sleeping(self, time_step) -> None:
        """
        Replay, while napping, the per-step clock advance an awake off-cadence
        step performs (``bootstrap_step`` + ``app.update_current``). No I/O, no
        barrier reply. Must not raise — the sleep gate runs before
        handle_orsim_agent_message's try/except.
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
                f"FacilityAgent {getattr(self, 'unique_id', '?')}: clock advance while sleeping failed"
            )

    def step(self, time_step):
        self.app.update_current(self.get_current_time_str())
        step_only_on_events = bool(self.behavior.get("step_only_on_events", False))
        if (
            self.current_time_step % self.behavior.get("steps_per_action", 1) == 0
            and random() <= self.behavior.get("response_rate", 1.0)
        ):
            if step_only_on_events and self.estimate_next_event_time() > self.current_time:
                return False
            self.app.execute_step_actions(self.current_time)
            return True
        return False
