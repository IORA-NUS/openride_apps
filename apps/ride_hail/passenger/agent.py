from orsim.messenger.interaction import message_handler, state_handler
import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging
import json, time, asyncio
import pika
import traceback
from random import random, randint, choice
from datetime import datetime
import geopandas as gp
from random import choice
from dateutil.relativedelta import relativedelta
from shapely.geometry import Point, mapping
from typing import Any, Dict

from .app import PassengerApp
from apps.utils.utils import id_generator #, cut
from apps.ride_hail.statemachine import RidehailPassengerTripStateMachine #, WorkflowStateMachine
# from apps.agent_core.state_machine import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine

from apps.loc_service import OSRMClient, cut

from apps.loc_service import TaxiStop, BusStop

# from apps.messenger_service import Messenger

# Passenger agent will be called to apply behavior at every step
# At each step, the Agent will process list of collected messages in the app.
from orsim.lifecycle import ORSimAgent

from apps.utils.excepions import WriteFailedException, RefreshException
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
from apps.ride_hail import RideHailActions, RideHailEvents, validate_driver_workflow_payload
# from apps.agent_core.runtime import AgentRuntimeBase
# from apps.config import orsim_settings, passenger_settings

class PassengerAgentIndie(ORSimAgent):

    current_loc = None
    current_time_step = None
    prev_time_step = None
    elapsed_duration_steps = None
    # projected_path = None # shapely.geometry.LineString

    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior): #, orsim_settings):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior) #, orsim_settings)

        self.step_size = self.orsim_settings['STEP_INTERVAL'] # NumSeconds per each step.

        self.current_loc = self.behavior['pickup_loc']
        self.pickup_loc = self.behavior['pickup_loc']
        self.dropoff_loc = self.behavior['dropoff_loc']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }
        # self.timeout_error = False
        self.failure_count = 0
        self.failure_log = {}

        try:
            self.app = PassengerApp(run_id=self.run_id,
                                    sim_clock=self.get_current_time_str(),
                                    current_loc=self.current_loc,
                                    credentials=self.credentials,
                                    profile=self.behavior['profile'],
                                    messenger=self.messenger,
                                    persona=self.behavior.get('persona', {}),
                                    agent_helper=self
                                )
            print(f"PassengerApp initialized for {self.unique_id}")

            for topic, method in self.app.topic_params.items():
                self.register_message_handler(topic=topic, method=method)

            self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

        except Exception as e:
            print(f"Exception during PassengerAgentIndie initialization: {str(e)}")

            logging.exception(f"{self.unique_id = }: {str(e)}")
            self.agent_failed = True

    def process_payload(self, payload: Dict[str, Any]) -> bool:
        did_step: bool = False

        if (payload.get("action") == "step") or (payload.get("action") == "init"):
            self.add_step_log("Before entering_market")
            print(f"Processing step for PassengerAgentIndie {self.unique_id} at time_step {payload.get('time_step')}")
            self.entering_market(payload.get("time_step"))
            self.add_step_log("After entering_market")
            print(f"Finished processing step for PassengerAgentIndie {self.unique_id} at time_step {payload.get('time_step')}")

            # if self.is_active():
            if self.active:
                try:
                    self.add_step_log("Before step")
                    did_step = self.step(payload.get("time_step"))
                    self.add_step_log("After step")
                    self.failure_count = 0
                    self.failure_log = {}
                except Exception:
                    self.failure_log[self.failure_count] = traceback.format_exc()
                    self.failure_count += 1

            self.add_step_log("Before exiting_market")
            self.exiting_market()
            self.add_step_log("After exiting_market")
        else:
            logging.error(f"{payload = }")

        return did_step

    def entering_market(self, time_step):
        # if time_step == self.behavior['trip_request_time']:
        if (self.active == False) and (time_step == self.behavior['trip_request_time']):
            # print('Enter Market')
            # print(self.behavior)
            self.app.launch(sim_clock=self.get_current_time_str(),
                            current_loc=self.current_loc,
                            pickup_loc=self.pickup_loc,
                            dropoff_loc=self.dropoff_loc,
                            trip_price=self.behavior.get('trip_price'))

            print(f"PassengerAgentIndie {self.unique_id} entered market at time_step {time_step}")
            self.active = True
            return True
        else:
            return False

    def exiting_market(self):
        failure_threshold = 3

        if self.failure_count > failure_threshold:
            logging.warning(f'Shutting down passenger {self.app.manager.get_id()} due to too many failures')
            logging.warning(json.dumps(self.failure_log, indent=2))
            self.shutdown()
            return True
        # elif self.timeout_error:
        #     self.shutdown()
        #     return True
        else:
            if self.app.exited_market:
                print(f"PassengerAgentIndie[{self.unique_id}]: Already exited market as {self.app.exited_market=}")
                return False
            elif (self.current_time_step > self.behavior['trip_request_time']) and \
                    (self.app.get_trip() is not None) and \
                    (self.app.get_trip()['state'] in [RidehailPassengerTripStateMachine.passenger_completed_trip.name,
                                                    RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,]):

                print(f"PassengerAgentIndie[{self.unique_id}]: Exiting market at time_step {self.current_time_step} with trip state {self.app.get_trip()['state']}")
                self.shutdown()
                return True

            else:
                if self.app.get_trip() is None:
                    print(f"PassengerAgentIndie[{self.unique_id}]: Not exiting market at time_step {self.current_time_step} because no {self.app.get_trip() =}")
                else:
                    print(f"PassengerAgentIndie[{self.unique_id}]: Not exiting market at time_step {self.current_time_step} with trip state {self.app.get_trip()['state']}")
                return False

    def logout(self):
        self.app.close(self.get_current_time_str(), current_loc=self.current_loc)

    def estimate_next_event_time(self):
        ''' '''
        # return self.current_time
        next_event_time =  min(self.app.manager.estimate_next_event_time(self.current_time),
                                self.app.trip.estimate_next_event_time(self.current_time))

        return next_event_time

    def step(self, time_step):
        self.add_step_log(f'In step')
        self.app.update_current(self.get_current_time_str(), self.current_loc)

        if (self.current_time_step % self.behavior['steps_per_action'] == 0) and \
                    (random() <= self.behavior['response_rate']) and \
                    (self.next_event_time <= self.current_time):

            self.app.execute_step_actions(self.current_time, add_step_log_fn=self.add_step_log)

            return True
        else:
            return False



if __name__ == '__main__':

    agent = PassengerAgentIndie('001', None)
    agent.step()


