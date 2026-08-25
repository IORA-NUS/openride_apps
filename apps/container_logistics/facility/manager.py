import logging

from apps.common.resource_client_mixin import ResourceClientMixin
from apps.config import simulation_domains
from apps.container_logistics.facility.service_time import resolve_service_time
from apps.container_logistics.statemachine import (
    FacilityQueueController,
    FacilityVisitType,
    GateStateMachine,
    QueueEntry,
)
from orsim.lifecycle import ORSimManager


class FacilityManager(ResourceClientMixin, ORSimManager):
    def __init__(self, run_id, sim_clock, user, profile=None, persona=None):
        self.run_id = run_id
        self.user = user
        self.profile = profile or {}
        self.persona = {"role": "facility", **(persona or {})}
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        self.queue_controller = FacilityQueueController(gate_count=self.profile.get("gate_count", 1))
        self._gate_assignments: dict[int, QueueEntry] = {}
        service_time = resolve_service_time(self.profile, self.profile)
        data = {
            "profile": self.profile,
            "persona": self.persona,
            "num_gates": self.profile.get("gate_count", 1),
            "service_time": service_time,
            # API may still validate legacy field names until redeployed with unified schema.
            "pickup_service_time": service_time,
            "dropoff_service_time": service_time,
            "statemachine": {
                "name": GateStateMachine.__name__,
                "domain": self.simulation_domain,
            },
            "state": GateStateMachine.initial_state.name,
            "sim_clock": sim_clock,
        }
        self.resource = self.init_resource(sim_clock, data=data)

    def on_init(self):
        pass

    def login(self, sim_clock):
        """Facilities use ``GateStateMachine`` (closed/available/...), not ``WorkflowStateMachine``."""
        return self.resource

    def as_dict(self):
        return self.resource

    def get_id(self):
        return self.resource.get("_id")

    def refresh(self):
        self.resource = self.resource_get(resource_id=self.resource.get("_id"))
        return self.resource

    def open_facility(self):
        self.queue_controller.open_facility()

    def enqueue_arrival(self, truck_id, *, visit_type: FacilityVisitType | str):
        self.queue_controller.enqueue_truck(truck_id, visit_type=visit_type)

    def assign_available_gates(self) -> dict[int, QueueEntry]:
        """Returns map ``gate_index -> QueueEntry`` for assignments in this call."""
        assignments = self.queue_controller.assign_available_gates()
        for gate_index, entry in assignments.items():
            self._gate_assignments[gate_index] = entry
        return assignments

    def patch_kpi_stats(self, stats: dict) -> None:
        """Merge KPI stats into the facility profile on the REST document."""
        rid = self.get_id()
        etag = self.resource.get("_etag") if self.resource else None
        if not rid or not etag:
            return
        try:
            profile = dict(self.resource.get("profile") or {})
            profile.update(stats)
            patched = self.resource_patch(
                resource_id=rid,
                data={"profile": profile},
                etag=etag,
            )
            # Eve PATCH responses are meta-only under BANDWIDTH_SAVER (the default) — no
            # state/profile — so assigning the response straight to self.resource would wipe
            # `state` to None (same defect fixed in TruckManager.set_last_dropoff). Update in
            # place: keep the full doc, apply the merged profile, refresh only the etag/updated.
            if isinstance(patched, dict):
                self.resource["profile"] = profile
                for meta_key in ("_etag", "_updated"):
                    if patched.get(meta_key) is not None:
                        self.resource[meta_key] = patched[meta_key]
        except Exception as exc:
            logging.debug("facility patch_kpi_stats failed: %s", exc)

    def complete_gate_service(self, gate_index) -> tuple[str | None, FacilityVisitType | None]:
        entry = self.queue_controller.release_gate(gate_index)
        if entry is None:
            entry = self._gate_assignments.pop(gate_index, None)
        else:
            self._gate_assignments.pop(gate_index, None)
        if entry is None:
            return None, None
        return entry.truck_id, entry.visit_type
