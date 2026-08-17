"""Assignment + analytics agents — single service agents, one policy each (template)."""

from __future__ import annotations

from .base import Agent, Policy


class AssignmentPolicy(Policy):
    role = "assignment"
    name = "default"


class AnalyticsPolicy(Policy):
    role = "analytics"
    name = "default"


class AssignmentAgent(Agent):
    role = "assignment"

    def generate(self, n: int) -> dict:
        cfg = self.spec.assignment_settings
        return {
            "assignment_main": {
                "email": "assignment_main@test.com",
                "password": "password",
                "persona": {"role": "engine", "domain": self.domain},
                "steps_per_action": cfg.get("steps_per_action", 1),
                "response_rate": cfg.get("response_rate", 1.0),
                "step_only_on_events": cfg.get("step_only_on_events", False),
                "profile": cfg.get("profile", {}),
            }
        }


class AnalyticsAgent(Agent):
    role = "analytics"

    def generate(self, n: int) -> dict:
        cfg = self.spec.analytics_settings
        return {
            "analytics_000": {
                "email": "analytics_000@test.com",
                "password": "password",
                "persona": {"role": "analytics", "domain": self.domain},
                "steps_per_action": cfg.get("steps_per_action", 1),
                "response_rate": cfg.get("response_rate", 1.0),
                "step_only_on_events": cfg.get("step_only_on_events", False),
                "profile": cfg.get("profile", {}),
            }
        }


class OrderLifecyclePolicy(Policy):
    role = "order_lifecycle"
    name = "default"


class OrderLifecycleAgent(Agent):
    role = "order_lifecycle"

    def generate(self, n: int) -> dict:
        cfg = getattr(self.spec, "order_lifecycle_settings", None) or {}
        return {
            "order_lifecycle_main": {
                "email": "order_lifecycle_main@test.com",
                "password": "password",
                "persona": {"role": "order_lifecycle", "domain": self.domain},
                "steps_per_action": cfg.get("steps_per_action", 1),
                "response_rate": cfg.get("response_rate", 1.0),
                "step_only_on_events": cfg.get("step_only_on_events", False),
                "profile": cfg.get("profile", {"haulier_filter": None, "sweep_interval_steps": 30}),
            }
        }
