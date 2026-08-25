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
