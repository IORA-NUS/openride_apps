import requests, json, polyline, traceback
from random import choice
from http import HTTPStatus
import shapely
from shapely.geometry.geo import mapping

import logging
from apps.config import settings
from apps.utils.utils import is_success

from apps.utils.user_registry import UserRegistry
from .manager import PassengerManager
from .trip_manager import PassengerTripManager
from apps.loc_service import OSRMClient
from apps.agent_core.base_app import BaseApp

from apps.state_machine import RidehailPassengerTripStateMachine
from apps.ride_hail import RideHailActions, validate_assigned_payload


class PassengerApp(BaseApp):

    exited_market = False

    def __init__(self, run_id, sim_clock, credentials, messenger, current_loc, profile, persona):
        super().__init__(run_id=run_id,
                         sim_clock=sim_clock,
                         credentials=credentials,
                         messenger=messenger,
                         current_loc=current_loc,
                         profile=profile,
                         persona=persona)
        self.trip = self.create_trip_manager()
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

    def create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def create_manager(self):
        return PassengerManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.profile,
            persona=self.persona
        )

    def create_trip_manager(self):
        return PassengerTripManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            messenger=self.messenger,
            persona=self.persona
        )

    # def get_manager(self):
    #     return self.manager.as_dict()

    def launch(self, sim_clock, current_loc, pickup_loc=None, dropoff_loc=None, trip_price=None):
        # self.manager.login(sim_clock)
        super().launch(sim_clock)  # Call BaseApp's launch method to login the manager

        if (pickup_loc is not None) and (dropoff_loc is not None):
            self.trip.create_new_trip_request(sim_clock, current_loc, self.manager.as_dict(), pickup_loc, dropoff_loc, trip_price)

    def close(self, sim_clock, current_loc):
        logging.debug(f'logging out Passenger {self.manager.get_id()}')
        try:
            self.trip.force_quit(sim_clock, current_loc)
        except Exception as e:
            logging.exception(str(e))

        super().close(sim_clock)  # Call BaseApp's close method to set exited_market = True
        # try:
        #     self.manager.logout(sim_clock)
        # except Exception as e:
        #     logging.warning(str(e))

        # self.exited_market = True

    def get_trip(self):
        return self.trip.as_dict()

    def ping(self, sim_clock, current_loc, **kwargs):
        self.trip.ping(sim_clock, current_loc, **kwargs)

    def refresh(self):
        self.trip.refresh()


    def message_handler(self, payload):
        if payload['action'] == RideHailActions.ASSIGNED:
            if validate_assigned_payload(payload) is False:
                logging.warning(f"Invalid assigned payload ignored: {payload=}")
                return

            if self.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.name:
                try:
                    self.trip.assign(
                        self.latest_sim_clock,
                        current_loc=self.latest_loc,
                        driver=payload['driver_id'],
                    )
                except Exception as e:
                    logging.warning(f"Assignment failed for {payload=}: {str(e)}")
                    self.handle_overbooking(self.latest_sim_clock, driver=payload['driver_id'])
            else:
                self.handle_overbooking(self.latest_sim_clock, driver=payload['driver_id'])
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

    # def update_current(self, sim_clock, current_loc):
    #     self.latest_sim_clock = sim_clock
    #     self.latest_loc = current_loc

    # def enqueue_message(self, payload):
    #     ''' '''
    #     self.message_queue.append(payload)

    # def dequeue_message(self):
    #     ''' '''
    #     try:
    #         return self.message_queue.pop(0)
    #     except: return None

    # def enfront_message(self, payload):
    #     self.message_queue.insert(0, payload)


if __name__ == '__main__':
    passenger_app = PassengerApp()

    print(passenger_app.registry.passenger)
