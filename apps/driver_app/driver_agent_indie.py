import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from re import I
from shapely.geometry.linestring import LineString
from apps.lib.agent_workflow_sm import WorkflowStateMachine

# print(sys.path)
import logging
import json, time, asyncio
import pika
from datetime import datetime
import haversine as hs
import geopandas as gp
from random import choice, randint, random
from dateutil.relativedelta import relativedelta

# from mesa import Agent

from shapely.geometry import Point, mapping

from apps.config import settings
from .driver_app import DriverApp
from apps.utils.utils import id_generator, cut
from apps.lib import RidehailDriverTripStateMachine
from apps.loc_service import OSRMClient
from apps.utils.behavior_gen import BehaviorGen

from apps.loc_service import TaxiStop, BusStop

from apps.messenger_service import Messenger

# Driver agent will be called to apply behavior at every step
# At each step, the Agent will process list of collected messages in the app.

from apps.orsim import ORSimAgent

class DriverAgentIndie(ORSimAgent):

    # unique_id = None
    # current_loc = None
    # prev_time_step = None
    # current_time_step = None
    # elapsed_duration_steps = None
    active_route = None # shapely.geometry.LineString
    current_route_coords = None # shapely.geometry.LineString
    active = False

    sim_settings = settings['SIM_SETTINGS']
    step_size = sim_settings['SIM_STEP_SIZE'] # NumSeconds per each step.
    # stop_locations = TaxiStop().get_locations_within('CLEMENTI') # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION
    stop_locations = BusStop().get_locations_within(sim_settings['PLANNING_AREA']) # NOTE THIS CAN A MEMORY HOG. FIND A BETTER SOLUTION


    def __init__(self, unique_id, run_id, reference_time, scheduler_id, behavior):

        super().__init__(unique_id, run_id, reference_time, scheduler_id, behavior)

        self.current_loc = self.behavior['init_loc']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        # self.app = DriverApp(model.run_id, model.get_current_time_str(), self.current_loc, credentials=self.credentials, driver_settings=self.behavior['settings'])
        self.app = DriverApp(self.run_id, self.get_current_time_str(), self.current_loc, credentials=self.credentials, driver_settings=self.behavior['settings'])

        # self.agent_credentials = {
        #     'email': f"Agent_{self.behavior.get('email')}",
        #     'password': self.behavior.get('password'),
        # }

        # self.agent_messenger = Messenger(run_id, self.agent_credentials, f"ORSimAgent_{self.unique_id}", self.on_receive_message)


    def process_payload(self, payload):
        ''' '''
        if payload.get('action') == 'step':
            self.entering_market(payload.get('time_step'))
            if self.is_active():
                self.step(payload.get('time_step'))
            self.exiting_market()
        else:
            logging.error(f"{payload = }")


    def get_random_location(cls):
        return BehaviorGen.get_random_location()

    # def process_message(self, client, userdata, message):
    #     ''' '''
    #     # print('received message:', message.payload.decode('utf-8'))
    #     payload = json.loads(message.payload.decode('utf-8'))

    #     # if payload.get('action') == 'enter_market':
    #     #     self.entering_market(payload.get('time_step'))
    #     # elif payload.get('action') == 'step':
    #     #     self.step(payload.get('time_step'))
    #     # elif payload.get('action') == 'exit_market':
    #     #     self.exiting_market()
    #     if payload.get('action') == 'step':
    #         self.entering_market(payload.get('time_step'))
    #         if self.is_active():
    #             self.step(payload.get('time_step'))
    #         self.exiting_market()

        # response_payload = {
        #     'agent_id': self.unique_id,
        #     'action': 'completed'
        # }

        # self.agent_messenger.client.publish(f'ORSimController', json.dumps(response_payload))


    # def on_receive_message(self, client, userdata, message):
    #     ''' '''
    #     print('received message:', message.payload.decode('utf-8'))
    #     payload = json.loads(message.payload.decode('utf-8'))

    #     if payload.get('action') == 'enter_market':
    #         self.entering_market(payload.get('time_step'))
    #     elif payload.get('action') == 'step':
    #         self.step(payload.get('time_step'))
    #     elif payload.get('action') == 'exit_market':
    #         self.exiting_market()

    #     response_payload = {
    #         'agent_id': self.unique_id,
    #         'action': 'completed'
    #     }

    #     self.agent_messenger.client.publish(f'ORSimController', json.dumps(response_payload))

    # def start_listening(self):
    #     loop = asyncio.get_event_loop()
    #     try:
    #         loop.run_forever()
    #     except KeyboardInterrupt:
    #         pass
    #     finally:
    #         loop.close()

    # # def get_current_time_str(self):
    # #     return self.model.get_current_time_str()
    # def get_current_time_str(self):
    #     return datetime.strftime(self.current_time, "%a, %d %b %Y %H:%M:%S GMT")

    # @classmethod
    # def load_behavior(cls, unique_id, behavior=None):
    #     ''' '''
    #     shift_start_time = randint(0, (cls.sim_settings['SIM_DURATION']//4))
    #     shift_end_time = randint(cls.sim_settings['SIM_DURATION']//2, cls.sim_settings['SIM_DURATION']-1)

    #     if behavior is None:
    #         behavior = {
    #             'email': f'{unique_id}@test.com',
    #             'password': 'password',

    #             # 'start_time': 0,
    #             'shift_start_time': shift_start_time,
    #             'shift_end_time': shift_end_time, # settings['SIM_DURATION'], #shift_start_time + (settings['SIM_DURATION']//2),


    #             'init_loc': cls.get_home_loc(), # mapping(choice(cls.stop_locations)), # shapely.geometry.Point
    #             'empty_dest_loc': cls.get_empty_destination_loc(), # mapping(choice(cls.stop_locations)), # shapely.geometry.Point

    #             'settings': {
    #                 'market': 'RideHail',
    #                 'patience': 150,
    #                 'service_score': randint(1, 1000),
    #             },

    #             'transition_prob': {
    #                 # (confirm + reject) | driver_received_trip == 1
    #                 ('confirm', 'driver_received_trip'): 1.0,
    #                 ('reject', 'driver_received_trip'): 0.0,

    #                 # cancel | driver_accepted_trip = 1  if  exceeded_patience
    #                 # cancel | driver_accepted_trip ~ 0 otherwise
    #                 ('cancel', 'driver_accepted_trip', 'exceeded_patience'): 1.0,
    #                 ('cancel', 'driver_accepted_trip'): 0.0,

    #                 # cancel | driver_moving_to_pickup ~ 0
    #                 # wait_to_pickup | driver_moving_to_pickup ~ 1
    #                 ('cancel', 'driver_moving_to_pickup'): 0.0,
    #                 ('wait_to_pickup', 'driver_moving_to_pickup'): 1.0,

    #                 # cancel | driver_waiting_to_pickup = 1 if exceeded_patience
    #                 # cancel | driver_waiting_to_pickup ~ 0 otherwise
    #                 ('cancel', 'driver_waiting_to_pickup', 'exceeded_patience'): 1.0,
    #                 ('cancel', 'driver_waiting_to_pickup'): 0.0,

    #             },

    #             # 'constraint_accept': {
    #             #     'max_distance_moving_to_pickup': 5000,
    #             #     'wait_time_between_jobs': 60,
    #             # },

    #             # 'TrTime_accepted_TO_moving_to_pickup': 0,

    #             # # 'waiting_time_for_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
    #             'TrTime_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
    #             # # 'waiting_time_for_dropoff': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)

    #             'TrTime_dropoff': 0,


    #         }

    #     return behavior

    # @classmethod
    # def get_home_loc(cls):
    #     return mapping(choice(cls.stop_locations))

    # @classmethod
    # def get_empty_destination_loc(cls):
    #     return mapping(choice(cls.stop_locations))

    def entering_market(self, time_step):
        ''' '''

        # if self.model.driver_schedule.time == self.behavior['shift_start_time']:
        if time_step == self.behavior['shift_start_time']:
            self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
            self.app.login(self.get_current_time_str(), self.current_loc, self.active_route)

            self.active = True
            return True
        else:
            return False

    def is_active(self):
        return self.active

    def exiting_market(self):
        ''' '''

        if self.app.get_trip() is None:
            return False
        # elif (self.model.driver_schedule.time > self.behavior['shift_end_time']) and \
        elif (self.current_time_step > self.behavior['shift_end_time']) and \
                (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_init_trip.identifier):

            self.app.logout(self.get_current_time_str(), self.current_loc)
            self.active = False
            return True
        # # elif self.model.driver_schedule.time == settings['SIM_DURATION']-1:
        elif self.current_time_step == self.sim_settings['SIM_DURATION']-1:

            self.app.logout(self.get_current_time_str(), self.current_loc)
            self.active = False
            return True
        # else:
        #     self.app.logout(self.get_current_time_str(), self.current_loc)
        #     return True
        #     # return False
        # else:
        #     print('logging out')
        #     self.app.logout(self.get_current_time_str(), self.current_loc)
        #     return True

    def set_route(self, from_loc, to_loc): #, state):
        ''' find a Feasible route using some routeing engine'''
        self.active_route = OSRMClient.get_route(from_loc, to_loc)
        self.current_route_coords = OSRMClient.get_coords_from_route(self.active_route)

    # async def step(self, time_step):
    def step(self, time_step):
        # # The agent's step will go here.
        # print(f"Driver: {self.behavior['email']}")

        # 1. Always refresh trip manager to sync InMemory States with DB
        # self.refresh(time_step)
        self.app.refresh()
        # Driver has likely moved between the ticks, so update their current loc
        self.update_location()

        # if self.model.driver_schedule.time == settings['SIM_DURATION']-1:
        if self.current_time_step == self.sim_settings['SIM_DURATION']-1:
            # If this is the last tick of Simulation, logout and Forcibly terminate the current trip
            self.app.logout(self.get_current_time_str(), self.current_loc)
            self.shutdown()
            # return
        else:
            # If Simulation continues, take the actions for each step
            # 1. DeQueue all messages and process them in sequence
            self.consume_messages()
            # 2. based on current state, perform any workflow actions according to Agent behavior
            self.perform_workflow_actions()

        # print(f"{self.model.driver_schedule.time}: Driver {self.behavior['email']} completed execution")
        # # print("Sleep driver for 1 second")
        # # time.sleep(1)

    # def refresh(self, time_step):
    #     super().refresh(time_step)
    #     self.app.refresh()

        # # print(self.current_time_step, self.model.driver_schedule.time)

        # self.prev_time_step = self.current_time_step
        # # self.current_time_step = self.model.driver_schedule.time
        # self.current_time_step = time_step
        # self.elapsed_duration_steps = self.current_time_step - self.prev_time_step

        # self.current_time = self.reference_time + relativedelta(seconds = time_step * self.sim_settings['SIM_STEP_SIZE'])

    def update_location(self):
        ''' - Update self.current_loc based on:
                - last known current_loc
                - driver.state (waiting ==> no change in current_loc)
                - route
                - elapsed time
                - speed (average estimated speed)
            - Ping Waypoint. This restores the current state of the driver
                - Workflow events will be processed in the next step
        '''

        speed = 40 * 1000/3600 # 40 Km/H --> m/sec

        current_trip = self.app.get_trip()

        if RidehailDriverTripStateMachine.is_moving(current_trip['state']) == False:
            return
        else:
            moved_distance = speed * self.elapsed_duration_steps * self.step_size ## Make sure the stepSize is appropriately handled

            self.current_route_coords = cut(self.current_route_coords, moved_distance)[-1]

            try:
                if type(self.current_route_coords) == LineString:
                    self.current_loc = mapping(Point(self.current_route_coords.boundary[0]))
                elif type(self.current_route_coords) == Point:
                    self.current_loc = mapping(self.current_route_coords)
                # print(moved_distance, self.current_loc) #, self.current_route_coords)
            except Exception as e:
                logging.info(moved_distance)
                # print(e)
                logging.exception(str(e))

        # print(self.current_route_coords)
        # print(list(self.current_route_coords.coords))

        # NOTE This will be called at every Step hence the current_route_coords will always be based on Latest info from Agent
        self.app.ping(self.get_current_time_str(), current_loc=self.current_loc, current_route_coords=list(self.current_route_coords.coords))

    def consume_messages(self):
        ''' Consume messages. This ensures all the messages recieved between the two ticks are processed appropriately
                - workflows as a consequence of events must be handled here.
                - NOTE, In Simulation, the duration between ticks is uniform & discrete as opposed to continuous time in the real world.
            - NOTE Some grouping of messages could be done to avoid creating Unnecessary empty records
        '''
        payload = self.app.dequeue_message()
        # print(f"driver_agent.consume_messages: {payload = }")

        while payload is not None:
            if payload['action'] == 'requested_trip':
                passenger_id = payload['passenger_id']
                requested_trip = payload['requested_trip']

                try:
                    self.app.handle_requested_trip(self.get_current_time_str(),
                                                        current_loc=self.current_loc,
                                                        requested_trip=requested_trip)
                except Exception as e:
                    logging.exception(str(e))
                    # print(e)
                    raise e
            elif payload['action'] == 'passenger_workflow_event':
                if RidehailDriverTripStateMachine.is_passenger_channel_open(self.app.get_trip()['state']):
                    if self.app.get_trip()['passenger'] == payload['passenger_id']:
                        passenger_data = payload['data']

                        if passenger_data.get('event') == "passenger_confirmed_trip":
                            self.set_route(self.current_loc, self.app.get_trip()['pickup_loc']) #, self.app.get_driver()['state'])
                            # self.app.passenger_confirmed_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                            self.app.trip.passenger_confirmed_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                        if passenger_data.get('event') == "passenger_rejected_trip":
                            self.set_route(self.current_loc, self.app.get_trip()['pickup_loc']) #, self.app.get_driver()['state'])
                            # self.app.passenger_confirmed_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                            self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc, force_quit=False)
                            self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                            self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                        if passenger_data.get('event') == "passenger_acknowledge_pickup":
                            self.set_route(self.current_loc, self.app.get_trip()['dropoff_loc']) #, self.app.get_driver()['state'])
                            # self.app.passenger_acknowledge_pickup(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                            self.app.trip.passenger_acknowledge_pickup(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                        if passenger_data.get('event') == "passenger_acknowledge_dropoff":
                            # self.app.passenger_acknowledge_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)
                            self.app.trip.passenger_acknowledge_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)

                    else:
                        logging.warning(f"Mismatch {self.app.get_trip()['passenger']=} and {payload['passenger_id']=}")
                else:
                    logging.warning(f"Driver will not listen to Passenger workflow events when {self.app.get_trip()['state']=}")


            payload = self.app.dequeue_message()

    def perform_workflow_actions(self):
        ''' '''
        # print('inside perform_workflow_actions')
        driver = self.app.get_driver()
        #### NOTE THIS is a mistake. Should use the last transition time instead of the last waypoint (_updated) time
        time_since_last_event = (datetime.strptime(self.get_current_time_str(), "%a, %d %b %Y %H:%M:%S GMT") - \
                                datetime.strptime(self.app.get_trip()['_updated'], "%a, %d %b %Y %H:%M:%S GMT")
                                ).total_seconds()
        # print(time_since_last_event)

        if driver['state'] != WorkflowStateMachine.online.identifier:
            raise Exception(f"{driver['state'] = } is not valid")
        else:
            # NOTE The following statements are executed in sequence and each update might affect the execution of the following statements.
            # The order matters.
            if self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_looking_for_job.identifier:
                if type(self.current_route_coords) == Point:
                    self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc)

                    # self.set_route(self.current_loc, self.get_empty_destination_loc())
                    self.set_route(self.current_loc, self.get_random_location())
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # No need to reset route, continue moving on previous route

            if self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_received_trip.identifier:
                if random() <= self.get_transition_probability(('accept', self.app.get_trip()['state']), 1):
                    # self.app.confirm_trip(self.get_current_time_str(), current_loc=self.current_loc,)
                    self.app.trip.confirm(self.get_current_time_str(), current_loc=self.current_loc,)
                else:
                    # self.app.reject_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    self.app.trip.reject(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # No need to reset route, continue moving on previous route

            # if self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_accepted_trip.identifier:

            if self.app.get_trip()['state'] in RidehailDriverTripStateMachine.driver_moving_to_pickup.identifier:
                ''''''
                distance = hs.haversine(self.current_loc['coordinates'][:2], self.app.get_trip()['pickup_loc']['coordinates'][:2], unit=hs.Unit.METERS)
                # print(f"{distance = }")

                if (distance < 100):
                    # self.app.wait_to_pickup(self.get_current_time_str(), current_loc=self.current_loc,)
                    self.app.trip.wait_to_pickup(self.get_current_time_str(), current_loc=self.current_loc,)


            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_pickedup.identifier) and \
                (time_since_last_event >= self.behavior['TrTime_pickup']):
                    # self.app.move_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.trip.move_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc)

            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_moving_to_dropoff.identifier):

                distance = hs.haversine(self.current_loc['coordinates'][:2], self.app.get_trip()['dropoff_loc']['coordinates'][:2], unit=hs.Unit.METERS)
                # print(f"{distance = }")

                if (distance < 100):
                    # self.app.wait_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)
                    self.app.trip.wait_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)

            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_droppedoff.identifier) and \
                (time_since_last_event >= self.behavior['TrTime_dropoff']):

                    # self.app.complete(self.get_current_time_str(), current_loc=self.current_loc,)

                    # self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
                    # self.app.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                    self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
                    # self.app.end_trip(self.get_current_time_str(), current_loc=self.current_loc, look_for_job=True, route=self.active_route)
                    self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc, force_quit=False)
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)


if __name__ == '__main__':

    agent = DriverAgentIndie('001', '001', '20200101080000')
    agent.step()


