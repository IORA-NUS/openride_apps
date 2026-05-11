from random import random

from orsim.lifecycle import ORSimAgent

from .app import TruckApp


class TruckAgent(ORSimAgent):
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
            return self.current_time
        try:
            return app.trip.estimate_next_event_time(self.current_time)
        except Exception:
            return self.current_time

    def step(self, time_step):
        # Defensive: ORSim may attempt steps before app initialization.
        if getattr(self, "app", None) is None:
            return False
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
