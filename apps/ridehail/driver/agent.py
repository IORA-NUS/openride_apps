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
from apps.ridehail.statemachine import RidehailDriverTripStateMachine
from apps.loc_service import OSRMClient
# from apps.utils.generate_behavior import GenerateBehavior
from apps.ridehail.scenario import GenerateBehavior

from apps.loc_service import TaxiStop, BusStop, cut, cut_route, create_route
from typing import Any, Dict

# from apps.messenger_service import Messenger

from orsim.lifecycle import ORSimAgent

from apps.utils.excepions import WriteFailedException, RefreshException
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext

class DriverAgentIndie(ORSimAgent):


    def _create_app(self):
        ''' Subclasses should implement this method to create their specific app instance.
        This method is called during initialization to set up the agent's application logic.
        '''
        return DriverApp(run_id=self.run_id,
                         sim_clock=self.get_current_time_str(),
                         behavior=self.behavior,
                         messenger=self.messenger,
                         agent_helper=self)

    @property
    def process_payload_on_init(self):
        return True

    def entering_market(self, time_step):
        ''' '''
        # if time_step == self.behavior['shift_start_time']:
        if (self.active == False) and (time_step == self.behavior.get('profile', {}).get('shift_start_time')):
            # self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
            print(f"DriverAgentIndie[{self.unique_id}]: Entering market at time_step {time_step}")
            try:
                self.app.launch(sim_clock=self.get_current_time_str())
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
            print(f"DriverAgentIndie[{self.unique_id}]: Not entering market at {time_step = } because {self.active = } and shift_start_time={self.behavior.get('profile', {}).get('shift_start_time')}")
            return False

    def exiting_market(self):
        ''' '''
        failure_threshold = 3
        if self.failure_count > failure_threshold:
            # print(f"DriverAgentIndie[{self.unique_id}]: Failure count {self.failure_count} exceeded threshold {failure_threshold}. Logging out.")
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
            elif (self.current_time_step > self.behavior.get('profile', {}).get('shift_end_time')) and \
                        (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_init_trip.name):
                    # (
                    #     (self.app.get_trip() is None) or \
                    #     (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_init_trip.name)
                    # ):
                print(f"DriverAgentIndie[{self.unique_id}]: Exiting market at time_step {self.current_time_step} with trip state {self.app.get_trip()['state']}")

                self.shutdown()
                return True
            else:
                print(f"DriverAgentIndie[{self.unique_id}]: Not exiting market at time_step {self.current_time_step} with trip state {self.app.get_trip()['state']} and shift_end_time {self.behavior.get('profile', {}).get('shift_end_time')}")
                return False

    def logout(self):
        # self.app.close(self.get_current_time_str(), current_loc=self.current_loc)
        self.app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        ''' '''
        next_event_time =  min(self.app.manager.estimate_next_event_time(self.current_time),
                                self.app.trip.estimate_next_event_time(self.current_time))

        # logging.debug(f'{self.unique_id} estimates {next_event_time=}')

        return next_event_time

    def step(self, time_step):
        # # The agent's step will go here.
        # self.app.update_current(self.get_current_time_str(), self.current_loc)
        self.app.update_current(self.get_current_time_str())
        print(f"driver_agent_indie.step: {self.unique_id}, time_step={time_step}, trip_state={self.app.get_trip()['state'] if self.app.get_trip() else 'No Trip'}, next_event_time={self.estimate_next_event_time()}")

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


