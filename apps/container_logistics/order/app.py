from orsim.lifecycle import ORSimApp
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext, message_handler

from apps.container_logistics.statemachine import ContainerLogisticsActions, ContainerLogisticsEvents
from apps.container_logistics.statemachine import haultrip_order_interactions

from apps.common.user_registry import UserRegistry

from .manager import OrderManager
from apps.container_logistics.statemachine import OrderStateMachine


class OrderApp(ORSimApp):
    @property
    def managed_statemachine(self):
        return OrderStateMachine

    @property
    def interaction_ground_truth_list(self):
        # Order lifecycle is driven by workflow events emitted from haul trips (and optionally other services).
        return [haultrip_order_interactions]

    @property
    def runtime_behavior_schema(self):
        # Minimal runtime behavior needed to instantiate an order agent.
        return {
            "request_time_step": {"type": "integer", "required": False},
            "steps_per_action": {"type": "integer", "required": False},
            "response_rate": {"type": "number", "required": False},
            "step_only_on_events": {"type": "boolean", "required": False},
        }

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def _create_manager(self):
        return OrderManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile", {}),
            persona=self.behavior.get("persona", {}),
        )

    def __init__(self, run_id, sim_clock, behavior, messenger, agent_helper=None):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
            agent_helper=agent_helper,
        )
        self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

    def launch(self, sim_clock):
        super().launch(sim_clock)
        # Orders are created in `created`; publish them to become available for assignment.
        try:
            self.manager.publish(sim_clock=sim_clock)
        except Exception:
            # Publishing is best-effort; if the statemachine definition isn't registered yet,
            # the order can still be advanced later by workflow events.
            pass

    def handle_app_topic_messages(self, payload):
        # Pass-through to the normal queue; the scheduler will call this for topic messages.
        self.enqueue_message(payload)

    @message_handler(ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, ContainerLogisticsEvents.ORDER_ASSIGNED_TO_TRUCK)
    def _on_order_assigned(self, payload, data):
        order_id = (data or {}).get("order_id") or payload.get("order_id")
        if order_id and order_id != self.manager.get_id():
            return
        truck_id = (data or {}).get("truck_id") or payload.get("truck_id")
        self.manager.assign_to_truck(sim_clock=self.latest_sim_clock, truck_id=truck_id)

    @message_handler(ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, ContainerLogisticsEvents.ORDER_PICKUP_STARTED)
    def _on_pickup_started(self, payload, data):
        order_id = (data or {}).get("order_id") or payload.get("order_id")
        if order_id and order_id != self.manager.get_id():
            return
        self.manager.mark_pickup_started(sim_clock=self.latest_sim_clock)

    @message_handler(ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, ContainerLogisticsEvents.ORDER_PICKUP_COMPLETED)
    def _on_pickup_completed(self, payload, data):
        order_id = (data or {}).get("order_id") or payload.get("order_id")
        if order_id and order_id != self.manager.get_id():
            return
        self.manager.mark_pickup_done(sim_clock=self.latest_sim_clock)

    @message_handler(ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, ContainerLogisticsEvents.ORDER_DROPOFF_STARTED)
    def _on_dropoff_started(self, payload, data):
        order_id = (data or {}).get("order_id") or payload.get("order_id")
        if order_id and order_id != self.manager.get_id():
            return
        self.manager.mark_dropoff_started(sim_clock=self.latest_sim_clock)

    @message_handler(ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, ContainerLogisticsEvents.ORDER_DELIVERED)
    def _on_delivered(self, payload, data):
        order_id = (data or {}).get("order_id") or payload.get("order_id")
        if order_id and order_id != self.manager.get_id():
            return
        self.manager.mark_delivered(sim_clock=self.latest_sim_clock)

    @message_handler(ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, ContainerLogisticsEvents.ORDER_CANCELLED)
    def _on_cancelled(self, payload, data):
        order_id = (data or {}).get("order_id") or payload.get("order_id")
        if order_id and order_id != self.manager.get_id():
            return
        self.manager.cancel(sim_clock=self.latest_sim_clock)

    def consume_messages(self):
        payload = self.dequeue_message()
        while payload is not None:
            data = payload.get("data") or {}
            self._interaction_plugin.on_message(
                InteractionContext(
                    action=payload.get("action"),
                    event=data.get("event", payload.get("event")),
                    payload=payload,
                    data=data,
                )
            )
            payload = self.dequeue_message()

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        # Mirror TruckApp's timestamp format for `sim_clock`.
        self.current_time = current_time
        self.current_time_str = current_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.latest_sim_clock = self.current_time_str
        self.consume_messages()
