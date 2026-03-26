
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
from .manager import DriverManager
from .trip_manager import DriverTripManager
from apps.loc_service import OSRMClient
from orsim.lifecycle import ORSimApp

from apps.ride_hail.statemachine import RidehailDriverTripStateMachine, driver_passenger_interactions
from orsim.utils import WorkflowStateMachine
from orsim.messenger.interaction import message_handler, state_handler

import haversine as hs
from random import choice, randint, random

from apps.utils.excepions import WriteFailedException, RefreshException, HandlerValidationException
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
from apps.ride_hail import RideHailActions, RideHailEvents, validate_passenger_workflow_payload

from shapely.geometry import Point, mapping
from shapely.geometry.linestring import LineString

from apps.utils.utils import id_generator, str_to_time, time_to_str #, cut
from apps.loc_service import TaxiStop, BusStop, cut, cut_route, create_route, get_tentative_travel_time

from .passenger_interaction_mixin import PassengerInteractionMixin
# from apps.messenger_service import Messenger

class DriverApp(ORSimApp, PassengerInteractionMixin):

    @property
    def managed_statemachine(self):
        return RidehailDriverTripStateMachine # <-- this must be a StateMachine class

    @property
    def interaction_ground_truth_list(self):
        return [driver_passenger_interactions]


    def __init__(self, run_id, sim_clock, credentials, messenger, current_loc, profile, persona, agent_helper=None):
        super().__init__(run_id=run_id,
                         sim_clock=sim_clock,
                         credentials=credentials,
                         messenger=messenger,
                         current_loc=current_loc,
                         profile=profile,
                         persona=persona,
                         agent_helper=agent_helper)
        self.trip = self.create_trip_manager()

        self.current_time = None
        self.current_time_str = None

        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

        self.active_route = None # shapely.geometry.LineString
        self.traversed_path = None # shapely.geometry.LineString
        self.projected_path = None # shapely.geometry.LineString

        self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

    def create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def create_manager(self):
        return DriverManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.profile,
            persona=self.persona)

    def create_trip_manager(self):
        return DriverTripManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            messenger=self.messenger,
            persona=self.persona)

    # def get_manager(self):
    #     # return self.manager.resource.as_dict()
        # return self.manager.as_dict()

    def launch(self, sim_clock, current_loc): #, route):
        ''' '''
        # self.manager.login(sim_clock)
        super().launch(sim_clock)  # Call BaseApp's launch method to login the manager
        # self.create_new_unoccupied_trip(sim_clock, current_loc)
        # self.trip.look_for_job(sim_clock, current_loc, route)
        if self.agent_helper.get_behavior_detail('action_when_free') == 'random_walk':
            # self.set_route(self.current_loc, self.behavior['empty_dest_loc'])
            self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.agent_helper.get_behavior_detail('empty_dest_loc'))
        elif self.agent_helper.get_behavior_detail('action_when_free') == 'stay':
            # self.set_route(self.current_loc, None)
            self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, None)
        self.create_new_unoccupied_trip(sim_clock, current_loc, self.active_route)


    def close(self, sim_clock, current_loc):
        ''' '''
        logging.debug(f'logging out Driver {self.manager.get_id()}')
        try:
            # self.trip.end_trip(sim_clock, current_loc, force_quit=True)
            self.trip.force_quit(sim_clock, current_loc)
        except Exception as e:
            logging.exception(str(e))

        super().close(sim_clock)  # Call BaseApp's close method to set exited_market = True
        # try:
        #     self.manager.logout(sim_clock)
        # except Exception as e:
        #     logging.warning(str(e))

        # # self.messenger.disconnect()

        # self.exited_market = True

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

    # def create_new_unoccupied_trip(self, sim_clock, current_loc):
    #     self.trip.create_new_unoccupied_trip(sim_clock, current_loc, self.manager.as_dict(), self.manager.vehicle)
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
            # self.trip.end_trip(sim_clock, current_loc, force_quit=False)
            self.trip.end_trip(sim_clock, current_loc)

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

        # print('driver_app received_message', payload)
        print(type(payload), payload)

        if payload['action'] == 'requested_trip':
            passenger_id = payload['passenger_id']
            requested_trip = payload['requested_trip']

            try:
                self.handle_requested_trip(self.latest_sim_clock,
                                            current_loc=self.latest_loc,
                                            requested_trip=requested_trip)
            except Exception as e:
                # logging.exception(traceback.format_exc())
                logging.warning(f"Driver failed to respond to trip Request {payload=}: {str(e)}")
                # raise e

        else:
            self.enqueue_message(payload)


    def execute_step_actions(self, current_time, add_step_log_fn=None):
        self.current_time = current_time
        self.current_time_str = time_to_str(current_time)

        # 1. Always refresh trip manager to sync InMemory States with DB
        if add_step_log_fn:
            add_step_log_fn('Before refresh')
        else:
            self.add_step_log('Before refresh')
        self.refresh() # Raises exception if unable to refresh
        ### Driver has likely moved between the ticks, so update their current loc
        # self.update_location()
        if add_step_log_fn:
            add_step_log_fn('Before update_location_by_route')
        else:
            self.add_step_log('Before update_location_by_route')
        self.update_location_by_route()

        # 1. DeQueue all messages and process them in sequence
        if add_step_log_fn:
            add_step_log_fn('Before consume_messages')
        else:
            self.add_step_log('Before consume_messages')
        self.consume_messages()
        # 2. based on current state, perform any workflow actions according to Agent behavior
        if add_step_log_fn:
            add_step_log_fn('Before perform_workflow_actions')
        else:
            self.add_step_log('Before perform_workflow_actions')
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
                    if not validate_passenger_workflow_payload(payload):
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
        # time_since_last_event = (
        #     datetime.strptime(self.get_current_time_str(), "%a, %d %b %Y %H:%M:%S GMT") -
        #     datetime.strptime(trip['_updated'], "%a, %d %b %Y %H:%M:%S GMT")
        # ).total_seconds()
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
            self.ping(self.current_time_str, current_loc=self.current_loc,
                    traversed_path=list(self.traversed_path.coords),
                    projected_path=list(self.projected_path.coords))
            # except Exception as e:
            #     # logging.exception(traceback.format_exc())
            #     # logging.exception(str(e))
            #     logging.warning(str(e))
            #     raise e

    # @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_CONFIRMED_TRIP)
    # def _on_passenger_confirmed_trip(self, payload, data):
    #     self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.get_trip()['pickup_loc'])
    #     self.trip.passenger_confirmed_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    # @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_REJECTED_TRIP)
    # def _on_passenger_rejected_trip(self, payload, data):
    #     self.trip.force_quit(self.current_time_str, current_loc=self.current_loc)

    #     if self.action_when_free == 'random_walk':
    #         self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.agent_helper.get_behavior_detail('empty_dest_loc')) # self.behavior['empty_dest_loc'])
    #     elif self.action_when_free == 'stay':
    #         self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, None)

    #     self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    # @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP)
    # def _on_passenger_acknowledge_pickup(self, payload, data):
    #     self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.get_trip()['dropoff_loc'])
    #     self.trip.passenger_acknowledge_pickup(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    # @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF)
    # def _on_passenger_acknowledge_dropoff(self, payload, data):
    #     self.trip.passenger_acknowledge_dropoff(self.current_time_str, current_loc=self.current_loc)

    # @state_handler(RidehailDriverTripStateMachine.driver_looking_for_job.name)
    # def _on_state_looking_for_job(self, time_since_last_event):
    #     if type(self.projected_path) == Point:
    #         self.trip.end_trip(self.current_time_str, current_loc=self.current_loc)
    #         self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.get_random_location())
    #         self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    # @state_handler(RidehailDriverTripStateMachine.driver_received_trip.name)
    # def _on_state_received_trip(self, time_since_last_event):
    #     print(f"DriverApp [{self.manager.get_id()}]: Received Trip Request.")
    #     if random() <= self.agent_helper.get_transition_probability(('accept', self.get_trip()['state']), 1):
    #         estimated_time_to_arrive = get_tentative_travel_time(self.current_loc, self.get_trip()['pickup_loc'])
    #         self.trip.confirm(self.current_time_str, current_loc=self.current_loc, estimated_time_to_arrive=estimated_time_to_arrive)
    #         print(f"DriverApp [{self.manager.get_id()}]: Trip Confirmed.")
    #     else:
    #         self.trip.reject(self.current_time_str, current_loc=self.current_loc)
    #         self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)
    #         print(f"DriverApp [{self.manager.get_id()}]: Trip Rejected.")

    #     print(f"DriverApp [{self.manager.get_id()}]: after _on_state_received_trip Current Trip State: {self.get_trip()['state']}")

    # @state_handler(RidehailDriverTripStateMachine.driver_moving_to_pickup.name)
    # def _on_state_moving_to_pickup(self, time_since_last_event):
    #     distance = hs.haversine(
    #         reversed(self.current_loc['coordinates'][:2]),
    #         reversed(self.get_trip()['pickup_loc']['coordinates'][:2]),
    #         unit=hs.Unit.METERS,
    #     )
    #     if distance < 100:
    #         self.trip.wait_to_pickup(self.current_time_str, current_loc=self.current_loc)

    # @state_handler(RidehailDriverTripStateMachine.driver_pickedup.name)
    # def _on_state_pickedup(self, time_since_last_event):
    #     if time_since_last_event >= self.agent_helper.get_behavior_detail('transition_time_pickup'): #self.behavior['transition_time_pickup']:
    #         self.trip.move_to_dropoff(self.current_time_str, current_loc=self.current_loc)

    # @state_handler(RidehailDriverTripStateMachine.driver_moving_to_dropoff.name)
    # def _on_state_moving_to_dropoff(self, time_since_last_event):
    #     distance = hs.haversine(
    #         reversed(self.current_loc['coordinates'][:2]),
    #         reversed(self.get_trip()['dropoff_loc']['coordinates'][:2]),
    #         unit=hs.Unit.METERS,
    #     )
    #     if distance < 100:
    #         self.trip.wait_to_dropoff(self.current_time_str, current_loc=self.current_loc)

    # @state_handler(RidehailDriverTripStateMachine.driver_droppedoff.name)
    # def _on_state_droppedoff(self, time_since_last_event):
    #     if time_since_last_event >= self.agent_helper.get_behavior_detail('transition_time_dropoff'): #self.behavior['transition_time_dropoff']:
    #         self.trip.end_trip(self.current_time_str, current_loc=self.current_loc)

    #         if self.agent_helper.get_behavior_detail('action_when_free') == 'random_walk':
    #             self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.agent_helper.get_behavior_detail('empty_dest_loc'))
    #         elif self.agent_helper.get_behavior_detail('action_when_free') == 'stay':
    #             self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, None)

    #         self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)



if __name__ == '__main__':
    credentials = {
        "email": "valuex@test.org",
        "password": "abcd1234"
    }

    driver_app = DriverApp(datetime.utcnow(), credentials)

    print(driver_app.manager)
    print(driver_app.trip)
