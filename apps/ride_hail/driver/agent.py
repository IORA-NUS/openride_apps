import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from re import I
from shapely.geometry.linestring import LineString
# from apps.state_machine.agent_workflow_sm import WorkflowStateMachine
# from apps.agent_core.state_machine import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine
# from orsim.messenger.interaction import message_handler, state_handler


import logging, traceback
import json, time, asyncio
import pika
from datetime import datetime
import haversine as hs
import geopandas as gp
from random import choice, randint, random
from dateutil.relativedelta import relativedelta

from shapely.geometry import Point, mapping

from .app import DriverApp
from apps.utils.utils import id_generator, str_to_time #, cut
from apps.ride_hail.statemachine import RidehailDriverTripStateMachine
from apps.loc_service import OSRMClient
# from apps.utils.generate_behavior import GenerateBehavior
from apps.ride_hail.scenario import GenerateBehavior

from apps.loc_service import TaxiStop, BusStop, cut, cut_route, create_route
from typing import Any, Dict

# from apps.messenger_service import Messenger

from orsim.lifecycle import ORSimAgent

from apps.utils.excepions import WriteFailedException, RefreshException
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
# from apps.ride_hail import RideHailActions, RideHailEvents, validate_passenger_workflow_payload
# from apps.agent_core.runtime import AgentRuntimeBase
# from apps.config import driver_settings, orsim_settings

class DriverAgentIndie(ORSimAgent):


    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior)
        self.current_loc = self.behavior['init_loc']
        # self.action_when_free = behavior.get('action_when_free', 'random_walk')
        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }
        self.failure_count = 0
        self.failure_log = {}
        try:
            self.app = DriverApp(
                run_id=self.run_id,
                sim_clock=self.get_current_time_str(),
                current_loc=self.current_loc,
                behavior=self.behavior,
                # credentials=self.credentials,
                # profile=self.behavior['profile'],
                messenger=self.messenger,
                # persona=self.behavior.get('persona', {}),
                agent_helper=self,
            )
            for topic, method in self.app.topic_params.items():
                self.register_message_handler(topic=topic, method=method)

        except Exception as e:
            logging.exception(f"{self.unique_id = }: {str(e)}")
            self.agent_failed = True



    # def get_random_location(self):
    #     return GenerateBehavior.get_random_location(self.behavior['coverage_area_name'])

    def process_payload(self, payload: Dict[str, Any]) -> bool:
        did_step: bool = False

        if (payload.get("action") == "step") or (payload.get("action") == "init"):
            self.add_step_log("Before entering_market")
            self.entering_market(payload.get("time_step"))
            self.add_step_log("After entering_market")
            # print(f"DriverAgentIndie[{self.unique_id}]: Completed entering_market with {self.app.get_trip()=}")

            # if self.is_active():
            if self.active:
                # print(f"DriverAgentIndie[{self.unique_id}]: Agent is active, processing step with payload {payload=}")
                try:
                    self.add_step_log("Before step")
                    did_step = self.step(payload.get("time_step"))
                    self.add_step_log("After step")
                    self.failure_count = 0
                    self.failure_log = {}
                except Exception as e:
                    print(f"Exception in step for driver {self.unique_id}: {str(e)}")
                    self.failure_log[self.failure_count] = traceback.format_exc()
                    self.failure_count += 1
            else:
                print(f"DriverAgentIndie[{self.unique_id}]: Agent is not active, checking exiting_market with {self.app.get_trip()=}")

            self.add_step_log("Before exiting_market")
            self.exiting_market()
            self.add_step_log("After exiting_market")
            # print(f"DriverAgentIndie[{self.unique_id}]: Completed exiting_market with {self.app.get_trip()=}")
        else:
            logging.error(f"{payload = }")

        # print(f"process_payload for driver {self.unique_id} completed with {self.step_log =}")
        return did_step


    def entering_market(self, time_step):
        ''' '''
        # if time_step == self.behavior['shift_start_time']:
        if (self.active == False) and (time_step == self.behavior['shift_start_time']):
            # self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
            print(f"DriverAgentIndie[{self.unique_id}]: Entering market at time_step {time_step}")
            try:
                self.app.launch(sim_clock=self.get_current_time_str(),
                                current_loc=self.current_loc,
                )
                self.active = True
            except Exception as e:
                logging.exception(f"Failed to launch DriverApp for agent {self.unique_id}: {str(e)}")
                self.agent_failed = True
                return False
            print(f"DriverAgentIndie[{self.unique_id}]: DriverApp launch successful, {self.active = }")
            return True
        elif self.active == True:
            print(f"DriverAgentIndie[{self.unique_id}]: Already active in market at {time_step = } with trip state {self.app.get_trip()['state'] if self.app.get_trip() else 'No Trip'}")
            return True
        else:
            print(f"DriverAgentIndie[{self.unique_id}]: Not entering market at {time_step = } because {self.active = } and shift_start_time={self.behavior['shift_start_time']}")
            return False

    # def is_active(self):
    #     return self.active

    def exiting_market(self):
        ''' '''
        failure_threshold = 3
        if self.failure_count > failure_threshold:
            print(f"DriverAgentIndie[{self.unique_id}]: Failure count {self.failure_count} exceeded threshold {failure_threshold}. Logging out.")
            logging.warning(f'Shutting down driver {self.app.manager.get_id()} due to too many failures')
            logging.warning(json.dumps(self.failure_log, indent=2))
            self.shutdown()
            return True
        # elif self.timeout_error:
        #     self.shutdown()
        #     return True
        else:
            # if self.app.get_trip() is None:
            #     return False
            if self.app.exited_market:
                print(f"DriverAgentIndie[{self.unique_id}]: Already exited market as {self.app.exited_market=}")
                return False
            elif (self.current_time_step > self.behavior['shift_end_time']) and \
                        (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_init_trip.name):
                    # (
                    #     (self.app.get_trip() is None) or \
                    #     (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_init_trip.name)
                    # ):
                print(f"DriverAgentIndie[{self.unique_id}]: Exiting market at time_step {self.current_time_step} with trip state {self.app.get_trip()['state']}")

                self.shutdown()
                return True
            else:
                print(f"DriverAgentIndie[{self.unique_id}]: Not exiting market at time_step {self.current_time_step} with trip state {self.app.get_trip()['state']} and shift_end_time {self.behavior['shift_end_time']}")
                return False

    def logout(self):
        self.app.close(self.get_current_time_str(), current_loc=self.current_loc)

    def estimate_next_event_time(self):
        ''' '''
        next_event_time =  min(self.app.manager.estimate_next_event_time(self.current_time),
                                self.app.trip.estimate_next_event_time(self.current_time))

        # logging.debug(f'{self.unique_id} estimates {next_event_time=}')

        return next_event_time

    def step(self, time_step):
        # # The agent's step will go here.
        self.app.update_current(self.get_current_time_str(), self.current_loc)
        print(f"driver_agent_indie.step: {self.unique_id}, time_step={time_step}, current_loc={self.current_loc}, trip_state={self.app.get_trip()['state'] if self.app.get_trip() else 'No Trip'}, next_event_time={self.estimate_next_event_time()}")

        if (self.current_time_step % self.behavior['steps_per_action'] == 0) and \
                    (random() <= self.behavior['response_rate']) and \
                    (self.next_event_time <= self.current_time):

                self.app.execute_step_actions(self.current_time, add_step_log_fn=self.add_step_log)

                return True
        else:
            return False

if __name__ == '__main__':

    agent = DriverAgentIndie('001', '001', '20200101080000')
    agent.step()


