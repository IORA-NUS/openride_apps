import requests, json, polyline, traceback
from random import choice
from http import HTTPStatus
import shapely
from shapely.geometry.geo import mapping

import logging
from apps.config import settings
from apps.utils.utils import is_success

from apps.utils.user_registry import UserRegistry
from apps.passenger_app.passenger_manager import PassengerManager
from apps.passenger_app.passenger_trip_manager import PassengerTripManager
from apps.loc_service import OSRMClient
import paho.mqtt.client as paho

from apps.state_machine import RidehailPassengerTripStateMachine
from apps.agent_core.runtime import RoleAppBase
from apps.ride_hail import RideHailActions, validate_assigned_payload


class PassengerApp(RoleAppBase):

    exited_market = False

    def __init__(self, run_id, sim_clock, current_loc, credentials, passenger_profile, messenger):
        super().__init__(run_id, sim_clock, current_loc, messenger)
        self.credentials = credentials

        self.user = UserRegistry(sim_clock, credentials)

        self.passenger = PassengerManager(run_id, sim_clock, self.user, passenger_profile)
        self.topic_params = {
            f"{self.run_id}/{self.passenger.get_id()}": self.message_handler
        }

        self.trip = PassengerTripManager(run_id, sim_clock, self.user, self.messenger)

    def get_passenger(self):
        return self.passenger.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def login(self, sim_clock, current_loc, pickup_loc=None, dropoff_loc=None, trip_price=None):
        self.passenger.login(sim_clock)

        if (pickup_loc is not None) and (dropoff_loc is not None):
            self.trip.create_new_trip_request(sim_clock, current_loc, self.passenger.as_dict(), pickup_loc, dropoff_loc, trip_price)

    def logout(self, sim_clock, current_loc):
        logging.debug(f'logging out Passenger {self.passenger.get_id()}')
        try:
            self.trip.force_quit(sim_clock, current_loc)
        except Exception as e:
            logging.exception(str(e))

        try:
            self.passenger.logout(sim_clock)
        except Exception as e:
            logging.warning(str(e))

        self.exited_market = True

    def ping(self, sim_clock, current_loc, **kwargs):
        self.trip.ping(sim_clock, current_loc, **kwargs)

    def refresh(self):
        self.trip.refresh()

    def handle_overbooking(self, sim_clock, driver):

        self.messenger.client.publish(
            f'{self.run_id}/{driver}',
            json.dumps(
                {
                    'action': 'passenger_workflow_event',
                    'passenger_id': self.passenger.get_id(),
                    'data': {
                        'event': 'passenger_rejected_trip'
                    }
                }
            ),
        )

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


if __name__ == '__main__':
    passenger_app = PassengerApp()

    print(passenger_app.registry.passenger)
