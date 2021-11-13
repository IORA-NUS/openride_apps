import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from re import I
from shapely.geometry.linestring import LineString
from apps.state_machine.agent_workflow_sm import WorkflowStateMachine

import logging
import json, time, asyncio
import pika
from datetime import datetime
import haversine as hs
import geopandas as gp
from random import choice, randint, random
from dateutil.relativedelta import relativedelta

from shapely.geometry import Point, mapping

from .driver_app import DriverApp
from apps.utils.utils import id_generator #, cut
from apps.state_machine import RidehailDriverTripStateMachine
from apps.loc_service import OSRMClient
from apps.utils.generate_behavior import GenerateBehavior

from apps.loc_service import TaxiStop, BusStop, cut

from apps.messenger_service import Messenger

from apps.orsim import ORSimAgent
from apps.config import driver_settings, orsim_settings

class DriverAgentIndie(ORSimAgent):

    active_route = None # shapely.geometry.LineString
    current_route_coords = None # shapely.geometry.LineString

    # step_size = orsim_settings['STEP_INTERVAL'] # NumSeconds per each step.



    def __init__(self, unique_id, run_id, reference_time, scheduler_id, behavior):

        super().__init__(unique_id, run_id, reference_time, scheduler_id, behavior)

        self.current_loc = self.behavior['init_loc']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.timeout_error = False
        self.failure_count = 0

        self.app = DriverApp(self.run_id, self.get_current_time_str(), self.current_loc, credentials=self.credentials, driver_settings=self.behavior['settings'])


    def process_payload(self, payload):
        ''' '''
        self.timeout_error = False

        if payload.get('action') == 'step':
            self.entering_market(payload.get('time_step'))

            if self.is_active():
                try:
                    self.step(payload.get('time_step'))
                    self.failure_count = 0
                except:
                    self.failure_count += 1

            self.exiting_market()
        else:
            logging.error(f"{payload = }")


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
            if self.app.get_trip() is None:
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
        self.current_route_coords = OSRMClient.get_coords_from_route(self.active_route)

    def logout(self):
        self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)

    def step(self, time_step):
        # # The agent's step will go here.
        if self.current_time_step % driver_settings['STEPS_PER_ACTION'] == 0:

            # 1. Always refresh trip manager to sync InMemory States with DB
            self.app.refresh() # Raises exception if unable to refresh
            ### Driver has likely moved between the ticks, so update their current loc
            ### SUpport for multiple pings per step for smooth animation
            # if time_step > self.behavior['shift_start_time']: # Strictly > 0
            #     ping_clock = self.current_time - relativedelta(seconds=(orsim_settings['STEP_INTERVAL'] * self.elapsed_duration_steps))
            #     prev_time_step = time_step -1

            #     ping_clock = ping_clock + relativedelta(seconds=driver_settings['LOCATION_PING_INTERVAL'])
            #     while ping_clock <= self.current_time:
            #         # For each tick of the ping_clock
            #         self.update_location(ping_clock, driver_settings['LOCATION_PING_INTERVAL'], publish=False)
            #         ping_clock = ping_clock + relativedelta(seconds=driver_settings['LOCATION_PING_INTERVAL'])

            #     # Push back ping_clock One tick
            #     ping_clock = ping_clock - relativedelta(seconds=driver_settings['LOCATION_PING_INTERVAL'])
            #     # update location at self.curent_time
            #     self.update_location(self.current_time, (self.current_time - ping_clock).total_seconds(), publish=True)


            # else:
            #     self.update_location(self.current_time, 0)

            self.update_location()

            # 1. DeQueue all messages and process them in sequence
            self.consume_messages()
            # 2. based on current state, perform any workflow actions according to Agent behavior
            self.perform_workflow_actions()


    # def update_location(self, ping_clock, elapsed_time, publish=False):
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
    #         # step_size = orsim_settings['STEP_INTERVAL'] # NumSeconds per each step.
    #         # elapsed_time = self.elapsed_duration_steps * step_size ## Make sure the stepSize is appropriately handled

    #         moved_distance = speed * elapsed_time

    #         self.current_route_coords = cut(self.current_route_coords, moved_distance)[-1]

    #         try:
    #             if type(self.current_route_coords) == LineString:
    #                 self.current_loc = mapping(Point(self.current_route_coords.boundary[0]))
    #             elif type(self.current_route_coords) == Point:
    #                 self.current_loc = mapping(self.current_route_coords)
    #             # print(moved_distance, self.current_loc) #, self.current_route_coords)
    #         except Exception as e:
    #             logging.info(moved_distance)
    #             # print(e)
    #             logging.exception(str(e))

    #     # print(self.current_route_coords)
    #     # print(list(self.current_route_coords.coords))

    #     # NOTE This will be called at every Step hence the current_route_coords will always be based on Latest info from Agent
    #     self.app.ping(self.format_time(ping_clock), current_loc=self.current_loc, publish=publish, current_route_coords=list(self.current_route_coords.coords))

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
            step_size = orsim_settings['STEP_INTERVAL'] # NumSeconds per each step.
            elapsed_time = self.elapsed_duration_steps * step_size ## Make sure the stepSize is appropriately handled

            moved_distance = speed * elapsed_time

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
            try:
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
                                self.set_route(self.current_loc, self.app.get_trip()['pickup_loc'])
                                self.app.trip.passenger_confirmed_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                            if passenger_data.get('event') == "passenger_rejected_trip":
                                self.set_route(self.current_loc, self.app.get_trip()['pickup_loc'])
                                self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc, force_quit=False)
                                self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                                self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                            if passenger_data.get('event') == "passenger_acknowledge_pickup":
                                self.set_route(self.current_loc, self.app.get_trip()['dropoff_loc'])
                                self.app.trip.passenger_acknowledge_pickup(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

                            if passenger_data.get('event') == "passenger_acknowledge_dropoff":
                                self.app.trip.passenger_acknowledge_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)

                        else:
                            logging.warning(f"Mismatch {self.app.get_trip()['passenger']=} and {payload['passenger_id']=}")
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
                if type(self.current_route_coords) == Point:
                    self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc)

                    self.set_route(self.current_loc, self.get_random_location())
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # No need to reset route, continue moving on previous route

            if self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_received_trip.identifier:
                if random() <= self.get_transition_probability(('accept', self.app.get_trip()['state']), 1):
                    self.app.trip.confirm(self.get_current_time_str(), current_loc=self.current_loc,)
                else:
                    self.app.trip.reject(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)
                    # No need to reset route, continue moving on previous route

            if self.app.get_trip()['state'] in RidehailDriverTripStateMachine.driver_moving_to_pickup.identifier:
                ''''''
                distance = hs.haversine(self.current_loc['coordinates'][:2], self.app.get_trip()['pickup_loc']['coordinates'][:2], unit=hs.Unit.METERS)

                if (distance < 100):
                    self.app.trip.wait_to_pickup(self.get_current_time_str(), current_loc=self.current_loc,)


            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_pickedup.identifier) and \
                (time_since_last_event >= self.behavior['TrTime_pickup']):
                    self.app.trip.move_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc)

            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_moving_to_dropoff.identifier):

                distance = hs.haversine(self.current_loc['coordinates'][:2], self.app.get_trip()['dropoff_loc']['coordinates'][:2], unit=hs.Unit.METERS)

                if (distance < 100):
                    self.app.trip.wait_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc,)

            if (self.app.get_trip()['state'] == RidehailDriverTripStateMachine.driver_droppedoff.identifier) and \
                (time_since_last_event >= self.behavior['TrTime_dropoff']):

                    self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
                    self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc, force_quit=False)
                    self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc)
                    self.app.trip.look_for_job(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)


if __name__ == '__main__':

    agent = DriverAgentIndie('001', '001', '20200101080000')
    agent.step()


