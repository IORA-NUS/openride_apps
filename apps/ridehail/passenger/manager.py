import logging, traceback
import requests, json
from http import HTTPStatus
from dateutil.relativedelta import relativedelta

from apps.config import settings, simulation_domains
from apps.utils import id_generator, is_success
from orsim.lifecycle import ORSimManager
from apps.common.resource_client_mixin import ResourceClientMixin
# from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine


class PassengerManager(ResourceClientMixin, ORSimManager):

    def __init__(self, run_id, sim_clock, user, profile, persona):
        self.resource_type = 'passenger'
        self.run_id = run_id
        self.user = user
        self.profile = profile
        self.persona = persona
        self.simulation_domain = simulation_domains['ridehail']

        data = {
            "profile": self.profile,
            "persona": self.persona,
            "statemachine": {
                "name": "WorkflowStateMachine",
                "domain": self.simulation_domain,
            },
            "state": WorkflowStateMachine().initial_state.name,
            "sim_clock": sim_clock
        }
        self.resource = self.init_resource(sim_clock, data=data)

    def on_init(self):
        pass


