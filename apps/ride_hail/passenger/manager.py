import logging, traceback
import requests, json
from http import HTTPStatus
from dateutil.relativedelta import relativedelta

from apps.config import settings
from apps.utils import id_generator, is_success
from apps.agent_core.base_manager import BaseManager
from apps.common.resource_client_mixin import ResourceClientMixin
from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine

# from apps.utils.user_registry import UserRegistry

class PassengerManager(ResourceClientMixin, BaseManager):

    def __init__(self, run_id, sim_clock, user, passenger_profile):
        self.resource_type = 'passenger'
        self.run_id = run_id
        self.user = user
        self.passenger_profile = passenger_profile

        data = {
            "profile": self.passenger_profile,
            "statemachine": {
                "name": "WorkflowStateMachine",
                "domain": "ride_hail",
            },
            "state": WorkflowStateMachine().initial_state.name,
            "sim_clock": sim_clock
        }
        self.resource = self.init_resource(sim_clock, data=data)



    # init_passenger is now handled by BaseManager's init_resource

    # create_passenger is now handled by BaseManager's create_resource

