from orsim.lifecycle import ORSimApp
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
import json
from datetime import timedelta

from apps.common.user_registry import UserRegistry
from apps.container_logistics.message_data_models import FacilityWorkflowPayload
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    FacilityVisitType,
    GateStateMachine,
    haultrip_gate_interactions,
)

from apps.utils import str_to_time
from apps.utils.step_profile import span, tick

from .facility_snapshot_publisher import FacilitySnapshotPublisher
from .haultrip_interaction_mixin import HaulTripInteractionMixin
from .manager import FacilityManager
from .service_time import resolve_service_time


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
            "service_time": {"type": "integer", "required": False},
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
        # gate_index -> service end time (datetime)
        self._gate_service_ends = {}
        self._facility_stream = None
        self._facility_refresh_pending = True
        # A snapshot publish only marks the REST KPI doc dirty; the blocking PATCH is
        # coalesced to at most one per step (see _flush_kpi_stats).
        self._kpi_patch_pending = False

    def _service_time_seconds(self) -> int:
        profile = self.behavior.get("profile") or {}
        return resolve_service_time(self.behavior, profile)

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
        self._init_facility_stream()
        self._publish_facility_snapshot(force=True)

    def update_current(self, sim_clock_gmt):
        self.current_time = str_to_time(sim_clock_gmt)
        self.current_time_str = sim_clock_gmt
        self.latest_sim_clock = sim_clock_gmt

    def invalidate_facility_cache(self) -> None:
        """Mark the facility resource cache stale so the next refresh fetches it."""
        self._facility_refresh_pending = True

    def refresh(self):
        if self._facility_refresh_pending:
            self.manager.refresh()
            self._facility_refresh_pending = False

    def _stream_enabled(self) -> bool:
        profile = self.behavior.get("profile") or {}
        return bool(profile.get("publish_facility_stream_kafka", True))

    def _init_facility_stream(self) -> None:
        if not self._stream_enabled():
            self._facility_stream = None
            return
        if self._facility_stream is not None:
            return
        profile = self.behavior.get("profile") or self.behavior
        facility_id = str(self.manager.get_id() or "")
        if not facility_id:
            return
        self._facility_stream = FacilitySnapshotPublisher(
            self.run_id,
            facility_id,
            profile,
            user=self.user,
        )

    def _publish_facility_snapshot(self, *, force: bool = False) -> None:
        if self._facility_stream is None or self.current_time_str is None:
            return
        with span("facility.snapshot_call"):
            self._publish_facility_snapshot_inner(force=force)

    def _publish_facility_snapshot_inner(self, *, force: bool = False) -> None:
        published = self._facility_stream.maybe_publish(
            self.manager,
            self.behavior,
            self.current_time_str,
            force=force,
        )
        if published:
            # Do NOT patch here. The PATCH is ~91 ms of blocking eventlet-scheduled HTTP and
            # fired 2.57x per facility tick (~98% of the tick). The two scalars it writes
            # (avg_queue_wait_seconds, peak_queue_length) are only read by the analytics
            # manager on a much slower cadence, and kpi_stats() is recomputed at flush time,
            # so coalescing to one last-write-wins PATCH per step is lossless.
            self._kpi_patch_pending = True

    def _flush_kpi_stats(self) -> None:
        """Persist the latest queue KPI scalars to the facility REST document, once.

        No-op unless a snapshot was published since the last flush. Clearing the flag
        before the call keeps the bound at one PATCH attempt per flush; a later publish
        re-arms it, and kpi_stats() is always read fresh so the newest value wins.
        """
        if not self._kpi_patch_pending:
            return
        self._kpi_patch_pending = False
        if self._facility_stream is None:
            return
        with span("facility.patch_kpi"):
            self.manager.patch_kpi_stats(self._facility_stream.kpi_stats())

    def enqueue_arrival(self, truck_id, *, visit_type: FacilityVisitType | str):
        """Add truck to FIFO gate queue; assignment runs on the next facility tick."""
        self.manager.enqueue_arrival(truck_id, visit_type=visit_type)
        if self._facility_stream and self.current_time_str:
            self._facility_stream.record_enqueue(
                truck_id, visit_type, self.current_time_str
            )
        self._publish_facility_snapshot(force=True)
        return {}

    def complete_gate_service(self, gate_index):
        truck_id, visit_type = self.manager.complete_gate_service(gate_index)
        self._gate_service_ends.pop(gate_index, None)
        if truck_id is not None and visit_type is not None:
            if self._facility_stream and self.current_time_str:
                self._facility_stream.record_service_complete(
                    self.current_time_str,
                    truck_id=truck_id,
                    visit_type=visit_type,
                )
            self._publish_gate_service_completed(
                truck_id=truck_id,
                gate_index=gate_index,
                visit_type=visit_type,
            )
        self._publish_facility_snapshot()
        return truck_id

    def _gate_event_for_visit(self, visit_type: FacilityVisitType, *, assigned: bool):
        if visit_type == FacilityVisitType.PICKUP:
            return (
                ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_PICKUP
                if assigned
                else ContainerLogisticsEvents.PICKUP_GATE_SERVICE_COMPLETED
            )
        return (
            ContainerLogisticsEvents.GATE_SLOT_ASSIGNED_FOR_DROPOFF
            if assigned
            else ContainerLogisticsEvents.DROPOFF_GATE_SERVICE_COMPLETED
        )

    def _publish_gate_assignment(self, truck_id, gate_index, visit_type: FacilityVisitType):
      with span("facility.mqtt_publish"):
        event = self._gate_event_for_visit(visit_type, assigned=True)
        service_time = self._service_time_seconds()
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
                        "visit_type": visit_type.value,
                    },
                }
            ),
        )

    def _publish_gate_service_completed(self, truck_id, gate_index, visit_type: FacilityVisitType):
      with span("facility.mqtt_publish"):
        event = self._gate_event_for_visit(visit_type, assigned=False)
        service_time = self._service_time_seconds()
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
                        "visit_type": visit_type.value,
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

    def _allocate_gates(self):
        service_time = self._service_time_seconds()
        assignments = self.manager.assign_available_gates()
        for gate_index, entry in assignments.items():
            if self.current_time is not None and service_time > 0:
                self._gate_service_ends[gate_index] = self.current_time + timedelta(
                    seconds=service_time
                )
            self._publish_gate_assignment(
                truck_id=entry.truck_id,
                gate_index=gate_index,
                visit_type=entry.visit_type,
            )

    def perform_workflow_actions(self):
        self._allocate_gates()

        if self.current_time is None:
            return

        due_gate_indices = [
            gate_index
            for gate_index, end_time in list(self._gate_service_ends.items())
            if end_time is not None and self.current_time >= end_time
        ]
        for gate_index in due_gate_indices:
            self.complete_gate_service(gate_index)

        if due_gate_indices:
            self._allocate_gates()

        self._publish_facility_snapshot(
            force=bool(self._gate_service_ends) or bool(due_gate_indices)
        )

    def has_pending_gate_work(self) -> bool:
        """True while messages, queue entries, or gate service remain."""
        if getattr(self, "message_queue", None):
            return True
        controller = getattr(self.manager, "queue_controller", None)
        if controller is None:
            return False
        if getattr(controller, "queue", None):
            return True
        if controller.active_truck_ids():
            return True
        return bool(getattr(self, "_gate_service_ends", None))

    def close(self, sim_clock):
        self.update_current(sim_clock)
        self._publish_facility_snapshot(force=True)
        # Last chance to persist the final queue KPIs to the REST document.
        self._flush_kpi_stats()
        super().close(sim_clock)

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        with span("facility.tick"):
            self.current_time = current_time
            self.current_time_str = current_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
            with span("facility.refresh"):
                self.refresh()
            with span("facility.consume_1"):
                self.consume_messages()
            with span("facility.workflow"):
                self.perform_workflow_actions()
            # Drain messages published by peers in the same scheduler tick (e.g. facility → truck).
            with span("facility.consume_2"):
                self.consume_messages()
            # One coalesced REST PATCH per step, covering every publish since the last
            # flush - including any triggered between ticks by an inbound MQTT message.
            self._flush_kpi_stats()
        tick("facility.tick")
