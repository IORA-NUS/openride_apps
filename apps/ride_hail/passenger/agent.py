from apps.agent_core.interaction.decorators import message_handler, state_handler
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
from apps.state_machine import RidehailPassengerTripStateMachine #, WorkflowStateMachine
# from apps.agent_core.state_machine import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine

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
# from apps.agent_core.runtime import AgentRuntimeBase
# from apps.config import orsim_settings, passenger_settings

class PassengerAgentIndie(ORSimAgent):

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
            self.app = PassengerApp(run_id=self.run_id,
                                    sim_clock=self.get_current_time_str(),
                                    current_loc=self.current_loc,
                                    credentials=self.credentials,
                                    profile=self.behavior['profile'],
                                    messenger=self.messenger,
                                    persona=self.behavior.get('persona', {})
                                )
            print(f"PassengerApp initialized for {self.unique_id}")

            for topic, method in self.app.topic_params.items():
                self.register_message_handler(topic=topic, method=method)

            self._interaction_plugin = CallbackRouterInteractionPlugin(handler_obj=self)

        except Exception as e:
            print(f"Exception during PassengerAgentIndie initialization: {str(e)}")

            logging.exception(f"{self.unique_id = }: {str(e)}")
            self.agent_failed = True

    def process_payload(self, payload: Dict[str, Any]) -> bool:
        did_step: bool = False

        if (payload.get("action") == "step") or (payload.get("action") == "init"):
            self.add_step_log("Before entering_market")
            print(f"Processing step for PassengerAgentIndie {self.unique_id} at time_step {payload.get('time_step')}")
            self.entering_market(payload.get("time_step"))
            self.add_step_log("After entering_market")
            print(f"Finished processing step for PassengerAgentIndie {self.unique_id} at time_step {payload.get('time_step')}")

            # if self.is_active():
            if self.active:
                try:
                    self.add_step_log("Before step")
                    did_step = self.step(payload.get("time_step"))
                    self.add_step_log("After step")
                    self.failure_count = 0
                    self.failure_log = {}
                except Exception:
                    self.failure_log[self.failure_count] = traceback.format_exc()
                    self.failure_count += 1

            self.add_step_log("Before exiting_market")
            self.exiting_market()
            self.add_step_log("After exiting_market")
        else:
            logging.error(f"{payload = }")

        return did_step

    def entering_market(self, time_step):
        # if time_step == self.behavior['trip_request_time']:
        if (self.active == False) and (time_step == self.behavior['trip_request_time']):
            # print('Enter Market')
            # print(self.behavior)
            self.app.launch(sim_clock=self.get_current_time_str(),
                            current_loc=self.current_loc,
                            pickup_loc=self.pickup_loc,
                            dropoff_loc=self.dropoff_loc,
                            trip_price=self.behavior.get('trip_price'))

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
            elif (self.current_time_step > self.behavior['trip_request_time']) and \
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
        self.app.close(self.get_current_time_str(), current_loc=self.current_loc)

    def estimate_next_event_time(self):
        ''' '''
        # return self.current_time
        next_event_time =  min(self.app.manager.estimate_next_event_time(self.current_time),
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
        '''
        Consume messages. This ensures all the messages received between the two ticks are processed appropriately.
        Workflows as a consequence of events must be handled here.
        '''
        payload = self.app.dequeue_message()

        while payload is not None:
            try:
                if payload['action'] == RideHailActions.DRIVER_WORKFLOW_EVENT:
                    if validate_driver_workflow_payload(payload) is False:
                        logging.warning(f"Invalid driver workflow payload ignored: {payload=}")
                        payload = self.app.dequeue_message()
                        continue

                    trip = self.app.get_trip()
                    channel_open = RidehailPassengerTripStateMachine.is_driver_channel_open(trip['state'])
                    driver_id_match = trip['driver'] == payload['driver_id']

                    if channel_open:
                        if driver_id_match:
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
                            logging.warning(f"WARNING: Mismatch {trip['driver']=} and {payload['driver_id']=}")
                    else:
                        logging.warning(f"WARNING: Passenger will not listen to Driver workflow events when {trip['state']=}")

                payload = self.app.dequeue_message()
            except WriteFailedException as e:
                self.app.enfront_message(payload)
                raise e # Important do not allow the while loop to continue
            except RefreshException as e:
                raise e # Important do not allow the while loop to continue
            except Exception as e:
                raise e # Important do not allow the while loop to continue


    def perform_workflow_actions(self):
        '''
        Executes workflow actions in a strict sequence using a for loop, allowing state changes between steps.
        '''
        passenger = self.app.get_manager()
        trip = self.app.get_trip()
        now_str = self.get_current_time_str()

        # 1. Check passenger online state
        if passenger['state'] != WorkflowStateMachine.online.name:
            raise Exception(f"{passenger['state'] = } is not valid")

        # 2. Check patience timeout and cancel trip if needed
        if (
            trip['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.name
            and (self.behavior['trip_request_time'] + (self.behavior['profile']['patience'] / self.step_size) < self.current_time_step)
        ):
            logging.info(
                f"Passenger {self.unique_id} has run out of patience. Requested: {self.behavior['trip_request_time']}, Max patience: {self.behavior['profile']['patience']/self.step_size} steps"
            )
            self.app.trip.cancel(now_str, current_loc=self.current_loc)

        # 3. Process trip state actions in strict sequence using a for loop
        state_sequence = [
            RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name,
            RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
            RidehailPassengerTripStateMachine.passenger_droppedoff.name,
        ]
        prev_state = trip['state']
        for state_name in state_sequence:
            state = self.app.get_trip()['state']
            if state == state_name:
                self._interaction_plugin.on_state(
                    InteractionContext(state=state)
                )
                new_state = self.app.get_trip()['state']
                if new_state != prev_state:
                    logging.info(f"PassengerAgentIndie [{self.unique_id}]: State changed from {prev_state} to {new_state}")
                prev_state = new_state

        # Always process the current state (for plugin extensibility)
        state = self.app.get_trip()['state']
        if state not in state_sequence:
            self._interaction_plugin.on_state(
                InteractionContext(state=state)
            )


    # ...existing code...

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CONFIRMED_TRIP)
    def _on_driver_confirmed_trip(self, payload, data):
        self.app.trip.driver_confirmed_trip(
            self.get_current_time_str(),
            self.current_loc,
            data.get('estimated_time_to_arrive', 0),
        )

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP)
    def _on_driver_arrived_for_pickup(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_arrived_for_pickup(self.get_current_time_str(), self.current_loc, data.get('driver_trip_id'))

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_MOVE_FOR_DROPOFF)
    def _on_driver_move_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_move_for_dropoff(self.get_current_time_str(), self.current_loc, route=data['planned_route'])

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF)
    def _on_driver_arrived_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_arrived_for_dropoff(self.get_current_time_str(), self.current_loc)

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_WAITING_FOR_DROPOFF)
    def _on_driver_waiting_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.app.trip.driver_waiting_for_dropoff(self.get_current_time_str(), self.current_loc)

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CANCELLED_TRIP)
    def _on_driver_cancelled_trip(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location', self.current_loc)
        self.app.trip.driver_cancelled_trip(self.get_current_time_str(), self.current_loc)

    @state_handler(RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name)
    def _on_state_received_trip_confirmation(self):
        if random() <= self.get_transition_probability(('accept', self.app.get_trip()['state']), 1):
            self.app.trip.accept(self.get_current_time_str(), current_loc=self.current_loc)
        else:
            self.app.trip.reject(self.get_current_time_str(), current_loc=self.current_loc)

    @state_handler(RidehailPassengerTripStateMachine.passenger_accepted_trip.name)
    def _on_state_accepted_trip(self):
        self.app.trip.wait_for_pickup(self.get_current_time_str(), current_loc=self.current_loc)

    @state_handler(RidehailPassengerTripStateMachine.passenger_droppedoff.name)
    def _on_state_droppedoff(self):
        self.app.trip.end_trip(self.get_current_time_str(), current_loc=self.current_loc)


if __name__ == '__main__':

    agent = PassengerAgentIndie('001', None)
    agent.step()


