
import requests, json, polyline, traceback
from random import choice
from http import HTTPStatus
from datetime import datetime
import shapely
from shapely.geometry.geo import mapping
import logging

from apps.config import settings
from apps.utils.utils import is_success

from apps.common.user_registry import UserRegistry
from apps.ridehail.message_data_models import RequestedTripActionPayload
from .manager import DriverManager
from .trip_manager import DriverTripManager
from apps.loc_service import OSRMClient
from orsim.lifecycle import ORSimApp

from apps.ridehail.statemachine import RidehailDriverTripStateMachine, driver_passenger_interactions
from orsim.utils import WorkflowStateMachine
from orsim.messenger.interaction import message_handler, state_handler

import haversine as hs
from random import choice, randint, random

from apps.utils.excepions import WriteFailedException, RefreshException, HandlerValidationException
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
from apps.ridehail.statemachine import RideHailActions, RideHailEvents
from apps.ridehail.message_data_models import RequestedTripActionPayload, PassengerWorkflowPayload

from shapely.geometry import Point, mapping
from shapely.geometry.linestring import LineString

from apps.utils.utils import id_generator, str_to_time, time_to_str #, cut
from apps.loc_service import TaxiStop, BusStop, cut, cut_route, create_route, get_tentative_travel_time
from apps.ridehail.scenario import GenerateBehavior

from .passenger_interaction_mixin import PassengerInteractionMixin

