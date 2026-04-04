from apps.common.resource_client_mixin import ResourceClientMixin
from apps.config import simulation_domains
from apps.container_logistics.statemachine import FacilityQueueController
from orsim.lifecycle import ORSimManager


class FacilityManager(ResourceClientMixin, ORSimManager):
    def __init__(self, run_id, sim_clock, user, profile=None, persona=None):
        self.run_id = run_id
        self.user = user
        self.profile = profile or {}
        self.persona = {"role": "facility", **(persona or {})}
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        self.queue_controller = FacilityQueueController(gate_count=self.profile.get("gate_count", 1))
        self._gate_leg = {}
        data = {
            "profile": self.profile,
            "persona": self.persona,
            "num_gates": self.profile.get("gate_count", 1),
            "pickup_service_time": self.profile.get("pickup_service_time", 0),
            "dropoff_service_time": self.profile.get("dropoff_service_time", 0),
            "sim_clock": sim_clock,
        }
        self.resource = self.init_resource(sim_clock, data=data)

    def on_init(self):
        pass

    def as_dict(self):
        return self.resource

    def get_id(self):
        return self.resource.get("_id")

    def refresh(self):
        self.resource = self.resource_get(resource_id=self.resource.get("_id"))
        return self.resource

    def open_facility(self):
        self.queue_controller.open_facility()

    def enqueue_arrival(self, truck_id):
        self.queue_controller.enqueue_truck(truck_id)

    def assign_waiting_trucks(self, is_pickup_leg):
        assignments = self.queue_controller.assign_waiting_trucks(is_pickup_leg=is_pickup_leg)
        for gate_index in assignments:
            self._gate_leg[gate_index] = is_pickup_leg
        return assignments

    def complete_gate_service(self, gate_index):
        truck_id = self.queue_controller.release_gate(gate_index)
        is_pickup_leg = self._gate_leg.pop(gate_index, None)
        return truck_id, is_pickup_leg
