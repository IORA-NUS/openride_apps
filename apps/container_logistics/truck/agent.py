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
        if (self.active is False) and (time_step == self.behavior.get("shift_start_time")):
            self.app.launch(sim_clock=self.get_current_time_str())
            self.active = True
            return True
        return self.active

    def exiting_market(self):
        if self.app.exited_market:
            return False
        if self.current_time_step > self.behavior.get("shift_end_time", 0) and self.app.get_trip() is None:
            self.shutdown()
            return True
        return False

    def logout(self):
        self.app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        return self.current_time

    def step(self, time_step):
        self.app.update_current(self.get_current_time_str())
        if (
            self.current_time_step % self.behavior.get("steps_per_action", 1) == 0
            and random() <= self.behavior.get("response_rate", 1.0)
        ):
            self.app.execute_step_actions(self.current_time)
            return True
        return False
