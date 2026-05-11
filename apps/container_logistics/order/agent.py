from random import random

from orsim.lifecycle import ORSimAgent

from .app import OrderApp


class OrderAgent(ORSimAgent):
    def _create_app(self):
        return OrderApp(
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
        """
        Launch the order app when the order is "requested" (request_time_step).
        """
        app = getattr(self, "app", None)
        if (self.active is False) and (time_step == self.behavior.get("request_time_step", 0)):
            if app is None:
                return False
            app.launch(sim_clock=self.get_current_time_str())
            self.active = True
            return True
        return self.active

    def exiting_market(self):
        """
        Exit once the order reaches a terminal state (completed/cancelled).
        """
        app = getattr(self, "app", None)
        if app is None:
            return False
        try:
            order = app.manager.as_dict() or {}
        except Exception:
            return False
        state = order.get("state")
        if state in {"completed", "cancelled"}:
            self.shutdown()
            return True
        return False

    def logout(self):
        # Orders don't hold external resources, but close hook keeps parity with other agents.
        app = getattr(self, "app", None)
        if app is not None:
            try:
                app.close(self.get_current_time_str())
            except Exception:
                pass

    def estimate_next_event_time(self):
        # Order state is event-driven in this first pass.
        return getattr(self, "current_time", None)

    def step(self, time_step):
        if getattr(self, "app", None) is None:
            return False
        self.app.update_current(self.get_current_time_str())
        if (
            self.current_time_step % self.behavior.get("steps_per_action", 1) == 0
            and random() <= self.behavior.get("response_rate", 1.0)
        ):
            self.app.execute_step_actions(self.current_time)
            return True
        return False
