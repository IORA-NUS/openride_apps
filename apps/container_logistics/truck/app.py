from orsim.lifecycle import ORSimApp
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
from orsim.utils import WorkflowStateMachine

from apps.common.user_registry import UserRegistry
from apps.container_logistics.message_data_models import (
    AssignedHaulTripPayload,
    FacilityWorkflowPayload,
    OrderWorkflowPayload,
)
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    HaulTripStateMachine,
    haultrip_gate_interactions,
    haultrip_order_interactions,
)

from .facility_interaction_mixin import FacilityInteractionMixin
from .manager import TruckManager
from .order_interaction_mixin import OrderInteractionMixin
from .trip_manager import TruckTripManager


class TruckApp(ORSimApp, OrderInteractionMixin, FacilityInteractionMixin):
    exited_market = False

    @property
    def managed_statemachine(self):
        return HaulTripStateMachine

    @property
    def interaction_ground_truth_list(self):
        return [haultrip_order_interactions, haultrip_gate_interactions]

    @property
    def runtime_behavior_schema(self):
        return {
            "init_loc": {"type": "dict", "required": True},
            "shift_start_time": {"type": "integer", "required": True},
            "shift_end_time": {"type": "integer", "required": True},
            "steps_per_action": {"type": "integer", "required": False},
            "response_rate": {"type": "number", "required": False},
        }

    def __init__(self, run_id, sim_clock, behavior, messenger, agent_helper=None):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
            agent_helper=agent_helper,
        )
        self.trip = self.create_trip_manager()
        self.current_time = None
        self.current_time_str = None
        self.current_loc = self.behavior.get("init_loc")
        self.latest_loc = self.current_loc
        self.latest_sim_clock = sim_clock
        self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def _create_manager(self):
        return TruckManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile", {}),
            persona=self.behavior.get("persona", {}),
        )

    def create_trip_manager(self):
        return TruckTripManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            messenger=self.messenger,
            persona=self.behavior.get("persona", {}),
        )

    def launch(self, sim_clock):
        super().launch(sim_clock)

    def close(self, sim_clock):
        if self.trip.as_dict() is not None:
            self.trip.cancel(sim_clock, current_loc=self.current_loc)
        super().close(sim_clock)

    def refresh(self):
        self.manager.refresh()
        if self.trip.as_dict() is not None:
            self.trip.refresh()

    def get_truck(self):
        return self.manager.as_dict()

    def get_manager(self):
        return self.manager.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def create_new_haul_trip(self, sim_clock, current_loc, truck, order):
        return self.trip.create_new_trip(sim_clock, current_loc, truck, order)

    def handle_assignment(self, sim_clock, current_loc, order):
        if not self.manager.is_assignable(active_trip=self.trip.as_dict()):
            return None
        trip = self.create_new_haul_trip(sim_clock, current_loc, self.get_truck(), order)
        self.trip.assign(sim_clock, current_loc=current_loc, order=order)
        return trip

    def handle_app_topic_messages(self, payload):
        if payload.get("action") == ContainerLogisticsActions.ASSIGNED_HAUL_TRIP:
            parsed = AssignedHaulTripPayload.parse(payload)
            if parsed is None:
                return
            if parsed.truck_id is not None and parsed.truck_id != self.manager.get_id():
                return
            self.handle_assignment(self.latest_sim_clock, self.latest_loc, parsed.order)
            return
        self.enqueue_message(payload)

    def consume_messages(self):
        payload = self.dequeue_message()
        while payload is not None:
            parsed_data = payload.get("data")
            if payload.get("action") == ContainerLogisticsActions.ORDER_WORKFLOW_EVENT:
                parsed = OrderWorkflowPayload.parse(payload)
                if parsed is None:
                    payload = self.dequeue_message()
                    continue
                parsed_data = parsed.data
            elif payload.get("action") == ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT:
                parsed = FacilityWorkflowPayload.parse(payload)
                if parsed is None:
                    payload = self.dequeue_message()
                    continue
                parsed_data = parsed.data
            self._interaction_plugin.on_message(
                InteractionContext(
                    action=payload.get("action"),
                    event=(parsed_data or {}).get("event", payload.get("event")),
                    payload=payload,
                    data=parsed_data,
                )
            )
            payload = self.dequeue_message()

    def perform_workflow_actions(self):
        if self.get_truck().get("state") != WorkflowStateMachine.online.name:
            raise Exception(f"Truck not available for workflow actions: {self.get_truck().get('state')}")
        trip = self.get_trip()
        if trip is None:
            return
        self._interaction_plugin.on_state(
            InteractionContext(
                state=trip.get("state"),
                extra={"time_since_last_event": 0},
            )
        )

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        self.current_time = current_time
        self.current_time_str = current_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.refresh()
        self.consume_messages()
        self.perform_workflow_actions()
