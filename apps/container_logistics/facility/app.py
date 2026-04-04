from orsim.lifecycle import ORSimApp
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
import json

from apps.common.user_registry import UserRegistry
from apps.container_logistics.message_data_models import FacilityWorkflowPayload
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    GateStateMachine,
    haultrip_gate_interactions,
)

from .haultrip_interaction_mixin import HaulTripInteractionMixin
from .manager import FacilityManager


class FacilityApp(ORSimApp, HaulTripInteractionMixin):
    @property
    def managed_statemachine(self):
        return GateStateMachine

    @property
    def interaction_ground_truth_list(self):
        return [haultrip_gate_interactions]

    @property
    def runtime_behavior_schema(self):
        return {
            "gate_count": {"type": "integer", "required": True},
            "pickup_service_time": {"type": "integer", "required": False},
            "dropoff_service_time": {"type": "integer", "required": False},
        }

    def __init__(self, run_id, sim_clock, behavior, messenger, agent_helper=None):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
            agent_helper=agent_helper,
        )
        self.current_time = None
        self.current_time_str = None
        self.latest_sim_clock = sim_clock
        self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def _create_manager(self):
        return FacilityManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile", self.behavior),
            persona=self.behavior.get("persona", {}),
        )

    def launch(self, sim_clock):
        super().launch(sim_clock)
        self.manager.open_facility()

    def refresh(self):
        self.manager.refresh()

    def enqueue_arrival(self, truck_id, is_pickup_leg):
        self.manager.enqueue_arrival(truck_id)
        assignments = self.manager.assign_waiting_trucks(is_pickup_leg=is_pickup_leg)
        for gate_index, assigned_truck in assignments.items():
            self._publish_gate_assignment(
                truck_id=assigned_truck,
                gate_index=gate_index,
                is_pickup_leg=is_pickup_leg,
            )
        return assignments

    def complete_gate_service(self, gate_index):
        truck_id, is_pickup_leg = self.manager.complete_gate_service(gate_index)
        if truck_id is not None and is_pickup_leg is not None:
            self._publish_gate_service_completed(
                truck_id=truck_id,
                gate_index=gate_index,
                is_pickup_leg=is_pickup_leg,
            )
        return truck_id

    def _publish_gate_assignment(self, truck_id, gate_index, is_pickup_leg):
        event = (
            ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_PICKUP
            if is_pickup_leg
            else ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_DROPOFF
        )
        service_time = self.behavior.get(
            "pickup_service_time" if is_pickup_leg else "dropoff_service_time", 0
        )
        self.messenger.client.publish(
            f"{self.run_id}/{truck_id}",
            json.dumps(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": truck_id,
                    "data": {
                        "event": event,
                        "gate_index": gate_index,
                        "service_time": service_time,
                    },
                }
            ),
        )

    def _publish_gate_service_completed(self, truck_id, gate_index, is_pickup_leg):
        event = (
            ContainerLogisticsEvents.PICKUP_GATE_SERVICE_COMPLETED
            if is_pickup_leg
            else ContainerLogisticsEvents.DROPOFF_GATE_SERVICE_COMPLETED
        )
        service_time = self.behavior.get(
            "pickup_service_time" if is_pickup_leg else "dropoff_service_time", 0
        )
        self.messenger.client.publish(
            f"{self.run_id}/{truck_id}",
            json.dumps(
                {
                    "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
                    "truck_id": truck_id,
                    "data": {
                        "event": event,
                        "gate_index": gate_index,
                        "service_time": service_time,
                    },
                }
            ),
        )

    def handle_app_topic_messages(self, payload):
        if payload.get("action") == ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT:
            self.enqueue_message(payload)
            return
        self.enqueue_message(payload)

    def consume_messages(self):
        payload = self.dequeue_message()
        while payload is not None:
            parsed = FacilityWorkflowPayload.parse(payload)
            if parsed is None:
                payload = self.dequeue_message()
                continue
            self._interaction_plugin.on_message(
                InteractionContext(
                    action=parsed.action,
                    event=parsed.data.get("event"),
                    payload=payload,
                    data=parsed.data,
                )
            )
            payload = self.dequeue_message()

    def perform_workflow_actions(self):
        pass

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        self.current_time = current_time
        self.current_time_str = current_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.refresh()
        self.consume_messages()
        self.perform_workflow_actions()
