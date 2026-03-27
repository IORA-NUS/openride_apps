import requests, json, polyline, traceback
from random import choice
from http import HTTPStatus
import shapely
from shapely.geometry.geo import mapping

import logging
from apps.config import settings
from apps.utils.utils import is_success
from random import choice, randint, random


from apps.common.user_registry import UserRegistry
from apps.ride_hail.message_data_models import AssignedActionPayload
from .manager import PassengerManager
from .trip_manager import PassengerTripManager
from apps.loc_service import OSRMClient
from orsim.lifecycle import ORSimApp

from apps.ride_hail.statemachine import RidehailPassengerTripStateMachine, driver_passenger_interactions
# from apps.ride_hail import RideHailActions, validate_assigned_payload
from apps.ride_hail.statemachine import RideHailActions, RideHailEvents
from apps.ride_hail.message_data_models import AssignedActionPayload, DriverWorkflowPayload

from orsim.utils import WorkflowStateMachine
from orsim.messenger.interaction import message_handler, state_handler

from apps.utils.utils import id_generator, str_to_time, time_to_str #, cut
from apps.utils.excepions import WriteFailedException, RefreshException
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
from .driver_interaction_mixin import DriverInteractionMixin

class PassengerApp(ORSimApp, DriverInteractionMixin):

    @property
    def managed_statemachine(self):
        return RidehailPassengerTripStateMachine # <-- this must be a StateMachine class

    @property
    def interaction_ground_truth_list(self):
        return [driver_passenger_interactions]

    @property
    def runtime_behavior_schema(self):
        return {
            'pickup_loc': {'type': 'dict', 'required': True},
            'dropoff_loc': {'type': 'dict', 'required': True},
            'trip_price': {'type': 'number', 'required': True},
            'trip_request_time': {'type': 'integer', 'required': True},
            'transition_prob': {'type': 'list', 'required': True},
        }

    exited_market = False

    def __init__(self, run_id, sim_clock, behavior, messenger, agent_helper=None):
        super().__init__(run_id=run_id,
                         sim_clock=sim_clock,
                         behavior = behavior,
                         messenger=messenger,
                         agent_helper=agent_helper)
        self.trip = self.create_trip_manager()
        self.latest_sim_clock = sim_clock

        self.current_loc = self.behavior['pickup_loc']
        self.latest_loc = self.current_loc

        self.current_time = None
        self.current_time_str = None

        self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def _create_manager(self):
        return PassengerManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get('profile', {}),
            persona=self.behavior.get('persona', {})
        )

    def create_trip_manager(self):
        return PassengerTripManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            messenger=self.messenger,
            persona=self.behavior.get('persona', {})
        )

    def launch(self, sim_clock):
        super().launch(sim_clock)  # Call BaseApp's launch method to login the manager

        # if (self.behavior.get('pickup_loc') is not None) and (self.behavior.get('dropoff_loc') is not None):
        self.trip.create_new_trip_request(sim_clock, self.current_loc, self.manager.as_dict(), self.behavior.get('pickup_loc'), self.behavior.get('dropoff_loc'), self.behavior.get('trip_price'))

    def close(self, sim_clock):
        logging.debug(f'logging out Passenger {self.manager.get_id()}')
        try:
            # self.trip.force_quit(sim_clock, current_loc)
            self.trip.end_active_trip(sim_clock, self.current_loc, force=False)
        except Exception as e:
            logging.exception(str(e))

        super().close(sim_clock)  # Call BaseApp's close method to set exited_market = True

    def get_trip(self):
        return self.trip.as_dict()


    def ping(self, sim_clock, current_loc, **kwargs):
        self.trip.ping(sim_clock, current_loc, **kwargs)

    def refresh(self):
        self.trip.refresh()

    def handle_app_topic_messages(self, payload):

        if payload.get('action') == RideHailActions.ASSIGNED:
            parsed = AssignedActionPayload.parse(payload)
            if parsed is None:
                logging.warning(f"Invalid assigned payload ignored: {payload=}")
                return

            if self.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.name:
                try:
                    self.trip.assign(
                        self.latest_sim_clock,
                        current_loc=self.latest_loc,
                        driver=parsed.driver_id,
                    )
                except Exception as e:
                    logging.warning(f"Assignment failed for {payload=}: {str(e)}")
                    self.handle_overbooking(self.latest_sim_clock, driver=parsed.driver_id)
            else:
                self.handle_overbooking(self.latest_sim_clock, driver=parsed.driver_id)
        else:
            self.enqueue_message(payload)

    def handle_overbooking(self, sim_clock, driver):

        self.messenger.client.publish(
            f'{self.run_id}/{driver}',
            json.dumps(
                {
                    'action': 'passenger_workflow_event',
                    'passenger_id': self.manager.get_id(),
                    'data': {
                        'event': 'passenger_rejected_trip'
                    }
                }
            ),
        )

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        self.current_time = current_time
        self.current_time_str = time_to_str(current_time)

        # 1. Always refresh trip manager to sync InMemory States with DB
        if add_step_log_fn:
            add_step_log_fn(f'Before refresh')
        self.refresh() # Raises exception if unable to refresh

        # 1. DeQueue all messages and process them in sequence
        if add_step_log_fn:
            add_step_log_fn(f'Before consume_messages')
        self.consume_messages()
        # 2. based on current state, perform any workflow actions according to Agent behavior
        if add_step_log_fn:
            add_step_log_fn(f'Before perform_workflow_actions')
        self.perform_workflow_actions()




    def consume_messages(self):
        '''
        Consume messages. This ensures all the messages received between the two ticks are processed appropriately.
        Workflows as a consequence of events must be handled here.
        '''
        payload = self.dequeue_message()

        while payload is not None:
            try:
                if payload['action'] == RideHailActions.DRIVER_WORKFLOW_EVENT:
                    # if validate_driver_workflow_payload(payload) is False:
                    if DriverWorkflowPayload.parse(payload) is None:
                        logging.warning(f"Invalid driver workflow payload ignored: {payload=}")
                        payload = self.dequeue_message()
                        continue

                    trip = self.get_trip()
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
                                self.ping(self.current_time_str, current_loc=self.current_loc)
                        else:
                            logging.warning(f"WARNING: Mismatch {trip['driver']=} and {payload['driver_id']=}")
                    else:
                        logging.warning(f"WARNING: Passenger will not listen to Driver workflow events when {trip['state']=}")

                payload = self.dequeue_message()
            except WriteFailedException as e:
                self.enfront_message(payload)
                raise e # Important do not allow the while loop to continue
            except RefreshException as e:
                raise e # Important do not allow the while loop to continue
            except Exception as e:
                raise e # Important do not allow the while loop to continue


    def perform_workflow_actions(self):
        '''
        Executes workflow actions in a strict sequence using a for loop, allowing state changes between steps.
        '''
        passenger = self.get_manager()
        trip = self.get_trip()

        # 1. Check passenger online state
        if passenger['state'] != WorkflowStateMachine.online.name:
            raise Exception(f"{passenger['state'] = } is not valid")

        # 2. Check patience timeout and cancel trip if needed
        if (
            trip['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.name
            # and (self.behavior['trip_request_time'] + (self.behavior['profile']['patience'] / self.step_size) < self.current_time_step)
            and (self.behavior['trip_request_time'] + (self.behavior.get('profile', {}).get('patience', 0) / self.agent_helper.step_size) < self.agent_helper.current_time_step)
        ):
            logging.info(
                # f"Passenger {self.unique_id} has run out of patience. Requested: {self.behavior['trip_request_time']}, Max patience: {self.behavior['profile']['patience']/self.step_size} steps"
                f"Passenger {self.manager.get_id()} has run out of patience. Requested: {self.behavior['trip_request_time']}, Max patience: {self.behavior.get('profile', {}).get('patience', 0)/self.agent_helper.step_size} steps"
            )
            self.trip.cancel(self.current_time_str, current_loc=self.current_loc)

        # 3. Process trip state actions in strict sequence using a for loop
        state_sequence = [
            RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name,
            RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
            RidehailPassengerTripStateMachine.passenger_droppedoff.name,
        ]
        prev_state = trip['state']
        for state_name in state_sequence:
            state = self.get_trip()['state']
            if state == state_name:
                self._interaction_plugin.on_state(
                    InteractionContext(state=state)
                )
                new_state = self.get_trip()['state']
                if new_state != prev_state:
                    logging.info(f"PassengerAgentIndie [{self.manager.get_id()}]: State changed from {prev_state} to {new_state}")
                prev_state = new_state

        # Always process the current state (for plugin extensibility)
        state = self.get_trip()['state']
        if state not in state_sequence:
            self._interaction_plugin.on_state(
                InteractionContext(state=state)
            )



if __name__ == '__main__':
    passenger_app = PassengerApp()

    print(passenger_app.registry.passenger)
