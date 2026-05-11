from random import random

from orsim.lifecycle import ORSimAgent

from .app import FacilityApp


class FacilityAgent(ORSimAgent):
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
        return False

    def logout(self):
        app = getattr(self, "app", None)
        if app is not None:
            try:
                app.close(self.get_current_time_str())
            except Exception:
                pass

    def estimate_next_event_time(self):
        # Facility progression is timer-driven, but in the first pass we allow it to tick.
        return getattr(self, "current_time", None)

    def step(self, time_step):
        self.app.update_current(self.get_current_time_str())
        if (
            self.current_time_step % self.behavior.get("steps_per_action", 1) == 0
            and random() <= self.behavior.get("response_rate", 1.0)
        ):
            self.app.execute_step_actions(self.current_time)
            return True
        return False
