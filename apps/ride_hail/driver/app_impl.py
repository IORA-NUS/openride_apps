import requests, json, polyline, traceback
from random import choice
from http import HTTPStatus
from datetime import datetime
import shapely
from shapely.geometry.geo import mapping
import logging

from apps.config import settings
from apps.utils.utils import is_success

from apps.utils.user_registry import UserRegistry
from apps.driver_app.driver_manager import DriverManager
from apps.driver_app.driver_trip_manager import DriverTripManager
from apps.loc_service import OSRMClient
import paho.mqtt.client as paho

from apps.state_machine import RidehailDriverTripStateMachine
from apps.agent_core.runtime import RoleAppBase
from apps.ride_hail import RideHailActions, validate_requested_trip_payload


class DriverApp(RoleAppBase):

    exited_market = False

    def __init__(self, run_id, sim_clock, current_loc, credentials, profile, messenger):
        super().__init__(run_id, sim_clock, current_loc, messenger)
        self.credentials = credentials

        self.user = UserRegistry(sim_clock, credentials)

        self.driver = DriverManager(run_id, sim_clock, self.user, profile)

        self.topic_params = {
            f"{self.run_id}/{self.driver.get_id()}": self.message_handler
        }

        self.trip = DriverTripManager(run_id, sim_clock, self.user, self.messenger)

    def get_driver(self):
        return self.driver.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def login(self, sim_clock, current_loc, route):
        self.driver.login(sim_clock)
        self.create_new_unoccupied_trip(sim_clock, current_loc, route)

    def create_new_unoccupied_trip(self, sim_clock, current_loc, route):
        self.trip.create_new_unoccupied_trip(sim_clock, current_loc, self.driver.as_dict(), self.driver.vehicle, route)

    def logout(self, sim_clock, current_loc):
        logging.debug(f'logging out Driver {self.driver.get_id()}')
        try:
            self.trip.force_quit(sim_clock, current_loc)
        except Exception as e:
            logging.exception(str(e))

        try:
            self.driver.logout(sim_clock)
        except Exception as e:
            logging.warning(str(e))

        self.exited_market = True

    def refresh(self):
        self.trip.refresh()

    def ping(self, sim_clock, current_loc, publish=False, **kwargs):
        self.trip.ping(sim_clock, current_loc, **kwargs)

        if publish:
            if self.get_trip()['state'] in [RidehailDriverTripStateMachine.driver_moving_to_dropoff.name]:
                self.messenger.client.publish(
                    f'{self.run_id}/{self.get_trip()["passenger"]}',
                    json.dumps(
                        {
                            'action': 'driver_workflow_event',
                            'driver_id': self.driver.get_id(),
                            'data': {
                                'location': current_loc,
                            }

                        }
                    ),
                )

    def handle_requested_trip(self, sim_clock, current_loc, requested_trip):
        if self.trip.as_dict()['is_occupied'] == False:
            self.trip.end_trip(sim_clock, current_loc)

            self.trip.create_new_occupied_trip(sim_clock, current_loc, self.driver.as_dict(), self.driver.vehicle, requested_trip)
        else:
            logging.warning(f'Ignoring Assignment request: Driver {self.driver.get_id()} is already engaged in an Occupied trip')

    def message_handler(self, payload):
        if payload['action'] == RideHailActions.REQUESTED_TRIP:
            if validate_requested_trip_payload(payload) is False:
                logging.warning(f"Invalid requested-trip payload ignored: {payload=}")
                return

            requested_trip = payload['requested_trip']

            try:
                self.handle_requested_trip(
                    self.latest_sim_clock,
                    current_loc=self.latest_loc,
                    requested_trip=requested_trip,
                )
            except Exception as e:
                logging.warning(f"Driver failed to respond to trip Request {payload=}: {str(e)}")

        else:
            self.enqueue_message(payload)


if __name__ == '__main__':
    credentials = {
        "email": "valuex@test.org",
        "password": "abcd1234"
    }

    driver_app = DriverApp(datetime.utcnow(), credentials)

    print(driver_app.driver.driver)