class DriverApp(ORSimApp, PassengerInteractionMixin):

    @property
    def managed_statemachine(self):
        return RidehailDriverTripStateMachine # <-- this must be a StateMachine class

    @property
    def interaction_ground_truth_list(self):
        return [driver_passenger_interactions] # <-- this must be a list of interaction dicts with a specific Key structure.

    @property
    def runtime_behavior_schema(self):
        return {
            'profile': {
                'type': 'dict',
                'required': True,
                'schema': {
                    'action_when_free': {'type': 'string', 'required': True},
                    'coverage_area_name': {'type': 'string', 'required': True},
                    'empty_dest_loc': {'type': 'dict', 'required': True},
                    'init_loc': {'type': 'dict', 'required': True},
                    'shift_start_time': {'type': 'integer', 'required': True},
                    'shift_end_time': {'type': 'integer', 'required': True},
                    'transition_prob': {'type': 'list', 'required': True},
                    'transition_time_dropoff': {'type': 'integer', 'required': True},
                    'transition_time_pickup': {'type': 'integer', 'required': True},
                    'update_passenger_location': {'type': 'boolean', 'required': True},
                }
            }
        }

    def __init__(self, run_id, sim_clock, behavior, messenger, agent_helper=None):
        super().__init__(run_id=run_id,
                         sim_clock=sim_clock,
                         behavior = behavior,
                         messenger=messenger,
                         agent_helper=agent_helper)
        self.trip = self.create_trip_manager()

        self.current_time = None
        self.current_time_str = None

        self.latest_sim_clock = sim_clock
        # self.latest_loc = current_loc
        self.current_loc = self.behavior.get('profile', {}).get('init_loc')
        self.latest_loc = self.current_loc

        self.active_route = None # shapely.geometry.LineString
        self.traversed_path = None # shapely.geometry.LineString
        self.projected_path = None # shapely.geometry.LineString

        self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def _create_manager(self):
        return DriverManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get('profile', {}),
            persona=self.behavior.get('persona', {}))

    def create_trip_manager(self):
        return DriverTripManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            messenger=self.messenger,
            persona=self.behavior.get('persona', {}))

    def launch(self, sim_clock):
        ''' '''
        super().launch(sim_clock)  # Call BaseApp's launch method to login the manager
        if self.behavior.get('profile', {}).get('action_when_free') == 'random_walk':
            self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.behavior.get('profile', {}).get('empty_dest_loc'))
        elif self.behavior.get('profile', {}).get('action_when_free') == 'stay':
            self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, None)
        self.create_new_unoccupied_trip(sim_clock, self.current_loc, self.active_route)

    def get_random_location(self):
        return GenerateBehavior.get_random_location(self.behavior.get('profile', {}).get('coverage_area_name'))

    def close(self, sim_clock):
        ''' '''
        logging.debug(f'logging out Driver {self.manager.get_id()}')
        try:
            self.trip.end_active_trip(sim_clock, self.current_loc, force=True)
        except Exception as e:
            logging.exception(str(e))

        super().close(sim_clock)  # Call BaseApp's close method to set exited_market = True

    def refresh(self):
        ''' Sync ALL inMemory State with the db State'''
        # Driver
        # No need to refresh driver at every step
        # self.manager.refresh()

        self.trip.refresh()
        # raise exception if unable to refresh

    def ping(self, sim_clock, current_loc, publish=False, **kwargs):
        ''' '''
        # self.latest_sim_clock = sim_clock
        # self.latest_loc = current_loc

        self.trip.ping(sim_clock, current_loc, **kwargs) # Raises exception if ping fails

        if publish:
            if self.get_trip()['state'] in [RidehailDriverTripStateMachine.driver_moving_to_dropoff.name]:
                self.messenger.client.publish(f'{self.run_id}/{self.get_trip()["passenger"]}',
                                    json.dumps({
                                        'action': 'driver_workflow_event',
                                        'driver_id': self.manager.get_id(),
                                        'data': {
                                            'location': current_loc,
                                        }

                                    })
                                )

    def get_trip(self):
        return self.trip.as_dict()

    def create_new_unoccupied_trip(self, sim_clock, current_loc, route):
        self.trip.create_new_unoccupied_trip(sim_clock, current_loc, self.manager.as_dict(), self.manager.vehicle.as_dict(), route)

    def handle_requested_trip(self, sim_clock, current_loc, requested_trip):
        '''
        Check for any existing trip
        If current trip is un occupied, end the trip
          - Note Driver will be without trip briefly. it might be a good idea to do the unassign/reassign in a transaction
        If current Trip is Occupied, this assignment must be rejected (This should NOT happen and might be a bug)
        print(self.trip.as_dict())
        '''

        if self.trip.as_dict()['is_occupied'] == False:
            self.trip.end_active_trip(sim_clock, current_loc, force=False)

            self.trip.create_new_occupied_trip(sim_clock, current_loc, self.manager.as_dict(), self.manager.vehicle.as_dict(), requested_trip)
        else:
            logging.warning(f'Ignoring Assignment request: Driver {self.manager.get_id()} is already engaged in an Occupied trip')


    ################
    # # Message Callbacks and other methods
    # def update_current(self, sim_clock, current_loc):
    #     self.latest_sim_clock = sim_clock
    #     self.latest_loc = current_loc


    def handle_app_topic_messages(self, payload):
        ''' Push message to a personal RabbitMQ Queue
        - At every step (simulation), The agent will pull items from queue and process them in sequence until Queue is empty
        '''
        if payload['action'] == RideHailActions.REQUESTED_TRIP:
            parsed = RequestedTripActionPayload.parse(payload)

            if parsed is not None:
                try:
                    self.handle_requested_trip(
                        self.latest_sim_clock,
                        current_loc=self.latest_loc,
                        requested_trip=parsed.requested_trip
                    )
                except Exception as e:
                    logging.warning(f"Driver failed to respond to trip Request {payload=}: {str(e)}")
        else:
            # CRITICAL: Ensure all messages are enqueued for processing in the next step
            self.enqueue_message(payload)


    def execute_step_actions(self, current_time, add_step_log_fn=None):
        self.current_time = current_time
        self.current_time_str = time_to_str(current_time)

        # 1. Always refresh trip manager to sync InMemory States with DB
        if add_step_log_fn:
            add_step_log_fn('Before refresh')
        self.refresh() # Raises exception if unable to refresh
        ### Driver has likely moved between the ticks, so update their current loc
        # self.update_location()
        if add_step_log_fn:
            add_step_log_fn('Before update_location_by_route')
        self.update_location_by_route()

        # 1. DeQueue all messages and process them in sequence
        if add_step_log_fn:
            add_step_log_fn('Before consume_messages')
        self.consume_messages()
        # 2. based on current state, perform any workflow actions according to Agent behavior
        if add_step_log_fn:
            add_step_log_fn('Before perform_workflow_actions')
        self.perform_workflow_actions()


    def consume_messages(self):
        '''
        Consume messages. This ensures all the messages received between the two ticks are processed appropriately.
        - Workflows as a consequence of events must be handled here.
        - In Simulation, the duration between ticks is uniform & discrete as opposed to continuous time in the real world.
        - Some grouping of messages could be done to avoid creating unnecessary empty records.
        '''
        payload = self.dequeue_message()
        while payload is not None:
            try:
                # Critical: Only process passenger workflow events if channel is open and passenger matches
                if payload['action'] == RideHailActions.PASSENGER_WORKFLOW_EVENT:
                    # if not validate_passenger_workflow_payload(payload):
                    if PassengerWorkflowPayload.parse(payload) is None:
                        logging.warning(f"Invalid passenger workflow payload ignored: {payload=}")
                        payload = self.dequeue_message()
                        continue
                    trip = self.get_trip()
                    channel_open = RidehailDriverTripStateMachine.is_passenger_channel_open(trip['state'])
                    passenger_id_match = trip['passenger'] == payload['passenger_id']
                    if channel_open and passenger_id_match:
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
                        logging.warning(f"Driver will not listen to Passenger workflow events when {trip['state']=}")
                else:
                    # For other actions, you can dispatch via plugin or handle as needed
                    self._interaction_plugin.on_message(
                        InteractionContext(
                            action=payload.get('action'),
                            event=payload.get('event'),
                            payload=payload,
                        )
                    )
                payload = self.dequeue_message()
            except WriteFailedException as e:
                self.enfront_message(payload)
                raise e
            except RefreshException as e:
                raise e
            except Exception as e:
                raise e

    def perform_workflow_actions(self):
        '''
        Execute workflow actions in a strict sequence using a for loop and interaction_plugin for extensibility and clarity.
        Critical: This ensures state transitions are handled one at a time, allowing for intermediate state changes.
        '''
        driver = self.get_manager()
        trip = self.get_trip()
        time_since_last_event = (
            datetime.strptime(self.current_time_str, "%a, %d %b %Y %H:%M:%S GMT") -
            datetime.strptime(trip['_updated'], "%a, %d %b %Y %H:%M:%S GMT")
        ).total_seconds()

        # Step 1: Check driver state # I believe this should be an agent level validation
        if driver['state'] != WorkflowStateMachine.online.name:
            raise Exception(f"{driver['state'] = } is not valid")

        # Step 2: Sequence of state actions using a for loop
        state_sequence = [
            RidehailDriverTripStateMachine.driver_looking_for_job.name,
            RidehailDriverTripStateMachine.driver_received_trip.name,
            RidehailDriverTripStateMachine.driver_moving_to_pickup.name,
            RidehailDriverTripStateMachine.driver_pickedup.name,
            RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
            RidehailDriverTripStateMachine.driver_droppedoff.name,
        ]
        prev_state = trip['state']
        for state_name in state_sequence:
            state = self.get_trip()['state']
            if state == state_name:
                # Standardize: Use interaction_plugin for all state handling
                self._interaction_plugin.on_state(
                    InteractionContext(
                        state=state,
                        extra={'time_since_last_event': time_since_last_event},
                    )
                )
                new_state = self.get_trip()['state']
                if new_state != prev_state:
                    # print(f"DriverAgentIndie [{self.unique_id}]: State changed from {prev_state} to {new_state}")
                    print(f"DriverApp [{self.manager.get_id()}]: State changed from {prev_state} to {new_state}")
                prev_state = new_state

        # Fallback: plugin dispatch for any custom/unknown state
        state = self.get_trip()['state']
        if state not in state_sequence:
            self._interaction_plugin.on_state(
                InteractionContext(
                    state=state,
                    extra={'time_since_last_event': time_since_last_event},
                )
            )
            new_state = self.get_trip()['state']
            if new_state != prev_state:
                # print(f"DriverAgentIndie [{self.unique_id}]: State changed from {prev_state} to {new_state}")
                print(f"DriverApp [{self.manager.get_id()}]: State changed from {prev_state} to {new_state}")

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

        trip = self.get_trip()
        # print(f"update_location_by_route: {self.unique_id}, current_loc={self.current_loc}, trip={trip}")
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

            if type(self.projected_path) == LineString:
                self.current_loc = mapping(Point(self.projected_path.boundary.geoms[0]))
            elif type(self.projected_path) == Point:
                self.current_loc = mapping(self.projected_path)

            # NOTE This will be called at every Step hence the projected_path will always be based on Latest info from Agent
            self.ping(self.current_time_str, current_loc=self.current_loc,
                    traversed_path=list(self.traversed_path.coords),
                    projected_path=list(self.projected_path.coords))


if __name__ == '__main__':
    credentials = {
        "email": "valuex@test.org",
        "password": "abcd1234"
    }

    driver_app = DriverApp(datetime.utcnow(), credentials)

    print(driver_app.manager)
    print(driver_app.trip)
