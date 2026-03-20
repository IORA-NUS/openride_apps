from apps.config import settings
from apps.utils import id_generator, is_success
from apps.agent_core.base_manager import BaseManager
from apps.common.resource_client_mixin import ResourceClientMixin
import requests, json
# from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine


class VehicleManager(ResourceClientMixin, BaseManager):

    def __init__(self, run_id, sim_clock, user, profile=None, persona=None):
        self.run_id = run_id
        self.user = user
        self.profile = profile
        self.persona = persona
        # self.resource_type = 'vehicle'
        data = {
            "registration": {
                "num": id_generator(6),
                "country": "Singapore",
                "expiry":  "Tue, 01 Jan 2030 00:00:00 GMT"
            },
            "capacity": 4,
            "statemachine": {
                "name": "WorkflowStateMachine",
                "domain": "ride_hail",
            },
            "state": WorkflowStateMachine().initial_state.name,
            "persona": self.persona,
            "sim_clock": sim_clock,
        }
        if self.profile:
            data["profile"] = self.profile
        self.resource = self.init_resource(sim_clock, data=data)


    # init_vehicle and create_vehicle are now handled by BaseManager's init_resource and create_resource
