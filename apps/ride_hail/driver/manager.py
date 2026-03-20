from dateutil.relativedelta import relativedelta
import requests, json, logging, traceback
from http import HTTPStatus

from apps.config import settings
from apps.utils import id_generator, is_success

from apps.agent_core.base_manager import BaseManager
from apps.common.resource_client_mixin import ResourceClientMixin
from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine
from apps.ride_hail.vehicle.manager import VehicleManager


class DriverManager(ResourceClientMixin, BaseManager):

    def __init__(self, run_id, sim_clock, user, profile):
        self.run_id = run_id
        self.user = user
        self.profile = profile
        self.resource_type = 'driver'

        data = {
            "license": {
                "num": id_generator(),
                "country": "Singapore",
                "expiry":  "Tue, 01 Jan 2030 00:00:00 GMT"
            },
            "profile": self.profile,
            "statemachine": {
                "name": "WorkflowStateMachine",
                "domain": "ride_hail",
            },
            "state": WorkflowStateMachine().initial_state.name,
            "sim_clock": sim_clock,
        }
        print(f"DriverManager.__init__: Initializing driver with data: {data}")
        self.resource = self.init_resource(sim_clock, data=data)

        self.vehicle = VehicleManager(run_id, sim_clock, user, profile={})



    # init_driver is now handled by BaseManager's init_resource


    # create_driver is now handled by BaseManager's create_resource




    # Vehicle logic is now handled by the Vehicle class
