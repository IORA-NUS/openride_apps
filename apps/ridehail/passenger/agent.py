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
from apps.ridehail.statemachine import RidehailPassengerTripStateMachine #, WorkflowStateMachine
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
from apps.ridehail.statemachine import RideHailActions, RideHailEvents

class PassengerAgentIndie(ORSimAgent):

    def _create_app(self):
        ''' Subclasses should implement this method to create their specific app instance.
        This method is called during initialization to set up the agent's application logic.
        '''
        return PassengerApp(run_id=self.run_id,
                            sim_clock=self.get_current_time_str(),
                            behavior=self.behavior,
                            messenger=self.messenger,
                            agent_helper=self
                        )


    @property
    def process_payload_on_init(self):
        return True

    def entering_market(self, time_step):
        # if time_step == self.behavior['trip_request_time']:
        if (self.active == False) and (time_step == self.behavior.get('profile', {}).get('trip_request_time')):
            # print('Enter Market')
            # print(self.behavior)
            self.app.launch(sim_clock=self.get_current_time_str())

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
            elif (self.current_time_step > self.behavior.get('profile', {}).get('trip_request_time')) and \
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
        # self.app.close(self.get_current_time_str(), current_loc=self.current_loc)
        self.app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        ''' '''
        # return self.current_time
        next_event_time =  min(self.app.manager.estimate_next_event_time(self.current_time),
                                self.app.trip.estimate_next_event_time(self.current_time))

        return next_event_time

    def step(self, time_step):
        self.add_step_log(f'In step')
        # self.app.update_current(self.get_current_time_str(), self.current_loc)
        self.app.update_current(self.get_current_time_str())

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


