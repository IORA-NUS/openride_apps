from dateutil.relativedelta import relativedelta
import requests, json, logging, traceback
from http import HTTPStatus

from apps.config import settings, simulation_domains
from apps.utils import id_generator, is_success

from orsim.lifecycle import ORSimManager
from apps.common.resource_client_mixin import ResourceClientMixin
# from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine
from apps.ridehail.vehicle.manager import VehicleManager


class DriverManager(ResourceClientMixin, ORSimManager):

    def __init__(self, run_id, sim_clock, user, profile, persona):
        self.run_id = run_id
        self.user = user
        self.profile = profile
        self.persona = persona
        # self.resource_type = 'driver'
        self.simulation_domain = simulation_domains['ridehail']

        data = {
            "license": {
                "num": id_generator(),
                "country": "Singapore",
                "expiry":  "Tue, 01 Jan 2030 00:00:00 GMT"
            },
            "profile": self.profile,
            "statemachine": {
                "name": "WorkflowStateMachine",
                "domain": self.simulation_domain,
            },
            "state": WorkflowStateMachine().initial_state.name,
            "persona": self.persona,
            "sim_clock": sim_clock,
        }
        print(f"DriverManager.__init__: Initializing driver with data: {data}")
        self.resource = self.init_resource(sim_clock, data=data)

        vehicle_persona = {
            "role": "vehicle",
            "domain": self.persona.get("domain"),
        }
        self.vehicle = VehicleManager(run_id, sim_clock, user, profile={}, persona=vehicle_persona)

    def on_init(self):
        pass

