import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

# print(sys.path)
import logging
import json, time, asyncio
import pika
import traceback
from random import random, randint, choice
from datetime import datetime
import geopandas as gp
from random import choice
from dateutil.relativedelta import relativedelta

# from mesa import Agent

from shapely.geometry import Point, mapping

# from apps.config import settings
from apps.passenger_app import PassengerApp
from apps.utils.utils import id_generator, cut
from apps.state_machine import RidehailPassengerTripStateMachine, WorkflowStateMachine
from apps.loc_service import OSRMClient

from apps.loc_service import TaxiStop, BusStop

from apps.messenger_service import Messenger

# Passenger agent will be called to apply behavior at every step
# At each step, the Agent will process list of collected messages in the app.
from apps.orsim import ORSimAgent
from apps.config import orsim_settings, passenger_settings

class PassengerAgentIndie(ORSimAgent):

    current_loc = None
    current_time_step = None
    prev_time_step = None
    elapsed_duration_steps = None
    current_route_coords = None # shapely.geometry.LineString
    # active = False
    # model = None
    # sim_settings = settings['SIM_SETTINGS']
    # step_size = sim_settings['SIM_STEP_SIZE'] # NumSeconds per each step.
    step_size = orsim_settings['SIM_STEP_SIZE'] # NumSeconds per each step.

    # # stop_locations = TaxiStop().stop_locations # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION
    # # stop_locations = TaxiStop().get_locations_within('CLEMENTI') # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION
    # stop_locations = BusStop().get_locations_within(sim_settings['PLANNING_AREA']) # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION


    def __init__(self, unique_id, run_id, reference_time, scheduler_id, behavior):
        # super().__init__(unique_id, model)
        # NOTE, model should include run_id and start_time
        super().__init__(unique_id, run_id, reference_time, scheduler_id, behavior)

        self.current_loc = self.behavior['pickup_loc']
        self.pickup_loc = self.behavior['pickup_loc']
        self.dropoff_loc = self.behavior['dropoff_loc']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.app = PassengerApp(self.run_id, self.get_current_time_str(), self.current_loc, credentials=self.credentials, passenger_settings=self.behavior['settings'])

    def process_payload(self, payload):
        if payload.get('action') == 'step':
            self.entering_market(payload.get('time_step'))
            if self.is_active():
                self.step(payload.get('time_step'))
            self.exiting_market()

        else:
            logging.error(f"{payload = }")


    def entering_market(self, time_step):
        # if self.model.passenger_schedule.time == self.behavior['trip_request_time']:
        if time_step == self.behavior['trip_request_time']:
            # print('Enter Market')
            # print(self.behavior)
            self.app.login(self.get_current_time_str(), self.current_loc, self.pickup_loc, self.dropoff_loc, trip_value=self.behavior.get('trip_value'))
            self.active = True
            return True
        else:
            return False

    # def is_active(self):
    #     return self.active


    def exiting_market(self):
        # if self.app.get_trip() is None:
        #     return False
        if self.app.exited_market:
            return False
        # elif (self.model.passenger_schedule.time > self.behavior['trip_request_time']) and \
        elif (self.current_time_step > self.behavior['trip_request_time']) and \
                (self.app.get_trip()['state'] in [RidehailPassengerTripStateMachine.passenger_completed_trip.identifier,
                                                RidehailPassengerTripStateMachine.passenger_cancelled_trip.identifier,]):
                # (self.app.get_trip() is None):

            # print('Exit Market')
            # print(self.app.get_trip())
            # self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)
            self.shutdown()
            self.active = False
            return True
        # elif self.model.passenger_schedule.time == settings['SIM_DURATION']-1:
        # elif self.current_time_step == self.sim_settings['SIM_DURATION']-1:

        #     # self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)
        #     self.shutdown()

        #     self.active = False

        #     return True
        else:
            return False


    # @classmethod
    # def load_behavior(cls, unique_id, behavior=None):
    #     ''' '''
    #     trip_request_time = randint(0, cls.sim_settings['SIM_DURATION']-1)
    #     # trip_request_time = 0

    #     if behavior is None:
    #         behavior = {
    #             'email': f'{unique_id}@test.com',
    #             'password': 'password',

    #             'trip_request_time': trip_request_time, # in units of Simulation Step Size

    #             'pickup_loc': mapping(choice(cls.stop_locations)), # shapely.geometry.Point
    #             'dropoff_loc': mapping(choice(cls.stop_locations)), # shapely.geometry.Point

    #             'settings':{
    #                 'market': 'RideHail',
    #                 'patience': 600, # in Seconds
    #             },

    #             'transition_prob': {
    #                 # cancel | passenger_requested_trip = 1 if exceeded_patience
    #                 # cancel | passenger_requested_trip ~ 0
    #                 ('cancel', 'passenger_requested_trip', 'exceeded_patience'): 1.0,
    #                 ('cancel', 'passenger_requested_trip'): 0.0,

    #                 # cancel | passenger_assigned_trip ~ 0
    #                 ('cancel', 'passenger_assigned_trip'): 0.0,

    #                 # (accept + reject + cancel) | passenger_received_trip_confirmation == 1
    #                 ('accept', 'passenger_received_trip_confirmation',): 1.0,
    #                 ('reject', 'passenger_received_trip_confirmation'): 0.0,
    #                 ('cancel', 'passenger_received_trip_confirmation'): 0.0,
    #                 ('cancel', 'passenger_received_trip_confirmation', 'exceeded_patience'): 1.0,

    #                 # (cancel + move_for_pickup + wait_for_pickup) | passenger_accepted_trip ~ 0
    #                 ('cancel', 'passenger_accepted_trip'): 0.0,
    #                 # NOTE move_for_pickup and wait_for_pickup transition dependant on currentLoc and PickupLoc

    #                 # cancel | passenger_moving_for_pickup ~ 0
    #                 ('cancel', 'passenger_moving_for_pickup'): 0.0,

    #                 # cancel | passenger_waiting_for_pickup ~ 0
    #                 ('cancel', 'passenger_waiting_for_pickup'): 0.0,

    #                 # end_trip | passenger_droppedoff = 1
    #                 ('end_trip', 'passenger_droppedoff'): 1.0,

    #             },

    #             # 'Constraint_accept': {
    #             # },

    #         }

    #     return behavior

    # def initialize_location(self):
    #     if self.current_time_step == self.behavior['start_time']:

    #         ''' find a Feasible route using some routeing engine'''
    #         # route = OSRMClient.get_route(self.behavior['trip_start_loc'], self.behavior['trip_end_loc'])
    #         # self.current_route_coords = OSRMClient.get_coords_from_route(route)

    #         # self.app.set_route(self.get_current_time_str(), self.behavior['trip_start_loc'], self.behavior['trip_end_loc'], route)

    #         # # self.current_route_coords = self.app.get_route(self.get_current_time_str(), start_loc=self.current_loc, end_loc=None)

    def logout(self):
        self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)


    # async def step(self, time_step):
    def step(self, time_step):
        # # The agent's step will go here.
        # print(f"{self.model.passenger_schedule.time}: Passenger {self.behavior['email']} start execution")
        # print(f"Passenger: {self.behavior['email']}")

        # 1. Always refresh trip manager to sync InMemory States with DB
        try:
            # self.refresh(time_step)
            self.app.refresh()
        except Exception as e:
            # print(self.behavior)
            # print(self.app.get_trip())
            logging.exception(str(e))
            raise e

        # if self.model.passenger_schedule.time == settings['SIM_DURATION']-1:
        # if self.current_time_step == self.sim_settings['SIM_DURATION']-1:
        #     self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)
        #     self.shutdown()
        # else:
        self.consume_messages()
        self.perform_workflow_actions()

        # # if self.behavior['email'] == "p_000001@test.com":
        # #     print(f"{self.behavior['email']} sleeping for 5 seconds")
        # #     time.sleep(5)
        # print(f"{self.model.passenger_schedule.time}: Passenger {self.behavior['email']} end execution")

    # def refresh(self, time_step):
    #     super().refresh(time_step)
    #     self.app.refresh()

        # # print(self.current_time_step, self.model.passenger_schedule.time)

        # self.prev_time_step = self.current_time_step
        # # self.current_time_step = self.model.passenger_schedule.time
        # self.current_time_step = time_step
        # self.elapsed_duration_steps = self.current_time_step - self.prev_time_step

        # self.current_time = self.reference_time + relativedelta(seconds = time_step * self.sim_settings['SIM_STEP_SIZE'])

    def consume_messages(self):
        ''' '''
        payload = self.app.dequeue_message()
        # print(f"passenger_agent.consume_messages: {payload = }")

        while payload is not None:
            # process message
            if payload['action'] == 'assigned':
                if self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.identifier:
                    # driver_id = payload['driver_id']

                    try:
                        # self.app.handle_assigned_trip(self.get_current_time_str(),
                        #                             current_loc=self.current_loc,
                        #                             driver_id=payload['driver_id'])
                        self.app.trip.assign(self.get_current_time_str(),
                                                    current_loc=self.current_loc,
                                                    driver=payload['driver_id'])
                    except Exception as e:
                        # print(e)
                        logging.exception(str(e))
                        raise e
                else:
                    # print(self.app.get_trip())
                    logging.warning(f"WARNING: Cannot assign Driver {payload['driver_id']} to passenger_trip {self.app.get_trip()['_id']} with state: {self.app.get_trip()['state']} ")
                    # raise Exception('passenger must be in Requested State to assign new driver.')

            elif payload['action'] == 'driver_workflow_event':
                if RidehailPassengerTripStateMachine.is_driver_channel_open(self.app.get_trip()['state']):
                    ''' '''
                    # Some sort of authentication must be present to allow only actie driver to message the passenger
                    if self.app.get_trip()['driver'] == payload['driver_id']:
                        driver_data = payload['data']

                        if driver_data.get('event') == "driver_confirmed_trip":
                            self.app.trip.driver_confirmed_trip(self.get_current_time_str(), self.current_loc) #, driver_data.get('driver_trip_id'))

                        elif driver_data.get('location') is not None:
                            self.current_loc = driver_data.get('location')

                            if driver_data.get('event') == "driver_arrived_for_pickup":
                                self.app.trip.driver_arrived_for_pickup(self.get_current_time_str(), self.current_loc, driver_data.get('driver_trip_id'))

                            elif driver_data.get('event') == "driver_move_for_dropoff":
                                self.app.trip.driver_move_for_dropoff(self.get_current_time_str(), self.current_loc, route=driver_data['planned_route'])

                            elif driver_data.get('event') == "driver_arrived_for_dropoff":
                                self.app.trip.driver_arrived_for_dropoff(self.get_current_time_str(), self.current_loc)

                            elif driver_data.get('event') == "driver_waiting_for_dropoff":
                                self.app.trip.driver_waiting_for_dropoff(self.get_current_time_str(), self.current_loc)

                            elif driver_data.get('event') == "driver_cancelled_trip":
                                self.app.trip.driver_cancelled_trip(self.get_current_time_str(), self.current_loc)

                            else:
                                self.app.ping(self.get_current_time_str(), current_loc=self.current_loc)

                    else:
                        logging.warning(f"WARNING: Mismatch {self.app.get_trip()['driver']=} and {payload['driver_id']=}")
                else:
                    logging.warning(f"WARNING: Passenger will not listen to Driver workflow events when {self.app.get_trip()['state']=}")



            payload = self.app.dequeue_message()

    def perform_workflow_actions(self):
        ''' '''
        passenger = self.app.get_passenger()
        # current_trip = self.app.get_trip # NOTE current trip is a function
        #### NOTE THIS is a mistake. Should use the last transition time instead of the last waypoint (_updated) time
        try:
            time_since_last_event = (datetime.strptime(self.get_current_time_str(), "%a, %d %b %Y %H:%M:%S GMT") - \
                                datetime.strptime(self.app.get_trip()['_updated'], "%a, %d %b %Y %H:%M:%S GMT")
                                ).total_seconds()
        except Exception as e:
            logging.info(self.behavior)
            logging.exception(str(e))
            raise e

        if passenger['state'] != WorkflowStateMachine.online.identifier:
            try:
                raise Exception(f"{passenger['state'] = } is not valid")
            except Exception as e:
                logging.exception(str(e))

        elif (self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.identifier) and \
                (self.behavior['trip_request_time'] + (self.behavior['settings']['patience']/self.step_size) < self.current_time_step):
                # (self.behavior['trip_request_time'] + (self.behavior['settings']['patience']/self.sim_settings['SIM_STEP_SIZE']) < self.current_time_step):
                # (self.behavior['trip_request_time'] + (self.behavior['settings']['patience']/self.sim_settings['SIM_STEP_SIZE']) < self.model.passenger_schedule.time):
            # logging.info(f"Passenger {self.app.get_passenger()['_id']} has run out of patience. Requested: {self.behavior['trip_request_time']}, patience: {self.behavior['settings']['patience']/self.sim_settings['SIM_STEP_SIZE']}")
            logging.info(f"Passenger {self.app.get_passenger()['_id']} has run out of patience. Requested: {self.behavior['trip_request_time']}, Max patience: {self.behavior['settings']['patience']/self.step_size} steps")
            self.app.trip.cancel(self.get_current_time_str(), current_loc=self.current_loc,)

        else:
            # print(f"{self.app.get_trip()['state'] = }")
            # if self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_assigned_trip.identifier:
            if self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.identifier:
                # if random() <= self.behavior['transition_prob'].get(('accept', self.app.get_trip()['state']), 1):
                if random() <= self.get_transition_probability(('accept', self.app.get_trip()['state']), 1):
                    self.app.trip.accept(self.get_current_time_str(), current_loc=self.current_loc,)
                else:
                    # self.app.trip.cancel(self.get_current_time_str(), current_loc=self.current_loc,)
                    self.app.trip.reject(self.get_current_time_str(), current_loc=self.current_loc,)

            # move for pickup not implemenetd
            if self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_accepted_trip.identifier:
                self.app.trip.wait_for_pickup(self.get_current_time_str(), current_loc=self.current_loc,)

            if self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_droppedoff.identifier:
                self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc,)


if __name__ == '__main__':

    agent = PassengerAgent('001', None)
    agent.step()


