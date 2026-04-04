from apps.common.resource_client_mixin import ResourceClientMixin
from apps.config import simulation_domains
from apps.container_logistics.statemachine import OrderStateMachine
from orsim.lifecycle import ORSimManager


class OrderManager(ResourceClientMixin, ORSimManager):
    def __init__(self, run_id, sim_clock, user, profile=None, persona=None):
        self.run_id = run_id
        self.user = user
        self.profile = profile or {}
        self.persona = {"role": "order", **(persona or {})}
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        data = {
            "profile": self.profile,
            "persona": self.persona,
            "statemachine": {
                "name": OrderStateMachine.__name__,
                "domain": self.simulation_domain,
            },
            "state": OrderStateMachine.initial_state.name,
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

    def create_order(self, payload):
        return payload

    def assign_to_truck(self, truck_id):
        return {"truck_id": truck_id}

    def mark_pickup_started(self):
        return "pickup_started"

    def mark_pickup_done(self):
        return "pickup_done"

    def mark_dropoff_started(self):
        return "dropoff_started"

    def mark_delivered(self):
        return "delivered"

    def cancel(self):
        return "cancelled"
