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
from dateutil import relativedelta

from mesa import Agent

from shapely.geometry import Point, mapping

from apps.config import settings
from apps.passenger_app import PassengerApp
from apps.utils.utils import id_generator, cut
from apps.lib import RidehailPassengerTripStateMachine, WorkflowStateMachine
from apps.loc_service import OSRMClient

from apps.loc_service import TaxiStop, BusStop

# Passenger agent will be called to apply behavior at every step
# At each step, the Agent will process list of collected messages in the app.

class PassengerAgent(Agent):

    current_loc = None
    current_time_step = None
    prev_time_step = None
    elapsed_duration_steps = None
    current_route_coords = None # shapely.geometry.LineString
    # model = None
    step_size = settings['SIM_STEP_SIZE'] # NumSeconds per each step.
    # stop_locations = TaxiStop().stop_locations # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION
    # stop_locations = TaxiStop().get_locations_within('CLEMENTI') # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION
    stop_locations = BusStop().get_locations_within(settings['PLANNING_AREA']) # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION


    def __init__(self, unique_id, model, behavior=None):
        super().__init__(unique_id, model)

        # NOTE, model should include run_id and start_time

        # Ideally behavior should be read from a datafile/db or in case of simulation, generated by the Model and passed in as attribute
        self.current_time_step = 0
        self.prev_time_step = 0
        self.elapsed_duration_steps = 0
        # self.stop_locations = TaxiStop().stop_locations # NOTE THIS IS A MEMORY HOG. FIND A BETTER SOLUTION

        # self.sim_clock = model.start_time
        if behavior is not None:
            self.behavior = behavior
        else:
            self.behavior = PassengerAgent.load_behavior(unique_id)
        self.current_loc = self.behavior['pickup_loc']
        self.pickup_loc = self.behavior['pickup_loc']
        self.dropoff_loc = self.behavior['dropoff_loc']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.app = PassengerApp(model.run_id, self.get_current_time_str(), self.current_loc, credentials=self.credentials, passenger_settings=self.behavior['settings'])
        # self.app.login(model.get_current_time_str(), self.current_loc, self.pickup_loc, self.dropoff_loc)

    def get_current_time_str(self):
        return self.model.get_current_time_str()

    def entering_market(self):
        if self.model.passenger_schedule.time == self.behavior['trip_request_time']:
            # print('Enter Market')
            # print(self.behavior)
            self.app.login(self.get_current_time_str(), self.current_loc, self.pickup_loc, self.dropoff_loc, trip_value=self.behavior.get('trip_value'))
            return True
        else:
            return False


    def exiting_market(self):
        # if self.app.get_trip() is None:
        #     return False
        if self.app.exited_market:
            return False
        elif (self.model.passenger_schedule.time > self.behavior['trip_request_time']) and \
                (self.app.get_trip()['state'] in [RidehailPassengerTripStateMachine.passenger_completed_trip.identifier,
                                                RidehailPassengerTripStateMachine.passenger_cancelled_trip.identifier,]):
                # (self.app.get_trip() is None):

            # print('Exit Market')
            # print(self.app.get_trip())
            self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)
            return True
        elif self.model.passenger_schedule.time == settings['SIM_DURATION']-1:

            self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)
            return True
        else:
            return False


    @classmethod
    def load_behavior(cls, unique_id, behavior=None):
        ''' '''
        trip_request_time = randint(0, settings['SIM_DURATION']-1)

        if behavior is None:
            behavior = {
                'email': f'{unique_id}@test.com',
                'password': 'password',

                'trip_request_time': trip_request_time, # in units of Simulation Step Size

                'pickup_loc': mapping(choice(cls.stop_locations)), # shapely.geometry.Point
                'dropoff_loc': mapping(choice(cls.stop_locations)), # shapely.geometry.Point

                'settings':{
                    'market': 'RideHail',
                    'patience': 600, # in Seconds
                },

                'transition_prob': {
                    # cancel | passenger_requested_trip = 1 if exceeded_patience
                    # cancel | passenger_requested_trip ~ 0
                    ('cancel', 'passenger_requested_trip', 'exceeded_patience'): 1.0,
                    ('cancel', 'passenger_requested_trip'): 0.0,

                    # cancel | passenger_assigned_trip ~ 0
                    ('cancel', 'passenger_assigned_trip'): 0.0,

                    # (accept + reject + cancel) | passenger_received_trip_confirmation == 1
                    ('accept', 'passenger_received_trip_confirmation',): 1.0,
                    ('reject', 'passenger_received_trip_confirmation'): 0.0,
                    ('cancel', 'passenger_received_trip_confirmation'): 0.0,
                    ('cancel', 'passenger_received_trip_confirmation', 'exceeded_patience'): 1.0,

                    # (cancel + move_for_pickup + wait_for_pickup) | passenger_accepted_trip ~ 0
                    ('cancel', 'passenger_accepted_trip'): 0.0,
                    # NOTE move_for_pickup and wait_for_pickup transition dependant on currentLoc and PickupLoc

                    # cancel | passenger_moving_for_pickup ~ 0
                    ('cancel', 'passenger_moving_for_pickup'): 0.0,

                    # cancel | passenger_waiting_for_pickup ~ 0
                    ('cancel', 'passenger_waiting_for_pickup'): 0.0,

                    # end_trip | passenger_droppedoff = 1
                    ('end_trip', 'passenger_droppedoff'): 1.0,

                },

                # 'Constraint_accept': {
                # },

            }

        return behavior

    # def initialize_location(self):
    #     if self.current_time_step == self.behavior['start_time']:

    #         ''' find a Feasible route using some routeing engine'''
    #         # route = OSRMClient.get_route(self.behavior['trip_start_loc'], self.behavior['trip_end_loc'])
    #         # self.current_route_coords = OSRMClient.get_coords_from_route(route)

    #         # self.app.set_route(self.get_current_time_str(), self.behavior['trip_start_loc'], self.behavior['trip_end_loc'], route)

    #         # # self.current_route_coords = self.app.get_route(self.get_current_time_str(), start_loc=self.current_loc, end_loc=None)



    async def step(self):
        # # The agent's step will go here.
        # print(f"{self.model.passenger_schedule.time}: Passenger {self.behavior['email']} start execution")
        # print(f"Passenger: {self.behavior['email']}")

        # 1. Always refresh trip manager to sync InMemory States with DB
        try:
            self.refresh()
        except Exception as e:
            # print(self.behavior)
            # print(self.app.get_trip())
            logging.exception(str(e))
            raise e

        if self.model.passenger_schedule.time == settings['SIM_DURATION']-1:
            self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)
        else:
            self.consume_messages()
            self.perform_workflow_actions()

        # # if self.behavior['email'] == "p_000001@test.com":
        # #     print(f"{self.behavior['email']} sleeping for 5 seconds")
        # #     time.sleep(5)
        # print(f"{self.model.passenger_schedule.time}: Passenger {self.behavior['email']} end execution")

    def refresh(self):
        self.app.refresh()

        # print(self.current_time_step, self.model.passenger_schedule.time)

        self.prev_time_step = self.current_time_step
        self.current_time_step = self.model.passenger_schedule.time
        self.elapsed_duration_steps = self.current_time_step - self.prev_time_step


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
                    logging.warning(f"Cannot assign Driver {payload['driver_id']} to passenger_trip {self.app.get_trip()['_id']} with state: {self.app.get_trip()['state']} ")
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
            raise Exception(f"{passenger['state'] = } is not valid")
        elif (self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.identifier) and \
                (self.behavior['trip_request_time'] + (self.behavior['settings']['patience']/settings['SIM_STEP_SIZE']) < self.model.passenger_schedule.time):
            logging.info(f"Passenger {self.app.get_passenger()['_id']} has run out of patience. Requested: {self.behavior['trip_request_time']}, patience: {self.behavior['settings']['patience']/settings['SIM_STEP_SIZE']}")
            self.app.trip.cancel(self.get_current_time_str(), current_loc=self.current_loc,)

        else:
            # print(f"{self.app.get_trip()['state'] = }")
            # if self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_assigned_trip.identifier:
            if self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.identifier:
                if random() <= self.behavior['transition_prob'].get(('accept', self.app.get_trip()['state']), 1):
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


