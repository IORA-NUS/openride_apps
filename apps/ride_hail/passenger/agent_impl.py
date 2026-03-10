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

from .app import PassengerApp
from apps.utils.utils import id_generator #, cut
from apps.state_machine import RidehailPassengerTripStateMachine, WorkflowStateMachine
from apps.loc_service import OSRMClient, cut

from apps.loc_service import TaxiStop, BusStop

# from apps.messenger_service import Messenger

# Passenger agent will be called to apply behavior at every step
# At each step, the Agent will process list of collected messages in the app.
# from apps.orsim import ORSimAgent
from orsim import ORSimAgent

from apps.utils.excepions import WriteFailedException, RefreshException
from apps.utils.interaction_plugin import CallbackRouterInteractionPlugin, InteractionContext
from apps.ride_hail import RideHailActions, RideHailEvents, validate_driver_workflow_payload
from apps.agent_core.runtime import AgentRuntimeBase
# from apps.config import orsim_settings, passenger_settings

class PassengerAgentIndie(AgentRuntimeBase, ORSimAgent):

    current_loc = None
    current_time_step = None
    prev_time_step = None
    elapsed_duration_steps = None
    # projected_path = None # shapely.geometry.LineString

    # def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler_id, behavior, orsim_settings):
    #     super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler_id, behavior, orsim_settings)
    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior): #, orsim_settings):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior) #, orsim_settings)

        self.step_size = self.orsim_settings['STEP_INTERVAL'] # NumSeconds per each step.

        self.current_loc = self.behavior['pickup_loc']
        self.pickup_loc = self.behavior['pickup_loc']
        self.dropoff_loc = self.behavior['dropoff_loc']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }
        # self.timeout_error = False
        self.failure_count = 0
        self.failure_log = {}

        try:
            self.app = PassengerApp(self.run_id,
                                    self.get_current_time_str(),
                                    self.current_loc,
                                    credentials=self.credentials,
                                    passenger_profile=self.behavior['profile'],
                                    messenger=self.messenger)

            for topic, method in self.app.topic_params.items():
                self.register_message_handler(topic=topic, method=method)

            self._register_interaction_callbacks()

        except Exception as e:
            logging.exception(f"{self.unique_id = }: {str(e)}")
            self.agent_failed = True

    def entering_market(self, time_step):
        # if time_step == self.behavior['trip_request_time']:
        if (self.active == False) and (time_step == self.behavior['trip_request_time']):
            # print('Enter Market')
            # print(self.behavior)
            self.app.login(self.get_current_time_str(), self.current_loc, self.pickup_loc, self.dropoff_loc, trip_price=self.behavior.get('trip_price'))
            self.active = True
            return True
        else:
            return False

    def exiting_market(self):
        failure_threshold = 3

        if self.failure_count > failure_threshold:
            logging.warning(f'Shutting down passenger {self.app.passenger.get_id()} due to too many failures')
            logging.warning(json.dumps(self.failure_log, indent=2))
            self.shutdown()
            return True
        # elif self.timeout_error:
        #     self.shutdown()
        #     return True
        else:
            if self.app.exited_market:
                return False
            elif (self.current_time_step > self.behavior['trip_request_time']) and \
                    (self.app.get_trip() is not None) and \
                    (self.app.get_trip()['state'] in [RidehailPassengerTripStateMachine.passenger_completed_trip.name,
                                                    RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,]):
                self.shutdown()
                return True

            else:
                return False

    def logout(self):
        self.app.logout(self.get_current_time_str(), current_loc=self.current_loc)

    def estimate_next_event_time(self):
        ''' '''
        # return self.current_time
        next_event_time =  min(self.app.passenger.estimate_next_event_time(self.current_time),
                                self.app.trip.estimate_next_event_time(self.current_time))

        return next_event_time

    def step(self, time_step):
        self.add_step_log(f'In step')
        self.app.update_current(self.get_current_time_str(), self.current_loc)

        if (self.current_time_step % self.behavior['steps_per_action'] == 0) and \
                    (random() <= self.behavior['response_rate']) and \
                    (self.next_event_time <= self.current_time):

            # 1. Always refresh trip manager to sync InMemory States with DB
            self.add_step_log(f'Before refresh')
            self.app.refresh() # Raises exception if unable to refresh

            # 1. DeQueue all messages and process them in sequence
            self.add_step_log(f'Before consume_messages')
            self.consume_messages()
            # 2. based on current state, perform any workflow actions according to Agent behavior
            self.add_step_log(f'Before perform_workflow_actions')
            self.perform_workflow_actions()

            return True
        else:
            return False


    def consume_messages(self):
        ''' '''
        payload = self.app.dequeue_message()

        while payload is not None:
            try:
                if payload['action'] == RideHailActions.DRIVER_WORKFLOW_EVENT:
                    if validate_driver_workflow_payload(payload) is False:
                        logging.warning(f"Invalid driver workflow payload ignored: {payload=}")
                        payload = self.app.dequeue_message()
                        continue

                    if RidehailPassengerTripStateMachine.is_driver_channel_open(self.app.get_trip()['state']):
                        if self.app.get_trip()['driver'] == payload['driver_id']:
                            driver_data = payload['data']
                            handled = self._interaction_plugin.on_message(
                                InteractionContext(
                                    action=RideHailActions.DRIVER_WORKFLOW_EVENT,
                                    event=driver_data.get('event'),
                                    payload=payload,
                                    data=driver_data,
                                )
                            )

                            if (handled == False) and (driver_data.get('location') is not None):
                                self.current_loc = driver_data.get('location')
                                self.app.ping(self.get_current_time_str(), current_loc=self.current_loc)

                        else:
                            logging.warning(f"WARNING: Mismatch {self.app.get_trip()['driver']=} and {payload['driver_id']=}")
                    else:
                        logging.warning(f"WARNING: Passenger will not listen to Driver workflow events when {self.app.get_trip()['state']=}")

                payload = self.app.dequeue_message()
            except WriteFailedException as e:
                # push message back into fornt of queue for processing in next step
                self.app.enfront_message(payload)
                raise e # Important do not allow the while loop to continue
            except RefreshException as e:
                raise e # Important do not allow the while loop to continue
            except Exception as e:
                raise e # Important do not allow the while loop to continue

    def perform_workflow_actions(self):
        ''' '''
        passenger = self.app.get_passenger()
        #### NOTE THIS is a mistake. Should use the last transition time instead of the last waypoint (_updated) time
        try:
            time_since_last_event = (datetime.strptime(self.get_current_time_str(), "%a, %d %b %Y %H:%M:%S GMT") - \
                                datetime.strptime(self.app.get_trip()['_updated'], "%a, %d %b %Y %H:%M:%S GMT")
                                ).total_seconds()
        except Exception as e:
            # logging.warning(self.behavior)
            # logging.exception(str(e))
            raise e

        if passenger['state'] != WorkflowStateMachine.online.name:
            # try:
            raise Exception(f"{passenger['state'] = } is not valid")
            # except Exception as e:
            #     # logging.exception(str(e))
            #     raise e

        elif (self.app.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.name) and \
                (self.behavior['trip_request_time'] + (self.behavior['profile']['patience']/self.step_size) < self.current_time_step):
            logging.info(f"Passenger {self.unique_id} has run out of patience. Requested: {self.behavior['trip_request_time']}, Max patience: {self.behavior['profile']['patience']/self.step_size} steps")
            self.app.trip.cancel(self.get_current_time_str(), current_loc=self.current_loc,)

        else:
            self._interaction_plugin.on_state(
                InteractionContext(state=self.app.get_trip()['state'])
            )


    def _register_interaction_callbacks(self):
        self._interaction_plugin = CallbackRouterInteractionPlugin()
        # Backward compatibility for tests and any code still reading this attribute.
        self._interaction_callbacks = self._interaction_plugin.router

        self._interaction_plugin.register_message(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CONFIRMED_TRIP, self._on_driver_confirmed_trip)
        self._interaction_plugin.register_message(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP, self._on_driver_arrived_for_pickup)
        self._interaction_plugin.register_message(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_MOVE_FOR_DROPOFF, self._on_driver_move_for_dropoff)
        self._interaction_plugin.register_message(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF, self._on_driver_arrived_for_dropoff)
        self._interaction_plugin.register_message(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_WAITING_FOR_DROPOFF, self._on_driver_waiting_for_dropoff)
        self._interaction_plugin.register_message(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CANCELLED_TRIP, self._on_driver_cancelled_trip)

        self._interaction_plugin.register_state(RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name, self._on_state_received_trip_confirmation)
        self._interaction_plugin.register_state(RidehailPassengerTripStateMachine.passenger_accepted_trip.name, self._on_state_accepted_trip)
        self._interaction_plugin.register_state(RidehailPassengerTripStateMachine.passenger_droppedoff.name, self._on_state_droppedoff)

    def _on_driver_confirmed_trip(self, payload, data):
        self.app.trip.driver_confirmed_trip(
            self.get_current_time_str(),
            self.current_loc,
            data.get('estimated_time_to_arrive', 0),
        )

    def _on_driver_arrived_for_pickup(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_arrived_for_pickup(self.get_current_time_str(), self.current_loc, data.get('driver_trip_id'))

    def _on_driver_move_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_move_for_dropoff(self.get_current_time_str(), self.current_loc, route=data['planned_route'])

    def _on_driver_arrived_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_arrived_for_dropoff(self.get_current_time_str(), self.current_loc)

    def _on_driver_waiting_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_waiting_for_dropoff(self.get_current_time_str(), self.current_loc)

    def _on_driver_cancelled_trip(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location', self.current_loc)
        self.app.trip.driver_cancelled_trip(self.get_current_time_str(), self.current_loc)

    def _on_state_received_trip_confirmation(self):
        if random() <= self.get_transition_probability(('accept', self.app.get_trip()['state']), 1):
            self.app.trip.accept(self.get_current_time_str(), current_loc=self.current_loc)
        else:
            self.app.trip.reject(self.get_current_time_str(), current_loc=self.current_loc)

    def _on_state_accepted_trip(self):
        self.app.trip.wait_for_pickup(self.get_current_time_str(), current_loc=self.current_loc)

    def _on_state_droppedoff(self):
        self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc)


if __name__ == '__main__':

    agent = PassengerAgent('001', None)
    agent.step()


