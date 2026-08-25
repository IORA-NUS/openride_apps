import logging
from random import random

from dateutil.relativedelta import relativedelta

from ..openride_agent import OpenRideAgent
from .app import OrderApp


class OrderAgent(OpenRideAgent):
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

        Uses ``>=`` so orders deferred by ORDER_SPAWN_MAX_PER_STEP batching
        still enter the market on their first step after Celery boot.
        """
        app = getattr(self, "app", None)
        request_step = int(self.behavior.get("request_time_step", 0))
        if (self.active is False) and (time_step >= request_step):
            if app is None:
                return False
            app.launch(sim_clock=self.get_current_time_str())
            self.active = True
            return True
        return self.active

    def exiting_market(self):
        """
        Exit once the order reaches a terminal state, OR leave the market so the run can drain.

        An order whose state never reaches completed/cancelled keeps its agent alive forever,
        which hangs HorizonDrainTermination (the agent scheduler never drains). Past the horizon
        there is no longer any way to serve such orders — all trucks have gone offline — so:

        - completed/cancelled                  → exit (normal terminal case).
        - past horizon, still created/unassigned (never startable) → leave now.
        - past horizon + POST_HORIZON_GRACE_STEPS, still non-terminal (e.g. orphaned by a
          cancelled haul trip) → leave.

        In-flight orders (assigned/in_transit/…) within the grace window are left alone so they
        can finish naturally — trucks complete their current trip before going offline.

        Leaving is a *local* shutdown only: it does NOT PATCH the order to ``cancelled``. Doing
        that per-agent does not scale — thousands of simultaneous cancels overwhelm the API and
        cannot drain within the post-horizon window. The order records are terminalized in one
        bulk update at simulation completion (SimulationRuntime._finalize_unserved_orders →
        bulk_cancel_nonterminal_orders). Shutting down here keeps the drain fast and local.
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

        # Bounded unassigned wait (UNASSIGNED_ORDER_MAX_WAIT_STEPS, 0=off): an
        # order this overdue can never be served — the backlog only grows when
        # demand exceeds fleet throughput — so terminalize it NOW (same
        # `cancelled` state the horizon cleanup gives unserved demand) instead
        # of parking a live agent + MQTT connection until the end of the run.
        # At 30k orders the accumulated park exhausted host ephemeral ports and
        # silently blocked later order agents from booting at all.
        if state == "unassigned" and self._unassigned_overdue(self.current_time_step):
            try:
                app.manager.cancel(sim_clock=self.get_current_time_str())
            except Exception:
                logging.exception(f"order {self.unique_id}: overdue cancel failed; exiting anyway")
            self.shutdown()
            return True

        horizon = int(self.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0))
        if horizon:
            grace = int(self.orsim_settings.get("POST_HORIZON_GRACE_STEPS", 60))
            # Never-startable orders can never be served once the horizon passes — leave now.
            if self.current_time_step >= horizon and state in {"created", "unassigned"}:
                self.shutdown()
                return True
            # Backstop: anything still non-terminal after the grace window leaves so the
            # scheduler can drain to zero and the run can terminate.
            if self.current_time_step >= horizon + grace:
                self.shutdown()
                return True
        return False

    # ── Sleep protocol (see OpenRideAgent) ─────────────────────────────────
    # An unassigned order is a pure event listener: its step does nothing but
    # drain the (empty) message queue while it waits for the assignment service
    # to match it — via Mongo, not via this agent. At 30k orders per run these
    # no-op replies dominated the step barrier (~9.9k of 11k replies/step
    # measured live), so a parked order tells the scheduler to stop expecting
    # it — by default indefinitely (-1), which also removes it from the step
    # broadcast fanout. Any workflow event (assignment/cancellation, delivered
    # on the app topic, which stays subscribed) wakes it on the next step — the
    # same one-step latency an awake order has. Unserved orders never wake by
    # themselves: the scheduler direct-notifies sleepers on the final step and
    # their records are terminalized by the existing finalize bulk-cancel.
    # A positive UNASSIGNED_ORDER_SLEEP_STEPS opts back into timed check-in
    # naps, capped at the horizon so post-horizon exiting_market rules run.

    def wake_signal(self) -> bool:
        app = getattr(self, "app", None)
        return bool(app is not None and getattr(app, "message_queue", None))

    def wake_signal_for_step(self, time_step: int) -> bool:
        # Also wake when the bounded unassigned wait expired, so exiting_market
        # can terminalize the order (a parked sleeper has no other wake source).
        return self.wake_signal() or self._unassigned_overdue(time_step)

    def _unassigned_overdue(self, time_step) -> bool:
        max_wait = int(self.orsim_settings.get("UNASSIGNED_ORDER_MAX_WAIT_STEPS", 0))
        if max_wait <= 0 or not isinstance(time_step, int):
            return False
        request_step = int(self.behavior.get("request_time_step", 0))
        if time_step - request_step <= max_wait:
            return False
        app = getattr(self, "app", None)
        if app is None:
            return False
        try:
            return (app.manager.as_dict() or {}).get("state") == "unassigned"
        except Exception:
            return False

    def sleep_steps_hint(self) -> int:
        if not self.active:
            return 0
        app = getattr(self, "app", None)
        if app is None or getattr(app, "message_queue", None):
            return 0
        try:
            state = (app.manager.as_dict() or {}).get("state")
        except Exception:
            return 0
        if state in {"completed", "cancelled"}:
            # Terminal: exiting_market shuts the agent down; never announce a nap.
            return 0
        if state != "unassigned":
            # Assigned/in-flight orders are ALSO pure event listeners — every
            # transition arrives on the app topic and wakes them. Event-driven
            # nap only (never timed): nothing to check in about.
            return -1
        sleep_steps = int(self.orsim_settings.get("UNASSIGNED_ORDER_SLEEP_STEPS", -1))
        if sleep_steps < 0:
            return -1
        horizon = int(self.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0))
        if horizon:
            sleep_steps = min(sleep_steps, horizon - self.current_time_step)
        return max(0, sleep_steps)

    def logout(self):
        # Orders don't hold external resources, but close hook keeps parity with other agents.
        app = getattr(self, "app", None)
        if app is not None:
            try:
                app.close(self.get_current_time_str())
            except Exception:
                pass

    def estimate_next_event_time(self):
        if not self.active:
            request_step = int(self.behavior.get("request_time_step", 0))
            interval = int(getattr(self, "orsim_settings", {}).get("STEP_INTERVAL", 30))
            return self.current_time + relativedelta(seconds=max(0, request_step) * interval)
        # Active orders are passive observers driven by haul-trip workflow events; if
        # one of those events is queued we must wake up this tick or the order state
        # never moves past `unassigned`.
        app = getattr(self, "app", None)
        if app is not None and getattr(app, "message_queue", None):
            return self.current_time
        interval = int(getattr(self, "orsim_settings", {}).get("STEP_INTERVAL", 30))
        spa = max(1, int(self.behavior.get("steps_per_action", 1)))
        return self.current_time + relativedelta(seconds=interval * spa)

    def _effective_steps_per_action(self) -> int:
        """Coarse cadence while the order agent is registered but not yet launched."""
        if not self.active:
            return max(1, int(self.behavior.get("dormant_steps_per_action", 48)))
        return max(1, int(self.behavior.get("steps_per_action", 1)))

    def process_payload(self, payload):
        """Skip heavy app work on dormant cadence ticks before the order launches."""
        action = payload.get("action") if isinstance(payload, dict) else None
        if action == "step" and not self.active:
            spa = self._effective_steps_per_action()
            if self.current_time_step % spa != 0:
                return False
        return super().process_payload(payload)

    def step(self, time_step):
        if getattr(self, "app", None) is None:
            return False
        self.app.update_current(self.get_current_time_str())
        step_only_on_events = bool(self.behavior.get("step_only_on_events", False))
        if (
            self.current_time_step % self._effective_steps_per_action() == 0
            and random() <= self.behavior.get("response_rate", 1.0)
        ):
            if step_only_on_events and self.estimate_next_event_time() > self.current_time:
                return False
            self.app.execute_step_actions(self.current_time)
            return True
        return False
