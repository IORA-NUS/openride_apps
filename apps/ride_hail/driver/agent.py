import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from re import I
from shapely.geometry.linestring import LineString
# from apps.state_machine.agent_workflow_sm import WorkflowStateMachine
from apps.agent_core.state_machine import WorkflowStateMachine

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
from apps.state_machine import RidehailDriverTripStateMachine
from apps.loc_service import OSRMClient
# from apps.utils.generate_behavior import GenerateBehavior
from apps.scenario import GenerateBehavior

from apps.loc_service import TaxiStop, BusStop, cut, cut_route
from typing import Any, Dict

# from apps.messenger_service import Messenger

# from apps.orsim import ORSimAgent
# from orsim import ORSimAgent
from orsim import ORSimAgent

from apps.utils.excepions import WriteFailedException, RefreshException
from apps.utils.interaction_plugin import CallbackRouterInteractionPlugin, InteractionContext
from apps.ride_hail import RideHailActions, RideHailEvents, validate_passenger_workflow_payload
# from apps.agent_core.runtime import AgentRuntimeBase
# from apps.config import driver_settings, orsim_settings

class DriverAgentIndie(ORSimAgent):

    active_route = None # shapely.geometry.LineString
    traversed_path = None # shapely.geometry.LineString
    projected_path = None # shapely.geometry.LineString


    # def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler_id, behavior, orsim_settings):
    #     super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler_id, behavior, orsim_settings)
    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior): #, orsim_settings):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior) #, orsim_settings)

        self.current_loc = self.behavior['init_loc']
        self.action_when_free = behavior.get('action_when_free', 'random_walk')

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        # self.timeout_error = False
        self.failure_count = 0
        self.failure_log = {}

        try:
            self.app = DriverApp(self.run_id,
                                self.get_current_time_str(),
                                self.current_loc,
                                credentials=self.credentials,
                                profile=self.behavior['profile'],
                                messenger=self.messenger)

            # print("DriverApp initialized successfully")

            for topic, method in self.app.topic_params.items():
                self.register_message_handler(topic=topic, method=method)

            self._register_interaction_callbacks()

        except Exception as e:
            logging.exception(f"{self.unique_id = }: {str(e)}")
            self.agent_failed = True

    def get_random_location(self):
        return GenerateBehavior.get_random_location(self.behavior['coverage_area_name'])

    def process_payload(self, payload: Dict[str, Any]) -> bool:
        did_step: bool = False

        if (payload.get("action") == "step") or (payload.get("action") == "init"):
            self.add_step_log("Before entering_market")
            self.entering_market(payload.get("time_step"))
            self.add_step_log("After entering_market")
            # print(f"DriverAgentIndie[{self.unique_id}]: Completed entering_market with {self.app.get_trip()=}")

            if self.is_active():
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

        print(f"process_payload for driver {self.unique_id} completed with {self.step_log =}")
        return did_step


    def entering_market(self, time_step):
        ''' '''
        # if time_step == self.behavior['shift_start_time']:
        if (self.active == False) and (time_step == self.behavior['shift_start_time']):
            # self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
            print(f"DriverAgentIndie[{self.unique_id}]: Entering market at time_step {time_step}")

            if self.action_when_free == 'random_walk':
                self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
            elif self.action_when_free == 'stay':
                self.set_route(self.current_loc, None)

            self.app.launch(self.get_current_time_str(), self.current_loc, self.active_route)
            print(f"DriverAgentIndie[{self.unique_id}]: DriverApp launch successful")
            self.active = True
            return True
        elif self.active == True:
            print(f"DriverAgentIndie[{self.unique_id}]: Already active in market at time_step {time_step} with trip state {self.app.get_trip()['state'] if self.app.get_trip() else 'No Trip'}")
            return True
        else:
            print(f"DriverAgentIndie[{self.unique_id}]: Not entering market at time_step {time_step} because active={self.active} and shift_start_time={self.behavior['shift_start_time']}")
            return False

    def is_active(self):
        return self.active

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

    def set_route(self, from_loc, to_loc):
        ''' find a Feasible route using some routeing engine'''
        if to_loc is not None:
            self.active_route = OSRMClient.get_route(from_loc, to_loc)
            self.projected_path = OSRMClient.get_coords_from_route(self.active_route)
            self.traversed_path = []
            print(f"DriverAgentIndie[{self.unique_id}]: Setting route from {from_loc} to {to_loc}")
            print(f"DriverAgentIndie[{self.unique_id}]: Active route set with duration {self.active_route['duration']} seconds and distance {self.active_route['distance']} meters")
        else:
            self.active_route = None
            self.projected_path = []
            self.traversed_path = []
            print(f"DriverAgentIndie[{self.unique_id}]: No route set as to_loc is None")


    def get_tentative_travel_time(self, from_loc, to_loc):
        ''' find a Feasible route using some routeing engine'''
        try:
            tentative_route = OSRMClient.get_route(from_loc, to_loc)
            return tentative_route['duration']
        except:
            return 36000 # Some arbitrarily large number in Seconds


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
                # 1. Always refresh trip manager to sync InMemory States with DB
                self.add_step_log('Before refresh')
                self.app.refresh() # Raises exception if unable to refresh
                ### Driver has likely moved between the ticks, so update their current loc
                # self.update_location()
                self.add_step_log('Before update_location_by_route')
                self.update_location_by_route()
                # 1. DeQueue all messages and process them in sequence
                self.add_step_log('Before consume_messages')
                self.consume_messages()
                # 2. based on current state, perform any workflow actions according to Agent behavior
                self.add_step_log('Before perform_workflow_actions')
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
        print(f"update_location_by_route: {self.unique_id}, current_loc={self.current_loc}, trip={trip}")
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

            # try:
            if type(self.projected_path) == LineString:
                self.current_loc = mapping(Point(self.projected_path.boundary.geoms[0]))
            elif type(self.projected_path) == Point:
                self.current_loc = mapping(self.projected_path)
            # print(moved_distance, self.current_loc) #, self.projected_path)
            # except Exception as e:
            #     logging.warning(f"{elapsed_time=}")
            #     # print(e)
            #     logging.exception(traceback.format_exc())

            # NOTE This will be called at every Step hence the projected_path will always be based on Latest info from Agent
            # self.app.ping(self.get_current_time_str(), current_loc=self.current_loc, projected_path=list(self.current_route_coords.coords))
            # try:
            self.app.ping(self.get_current_time_str(), current_loc=self.current_loc,
                    traversed_path=list(self.traversed_path.coords),
                    projected_path=list(self.projected_path.coords))
            # except Exception as e:
            #     # logging.exception(traceback.format_exc())
            #     # logging.exception(str(e))
            #     logging.warning(str(e))
            #     raise e


    def consume_messages(self):

        ''' Consume messages. This ensures all the messages recieved between the two ticks are processed appropriately
                - workflows as a consequence of events must be handled here.
                - NOTE, In Simulation, the duration between ticks is uniform & discrete as opposed to continuous time in the real world.
            - NOTE Some grouping of messages could be done to avoid creating Unnecessary empty records
        '''
        payload = self.app.dequeue_message()

        while payload is not None:
            try:
                if payload['action'] == RideHailActions.PASSENGER_WORKFLOW_EVENT:
                    if validate_passenger_workflow_payload(payload) is False:
                        logging.warning(f"Invalid passenger workflow payload ignored: {payload=}")
                        payload = self.app.dequeue_message()
                        continue

                    trip = self.app.get_trip()
                    channel_open = RidehailDriverTripStateMachine.is_passenger_channel_open(trip['state'])
                    passenger_id_match = trip['passenger'] == payload['passenger_id']

                    if channel_open:
                        if passenger_id_match:
                            passenger_data = payload['data']
                            self._interaction_plugin.on_message(
                                InteractionContext(
                                    action=RideHailActions.PASSENGER_WORKFLOW_EVENT,
                                    event=passenger_data.get('event'),
                                    payload=payload,
                                    data=passenger_data,
                                )
                            )
                        else:
                            logging.warning(f"Ignore Message due to Mismatch {trip['passenger']=} and {payload['passenger_id']=}")
                    else:
                        logging.warning(f"Driver will not listen to Passenger workflow events when {trip['state']=}")

                payload = self.app.dequeue_message()
            except WriteFailedException as e:
                self.app.enfront_message(payload)
                raise e # Important do not allow the while loop to continue
            except RefreshException as e:
                raise e # Important do not allow the while loop to continue
            except Exception as e:
                raise e # Important do not allow the while loop to continue

    def perform_workflow_actions(self):
        """
        Execute workflow actions in a strict sequence using 'if' statements, not 'if-else', to allow state changes between steps.
        """
        driver = self.app.get_manager()
        trip = self.app.get_trip()
        time_since_last_event = (
            datetime.strptime(self.get_current_time_str(), "%a, %d %b %Y %H:%M:%S GMT") -
            datetime.strptime(trip['_updated'], "%a, %d %b %Y %H:%M:%S GMT")
        ).total_seconds()

        # Step 1: Check driver state
        if driver['state'] != WorkflowStateMachine.online.name:
            raise Exception(f"{driver['state'] = } is not valid")

        # Step 2: Sequence of state actions (not if-else)
        state = trip['state']
        prev_state = state
        if state == RidehailDriverTripStateMachine.driver_looking_for_job.name:
            self._on_state_looking_for_job(time_since_last_event)
            new_state = self.app.get_trip()['state']
            if new_state != prev_state:
                print(f"DriverAgentIndie [{self.unique_id}]: State changed from {prev_state} to {new_state}")
            prev_state = new_state
        state = self.app.get_trip()['state']
        if state == RidehailDriverTripStateMachine.driver_received_trip.name:
            self._on_state_received_trip(time_since_last_event)
            new_state = self.app.get_trip()['state']
            if new_state != prev_state:
                print(f"DriverAgentIndie[{self.unique_id}]: State changed from {prev_state} to {new_state}")
            prev_state = new_state
        state = self.app.get_trip()['state']
        if state == RidehailDriverTripStateMachine.driver_moving_to_pickup.name:
            self._on_state_moving_to_pickup(time_since_last_event)
            new_state = self.app.get_trip()['state']
            if new_state != prev_state:
                print(f"DriverAgentIndie[{self.unique_id}]: State changed from {prev_state} to {new_state}")
            prev_state = new_state
        state = self.app.get_trip()['state']
        if state == RidehailDriverTripStateMachine.driver_pickedup.name:
            self._on_state_pickedup(time_since_last_event)
            new_state = self.app.get_trip()['state']
            if new_state != prev_state:
                print(f"DriverAgentIndie[{self.unique_id}]: State changed from {prev_state} to {new_state}")
            prev_state = new_state
        state = self.app.get_trip()['state']
        if state == RidehailDriverTripStateMachine.driver_moving_to_dropoff.name:
            self._on_state_moving_to_dropoff(time_since_last_event)
            new_state = self.app.get_trip()['state']
            if new_state != prev_state:
                print(f"DriverAgentIndie[{self.unique_id}]: State changed from {prev_state} to {new_state}")
            prev_state = new_state
        state = self.app.get_trip()['state']
        if state == RidehailDriverTripStateMachine.driver_droppedoff.name:
            self._on_state_droppedoff(time_since_last_event)
            new_state = self.app.get_trip()['state']
            if new_state != prev_state:
                print(f"DriverAgentIndie[{self.unique_id}]: State changed from {prev_state} to {new_state}")
            prev_state = new_state
        # Fallback: plugin dispatch for any custom/unknown state
        state = self.app.get_trip()['state']
        if state not in [
            RidehailDriverTripStateMachine.driver_looking_for_job.name,
            RidehailDriverTripStateMachine.driver_received_trip.name,
            RidehailDriverTripStateMachine.driver_moving_to_pickup.name,
            RidehailDriverTripStateMachine.driver_pickedup.name,
            RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
            RidehailDriverTripStateMachine.driver_droppedoff.name,
        ]:
            self._interaction_plugin.on_state(
                InteractionContext(
                    state=state,
                    extra={'time_since_last_event': time_since_last_event},
                )
            )
            new_state = self.app.get_trip()['state']
            if new_state != prev_state:
                print(f"DriverAgentIndie [{self.unique_id}]: State changed from {prev_state} to {new_state}")


    def _register_interaction_callbacks(self):
        self._interaction_plugin = CallbackRouterInteractionPlugin()
        # Backward compatibility for tests and any code still reading this attribute.
        self._interaction_callbacks = self._interaction_plugin.router

        self._interaction_plugin.register_message(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_CONFIRMED_TRIP, self._on_passenger_confirmed_trip)
        self._interaction_plugin.register_message(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_REJECTED_TRIP, self._on_passenger_rejected_trip)
        self._interaction_plugin.register_message(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP, self._on_passenger_acknowledge_pickup)
        self._interaction_plugin.register_message(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF, self._on_passenger_acknowledge_dropoff)

        self._interaction_plugin.register_state(RidehailDriverTripStateMachine.driver_looking_for_job.name, self._on_state_looking_for_job)
        self._interaction_plugin.register_state(RidehailDriverTripStateMachine.driver_received_trip.name, self._on_state_received_trip)
        self._interaction_plugin.register_state(RidehailDriverTripStateMachine.driver_moving_to_pickup.name, self._on_state_moving_to_pickup)
        self._interaction_plugin.register_state(RidehailDriverTripStateMachine.driver_pickedup.name, self._on_state_pickedup)
        self._interaction_plugin.register_state(RidehailDriverTripStateMachine.driver_moving_to_dropoff.name, self._on_state_moving_to_dropoff)
        self._interaction_plugin.register_state(RidehailDriverTripStateMachine.driver_droppedoff.name, self._on_state_droppedoff)

    def _on_passenger_confirmed_trip(self, payload, data):
        self.set_route(self.current_loc, self.app.get_trip()['pickup_loc'])
        self.app.trip.passenger_confirmed_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

    def _on_passenger_rejected_trip(self, payload, data):
        self.app.trip.force_quit(self.get_current_time_str(), current_loc=self.current_loc)

        if self.action_when_free == 'random_walk':
            self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
        elif self.action_when_free == 'stay':
            self.set_route(self.current_loc, None)

        self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

    def _on_passenger_acknowledge_pickup(self, payload, data):
        self.set_route(self.current_loc, self.app.get_trip()['dropoff_loc'])
        self.app.trip.passenger_acknowledge_pickup(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

    def _on_passenger_acknowledge_dropoff(self, payload, data):
        self.app.trip.passenger_acknowledge_dropoff(self.get_current_time_str(), current_loc=self.current_loc)

    def _on_state_looking_for_job(self, time_since_last_event):
        if type(self.projected_path) == Point:
            self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc)
            self.set_route(self.current_loc, self.get_random_location())
            self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

    def _on_state_received_trip(self, time_since_last_event):
        if random() <= self.get_transition_probability(('accept', self.app.get_trip()['state']), 1):
            estimated_time_to_arrive = self.get_tentative_travel_time(self.current_loc, self.app.get_trip()['pickup_loc'])
            self.app.trip.confirm(self.get_current_time_str(), current_loc=self.current_loc, estimated_time_to_arrive=estimated_time_to_arrive)
        else:
            self.app.trip.reject(self.get_current_time_str(), current_loc=self.current_loc)
            self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)

    def _on_state_moving_to_pickup(self, time_since_last_event):
        distance = hs.haversine(
            reversed(self.current_loc['coordinates'][:2]),
            reversed(self.app.get_trip()['pickup_loc']['coordinates'][:2]),
            unit=hs.Unit.METERS,
        )
        if distance < 100:
            self.app.trip.wait_to_pickup(self.get_current_time_str(), current_loc=self.current_loc)

    def _on_state_pickedup(self, time_since_last_event):
        if time_since_last_event >= self.behavior['transition_time_pickup']:
            self.app.trip.move_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc)

    def _on_state_moving_to_dropoff(self, time_since_last_event):
        distance = hs.haversine(
            reversed(self.current_loc['coordinates'][:2]),
            reversed(self.app.get_trip()['dropoff_loc']['coordinates'][:2]),
            unit=hs.Unit.METERS,
        )
        if distance < 100:
            self.app.trip.wait_to_dropoff(self.get_current_time_str(), current_loc=self.current_loc)

    def _on_state_droppedoff(self, time_since_last_event):
        if time_since_last_event >= self.behavior['transition_time_dropoff']:
            self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc)

            if self.action_when_free == 'random_walk':
                self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
            elif self.action_when_free == 'stay':
                self.set_route(self.current_loc, None)

            self.app.create_new_unoccupied_trip(self.get_current_time_str(), current_loc=self.current_loc, route=self.active_route)


if __name__ == '__main__':

    agent = DriverAgentIndie('001', '001', '20200101080000')
    agent.step()


