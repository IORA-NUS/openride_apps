"""The order-lifecycle service agent — a service-scheduler citizen, mirroring AssignmentAgent.

Deliberately subclasses ``ORSimAgent`` (not ``OpenRideAgent``): like assignment/analytics it is
bootstrap-spawned, never sleeps, and must not participate in the order/truck orphan-teardown
machinery.
"""

from __future__ import annotations

from random import random

from orsim.lifecycle import ORSimAgent

from .app import OrderLifecycleApp


class OrderLifecycleAgent(ORSimAgent):
    def _create_app(self):
        return OrderLifecycleApp(
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
            self.app.run_step(
                self.get_current_time_str(),
                self.current_time_step,
                max_wait_steps=int(
                    self.orsim_settings.get("UNASSIGNED_ORDER_MAX_WAIT_STEPS", 0) or 0
                ),
                horizon_steps=int(
                    self.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0) or 0
                ),
            )
            return True
        return False
