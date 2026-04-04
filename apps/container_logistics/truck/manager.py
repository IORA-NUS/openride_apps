from apps.common.resource_client_mixin import ResourceClientMixin
from apps.config import simulation_domains
from orsim.lifecycle import ORSimManager
from orsim.utils import WorkflowStateMachine


class TruckManager(ResourceClientMixin, ORSimManager):
    def __init__(self, run_id, sim_clock, user, profile=None, persona=None):
        self.run_id = run_id
        self.user = user
        self.profile = profile or {}
        self.persona = {"role": "truck", **(persona or {})}
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")

        data = {
            "profile": self.profile,
            "persona": self.persona,
            "statemachine": {
                "name": "WorkflowStateMachine",
                "domain": self.simulation_domain,
            },
            "state": WorkflowStateMachine().initial_state.name,
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

    def is_assignable(self, active_trip=None):
        return self.resource.get("state") == WorkflowStateMachine.online.name and active_trip is None
