"""The order-lifecycle service app: one shared subscription, three batch Mongo ops per step.

Mirrors ``assignment/app.py`` (the proven service-app precedent): no managed statemachine, no
interaction ground truth, admin ``UserRegistry``. Its topic is the single
``{run_id}/order_lifecycle`` the truck emitter targets in ``service`` mode, so *every* ORDER_*
workflow event for *every* order arrives in one queue.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from apps.common.user_registry import UserRegistry
from apps.utils import str_to_time
from orsim.lifecycle import ORSimApp

from .batch_writer import OrderBatchWriter
from .manager import OrderLifecycleManager

_DEFAULT_SWEEP_INTERVAL_STEPS = 30


class OrderLifecycleApp(ORSimApp):
    def __init__(self, run_id: str, sim_clock: str, behavior: Dict[str, Any], messenger, agent_helper=None):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
            agent_helper=agent_helper,
        )
        profile = self.behavior.get("profile") or {}
        self.writer = OrderBatchWriter(run_id, haulier_filter=profile.get("haulier_filter"))
        self.sweep_interval = max(
            1, int(profile.get("sweep_interval_steps", _DEFAULT_SWEEP_INTERVAL_STEPS))
        )

    @property
    def managed_statemachine(self):
        return None

    @property
    def interaction_ground_truth_list(self):
        return []

    @property
    def runtime_behavior_schema(self):
        return {
            "steps_per_action": {"type": "integer", "required": False},
            "response_rate": {"type": "number", "required": False},
            "step_only_on_events": {"type": "boolean", "required": False},
            "profile": {"type": "dict", "required": False, "allow_unknown": True},
        }

    def _create_user(self):
        # Admin: the lifecycle service reads haul trips across every owner (F4).
        return UserRegistry(self.sim_clock, self.credentials, role="admin")

    def _create_manager(self):
        return OrderLifecycleManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile") or {},
            persona=self.behavior.get("persona") or {},
        )

    def handle_app_topic_messages(self, payload):
        """Every ORDER_* event lands here. No per-order routing guard — ``data.order_id``
        makes each payload self-describing, so the queue is drained wholesale each step."""
        self.enqueue_message(payload)

    def _drain(self) -> List[Any]:
        events: List[Any] = []
        while True:
            payload = self.dequeue_message()
            if payload is None:
                break
            events.append(payload)
        return events

    def run_step(
        self,
        sim_clock_str: str,
        time_step: int,
        *,
        max_wait_steps: int = 0,
        horizon_steps: int = 0,
    ) -> Dict[str, Any]:
        now = str_to_time(sim_clock_str)

        # 1. Apply queued workflow events — ALWAYS, including past the horizon, so in-flight
        #    hauls still terminalize their orders before the run-end backstop runs.
        stats = self.writer.apply_events(self._drain(), fallback_sim_clock=now)

        # 2. Publish due demand. Suppressed at/past the horizon: publishing new demand that
        #    can never be served would just inflate the run-end cancelled count.
        published = 0
        if not horizon_steps or time_step < horizon_steps:
            published = self.writer.publish_due(time_step, now)

        # 3. Overdue-unassigned sweep, open-haul guarded. Runs *after* the event drain so an
        #    order assigned this very step is already out of ``unassigned``.
        swept = 0
        if max_wait_steps > 0 and time_step % self.sweep_interval == 0:
            open_ids = self.manager.order_ids_with_open_haul()
            swept = self.writer.cancel_overdue_unassigned(
                time_step, max_wait_steps, open_ids, now
            )

        return {"stats": stats, "published": published, "swept": swept}

    def close(self, sim_clock):
        # Final drain: events published in the last step must not be lost on shutdown.
        try:
            self.writer.apply_events(self._drain(), fallback_sim_clock=str_to_time(sim_clock))
        except Exception:
            logging.exception("OrderLifecycleApp: final event drain failed (continuing)")
        super().close(sim_clock)
