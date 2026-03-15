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
from .manager import DriverManager
from .trip_manager import DriverTripManager
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
        # print(f"DriverApp initialized with driver ID: {self.driver.get_id()}")

        self.trip = DriverTripManager(run_id, sim_clock, self.user, self.messenger)

        # print(f"DriverApp initialized with trip ID: {self.trip.trip['_id'] if self.trip.trip else 'None'}")

    def get_driver(self):
        return self.driver.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def login(self, sim_clock, current_loc, route):
        self.driver.login(sim_clock)
        print(f"DriverApp.login: Driver {self.driver.get_id()} logged in at sim_clock {sim_clock} with location {current_loc}")
        self.create_new_unoccupied_trip(sim_clock, current_loc, route)
        print(f"DriverApp.login: Created new unoccupied trip for driver {self.driver.get_id()} at sim_clock {sim_clock} with location {current_loc} and route {route}")

    def create_new_unoccupied_trip(self, sim_clock, current_loc, route):
        try:
            self.trip.create_new_unoccupied_trip(sim_clock, current_loc, self.driver.as_dict(), self.driver.vehicle.as_dict(), route)
        except Exception as e:
            print(f"Exception in create_new_unoccupied_trip for driver {self.driver.get_id()}: {str(e)}")
            traceback.print_exc()

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

            self.trip.create_new_occupied_trip(sim_clock, current_loc, self.driver.as_dict(), self.driver.vehicle.as_dict(), requested_trip)
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
