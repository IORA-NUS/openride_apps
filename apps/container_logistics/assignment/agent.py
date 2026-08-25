from random import random

from orsim.lifecycle import ORSimAgent

from .app import AssignmentApp


class AssignmentAgent(ORSimAgent):
    def _create_app(self):
        return AssignmentApp(
            run_id=self.run_id,
            sim_clock=self.get_current_time_str(),
            behavior=self.behavior,
            messenger=self.messenger,
            agent_helper=self,
        )

    @property
    def process_payload_on_init(self):
        return False

    def entering_market(self, time_step):
        app = getattr(self, "app", None)
        if self.active is False and app is not None:
            # Hand the app the simulation horizon so its market provenance can
            # separate in-horizon ticks from post-horizon drain ticks, putting the
            # tick count on the same window as the KPI block beside it (plan R3-5).
            try:
                app._sim_horizon_steps = (self.orsim_settings or {}).get(
                    "SIMULATION_LENGTH_IN_STEPS"
                )
            except Exception:
                app._sim_horizon_steps = None
            app.launch(sim_clock=self.get_current_time_str())
        self.active = True

    def exiting_market(self):
        self.active = False

    def logout(self):
        app = getattr(self, "app", None)
        if app is not None:
            app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        return self.current_time

    def step(self, time_step):
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
            result = self.app.assign(self.get_current_time_str(), self.current_time_step)
            self.app.publish(result)
            return True
        return False
