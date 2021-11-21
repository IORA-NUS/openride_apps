import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from re import I
from shapely.geometry.linestring import LineString
from apps.state_machine.agent_workflow_sm import WorkflowStateMachine

import logging, traceback
import json, time, asyncio
import pika
from datetime import datetime
import haversine as hs
import geopandas as gp
from random import choice, randint, random
from dateutil.relativedelta import relativedelta

from shapely.geometry import Point, mapping

from .driver_app import DriverApp
from apps.utils.utils import id_generator, str_to_time #, cut
from apps.state_machine import RidehailDriverTripStateMachine
from apps.loc_service import OSRMClient
# from apps.utils.generate_behavior import GenerateBehavior
from apps.scenario import GenerateBehavior

from apps.loc_service import TaxiStop, BusStop, cut, cut_route

from apps.messenger_service import Messenger

from apps.orsim import ORSimAgent
from apps.config import driver_settings, orsim_settings

class DriverAgentIndie(ORSimAgent):

    active_route = None # shapely.geometry.LineString
    traversed_path = None # shapely.geometry.LineString
    projected_path = None # shapely.geometry.LineString


    def __init__(self, unique_id, run_id, reference_time, scheduler_id, behavior):

        super().__init__(unique_id, run_id, reference_time, scheduler_id, behavior)

        self.current_loc = self.behavior['init_loc']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.timeout_error = False
        self.failure_count = 0

        self.app = DriverApp(self.run_id, self.get_current_time_str(), self.current_loc, credentials=self.credentials, profile=self.behavior['profile'])


    def process_payload(self, payload):
        ''' '''
        self.timeout_error = False
        did_step = False

        if payload.get('action') == 'step':
            self.entering_market(payload.get('time_step'))

            if self.is_active():
                try:
                    did_step = self.step(payload.get('time_step'))
                    self.failure_count = 0
                except Exception as e:
                    logging.exception(str(e))
                    self.failure_count += 1

            self.exiting_market()
        else:
            logging.error(f"{payload = }")

        return did_step

    def get_random_location(self):
        return GenerateBehavior.get_random_location(self.behavior['coverage_area_name'])


    def entering_market(self, time_step):
        ''' '''
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
        failure_threshold = 3
        if self.failure_count > failure_threshold:
            logging.warning(f'Shutting down {self.unique_id} due to too many failures')
            self.shutdown()
            return True
        elif self.timeout_error:
            self.shutdown()
            return True
        else:
            # if self.app.get_trip() is None:
            #     return False
            if self.app.exited_market:
                return False
            elif (self.current_time_step > self.behavior['shift_end_time']) and \
                    (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_init_trip.identifier):

                self.shutdown()
                return True
            else:
                return False

    def set_route(self, from_loc, to_loc):
        ''' find a Feasible route using some routeing engine'''
        self.active_route = OSRMClient.get_route(from_loc, to_loc)
        self.projected_path = OSRMClient.get_coords_from_route(self.active_route)
        self.traversed_path = []

    def get_tentative_travel_time(self, from_loc, to_loc):
        ''' find a Feasible route using some routeing engine'''
        tentative_route = OSRMClient.get_route(from_loc, to_loc)
        try:
            return tentative_route['duration']
        except:
            return 3600 # Some arbitrarily large number in Seconds


    def logout(self):
        self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)

    def estimate_next_event_time(self):
        ''' '''
        next_event_time =  min(self.app.driver.estimate_next_event_time(self.current_time),
                                self.app.trip.estimate_next_event_time(self.current_time))

        # logging.debug(f'{self.unique_id} estimates {next_event_time=}')

        return next_event_time

    def step(self, time_step):
        # # The agent's step will go here.
        self.app.update_current(self.get_current_time_str(), self.current_loc)

        if (self.current_time_step % self.behavior['STEPS_PER_ACTION'] == 0) and \
                    (random() <= self.behavior['RESPONSE_RATE']) and \
                    (self.next_event_time <= self.current_time):
                # 1. Always refresh trip manager to sync InMemory States with DB
                self.app.refresh() # Raises exception if unable to refresh
                ### Driver has likely moved between the ticks, so update their current loc
                # self.update_location()
                self.update_location_by_route()
                # 1. DeQueue all messages and process them in sequence
                self.consume_messages()
                # 2. based on current state, perform any workflow actions according to Agent behavior
                self.perform_workflow_actions()

                return True
        else:
            return False

    # def update_location(self):
    #     ''' - Update self.current_loc based on:
    #             - last known current_loc
    #             - driver.state (waiting ==> no change in current_loc)
    #             - route
    #             - elapsed time
    #             - speed (average estimated speed)
    #         - Ping Waypoint. This restores the current state of the driver
    #             - Workflow events will be processed in the next step
    #     '''

    #     speed = 40 * 1000/3600 # 40 Km/H --> m/sec

    #     current_trip = self.app.get_trip()

    #     if RidehailDriverTripStateMachine.is_moving(current_trip['state']) == False:
    #         return
    #     else:
    #         step_size = orsim_settings['STEP_INTERVAL'] # NumSeconds per each step.
    #         elapsed_time = self.elapsed_duration_steps * step_size ## Make sure the stepSize is appropriately handled

    #         moved_distance = speed * elapsed_time

    #         # self.projected_path = cut(self.projected_path, moved_distance)[-1]
    #         self.traversed_path, self.projected_path = cut(self.projected_path, moved_distance)

    #         try:
    #             if type(self.projected_path) == LineString:
    #                 self.current_loc = mapping(Point(self.projected_path.boundary[0]))
    #             elif type(self.projected_path) == Point:
    #                 self.current_loc = mapping(self.projected_path)
    #             # print(moved_distance, self.current_loc) #, self.projected_path)
    #         except Exception as e:
    #             logging.info(moved_distance)
    #             # print(e)
    #             logging.exception(str(e))

    #     # print(self.projected_path)
    #     # print(list(self.projected_path.coords))

    #     # NOTE This will be called at every Step hence the projected_path will always be based on Latest info from Agent
    #     # self.app.ping(self.get_current_time_str(), current_loc=self.current_loc, projected_path=list(self.current_route_coords.coords))
    #     self.app.ping(self.get_current_time_str(), current_loc=self.current_loc,
    #                   traversed_path=list(self.traversed_path.coords),
    #                   projected_path=list(self.projected_path.coords))


    def update_location_by_route(self):
        ''' - Update self.current_loc based on:
                - last known current_loc
                - driver.state (waiting ==> no change in current_loc)
                - route
                - elapsed time
                - speed (average estimated speed)
            - Ping Waypoint. This restores the current state of the driver
                - Workflow events will be processed in the next step
        '''

        trip = self.app.get_trip()
        elapsed_time = (self.current_time - str_to_time(trip['_updated'])).total_seconds()

        if (RidehailDriverTripStateMachine.is_moving(trip['state']) == False) or \
                (elapsed_time == 0) or (self.active_route is None):
            return
        else:

            try:
                self.traversed_path, self.projected_path, self.active_route = cut_route(self.active_route, elapsed_time)
            except Exception as e:
                logging.exception(traceback.format_exc())
                return

            try:
                if type(self.projected_path) == LineString:
                    self.current_loc = mapping(Point(self.projected_path.boundary[0]))
                elif type(self.projected_path) == Point:
                    self.current_loc = mapping(self.projected_path)
                # print(moved_distance, self.current_loc) #, self.projected_path)
            except Exception as e:
                logging.warning(f"{elapsed_time=}")
                # print(e)
                logging.exception(traceback.format_exc())

            # NOTE This will be called at every Step hence the projected_path will always be based on Latest info from Agent
            # self.app.ping(self.get_current_time_str(), current_loc=self.current_loc, projected_path=list(self.current_route_coords.coords))
            self.app.ping(self.get_current_time_str(), current_loc=self.current_loc,
                        traversed_path=list(self.traversed_path.coords),
                        projected_path=list(self.projected_path.coords))

    def consume_messages(self):
        ''' Consume messages. This ensures all the messages recieved between the two ticks are processed appropriately
                - workflows as a consequence of events must be handled here.
                - NOTE, In Simulation, the duration between ticks is uniform & discrete as opposed to continuous time in the real world.
            - NOTE Some grouping of messages could be done to avoid creating Unnecessary empty records
        '''
        payload = self.app.dequeue_message()
        # print(f"driver_agent.consume_messages: {payload = }")

        while payload is not None:
            try:
                # if payload['action'] == 'requested_trip':
                #     passenger_id = payload['passenger_id']
                #     requested_trip = payload['requested_trip']

                #     try:
                #         self.app.handle_requested_trip(self.get_current_time_str(),
                #                                             current_loc=self.current_loc,
                #                                             requested_trip=requested_trip)
                #     except Exception as e:
                #         logging.exception(str(e))
                #         # print(e)
                #         raise e
                # elif payload['action'] == 'passenger_workflow_event':
                if payload['action'] == 'passenger_workflow_event':
                    if RidehailDriverTripStateMachine.is_passenger_channel_open(self.app.get_trip()['state']):
                        if self.app.get_trip()['passenger'] == payload['passenger_id']:
                            passenger_data = payload['data']

                            if passenger_data.get('event') == "passenger_confirmed_trip":
                                self.set_route(self.current_loc, self.app.get_trip()['pickup_loc'])
                                self.app.trip.passenger_confirmed_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                            if passenger_data.get('event') == "passenger_rejected_trip":
                                # logging.warning('Overbooking handling is working ok')
                                self.set_route(self.current_loc, self.app.get_trip()['pickup_loc'])
                                # self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc, force_quit=False)
                                self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc, force_quit=True)
                                # self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                                # self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                                self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                            if passenger_data.get('event') == "passenger_acknowledge_pickup":
                                self.set_route(self.current_loc, self.app.get_trip()['dropoff_loc'])
                                self.app.trip.passenger_acknowledge_pickup(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                            if passenger_data.get('event') == "passenger_acknowledge_dropoff":
                                self.app.trip.passenger_acknowledge_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)

                        else:
                            logging.warning(f"Ignore Message due to Mismatch {self.app.get_trip()['passenger']=} and {payload['passenger_id']=}")
                    else:
                        logging.warning(f"Driver will not listen to Passenger workflow events when {self.app.get_trip()['state']=}")

                payload = self.app.dequeue_message()
            except Exception as e:
                # push message back into fornt of queue for processing in next step
                self.app.enfront_message(payload)
                raise e # Important do not allow the while loop to continue

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
                if type(self.projected_path) == Point:
                    self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc)

                    self.set_route(self.current_loc, self.get_random_location())
                    # self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    # self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # No need to reset route, continue moving on previous route

            if self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_received_trip.identifier:
                if random() <= self.get_transition_probability(('accept', self.app.get_trip()['state']), 1):
                    estimated_time_to_arrive = self.get_tentative_travel_time(self.current_loc, self.app.get_trip()['pickup_loc'])
                    self.app.trip.confirm(self.get_current_time_str(), current_loc=self.current_loc, estimated_time_to_arrive=estimated_time_to_arrive)
                else:
                    self.app.trip.reject(self.get_current_time_str(), current_loc=self.current_loc)
                    # self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    # self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # No need to reset route, continue moving on previous route

            if self.app.get_trip()['state'] in RidehailDriverTripStateMachine.driver_moving_to_pickup.identifier:
                ''''''
                distance = hs.haversine(self.current_loc['coordinates'][:2], self.app.get_trip()['pickup_loc']['coordinates'][:2], unit=hs.Unit.METERS)

                if (distance < 100):
                    self.app.trip.wait_to_pickup(self.get_current_time_str(), current_loc=self.current_loc,)


            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_pickedup.identifier) and \
                (time_since_last_event >= self.behavior['transition_time_pickup']):
                    self.app.trip.move_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc)

            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_moving_to_dropoff.identifier):

                distance = hs.haversine(self.current_loc['coordinates'][:2], self.app.get_trip()['dropoff_loc']['coordinates'][:2], unit=hs.Unit.METERS)

                if (distance < 100):
                    self.app.trip.wait_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)

            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_droppedoff.identifier) and \
                (time_since_last_event >= self.behavior['transition_time_dropoff']):

                    self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
                    self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc, force_quit=False)
                    # self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    # self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)


if __name__ == '__main__':

    agent = DriverAgentIndie('001', '001', '20200101080000')
    agent.step()


