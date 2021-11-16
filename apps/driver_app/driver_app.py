
import requests, json, polyline
from random import choice
from http import HTTPStatus
from datetime import datetime
import shapely
from shapely.geometry.geo import mapping
import logging

from apps.config import settings
from apps.utils.utils import is_success

from apps.utils.user_registry import UserRegistry
from .driver_manager import DriverManager
from .driver_trip_manager import DriverTripManager
from apps.loc_service import OSRMClient
import paho.mqtt.client as paho

from apps.state_machine import RidehailDriverTripStateMachine

from apps.messenger_service import Messenger

class DriverApp:

    def __init__(self, run_id, sim_clock, current_loc, credentials, driver_settings):
        self.run_id = run_id
        self.credentials = credentials

        self.user = UserRegistry(sim_clock, credentials)

        self.driver = DriverManager(run_id, sim_clock, self.user, driver_settings)

        self.messenger = Messenger(credentials, f"{self.run_id}/{self.driver.get_id()}", self.on_receive_message)

        self.trip = DriverTripManager(run_id, sim_clock, self.user, self.messenger)

        self.message_queue = [] # message_queue is used to support buffering of messages between each Simulation step to provide for an approximation of actual driver behavior

    def get_driver(self):
        return self.driver.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def login(self, sim_clock, current_loc, route):
        ''' '''
        self.driver.login(sim_clock)
        # self.create_new_unoccupied_trip(sim_clock, current_loc)
        # self.trip.look_for_job(sim_clock, current_loc, route)
        self.create_new_unoccupied_trip(sim_clock, current_loc, route)

    # def create_new_unoccupied_trip(self, sim_clock, current_loc):
    #     self.trip.create_new_unoccupied_trip(sim_clock, current_loc, self.driver.as_dict(), self.driver.vehicle)
    def create_new_unoccupied_trip(self, sim_clock, current_loc, route):
        self.trip.create_new_unoccupied_trip(sim_clock, current_loc, self.driver.as_dict(), self.driver.vehicle, route)

    def logout(self, sim_clock, current_loc):
        ''' '''
        logging.info(f'logging out Driver {self.driver.get_id()}')
        try:
            self.trip.end_trip(sim_clock, current_loc, force_quit=True)
            self.driver.logout(sim_clock)
        except Exception as e:
            logging.exception(str(e))

    def refresh(self):
        ''' Sync ALL inMemory State with the db State'''
        # Driver
        # No need to refresh driver at every step
        # self.driver.refresh()

        self.trip.refresh()
        # raise exception if unable to refresh

    def ping(self, sim_clock, current_loc, publish=False, **kwargs):
        ''' '''
        self.trip.ping(sim_clock, current_loc, **kwargs) # Raises exception if ping fails

        if publish:
            if self.get_trip()['state'] in [RidehailDriverTripStateMachine.driver_moving_to_dropoff.identifier]:
                self.messenger.client.publish(f'{self.run_id}/{self.get_trip()["passenger"]}',
                                    json.dumps({
                                        'action': 'driver_workflow_event',
                                        'driver_id': self.driver.get_id(),
                                        'data': {
                                            'location': current_loc,
                                        }

                                    })
                                )

    def handle_requested_trip(self, sim_clock, current_loc, requested_trip):
        '''
        Check for any existing trip
        If current trip is un occupied, end the trip
          - Note Driver will be without trip briefly. it might be a good idea to do the unassign/reassign in a transaction
        If current Trip is Occupied, this assignment must be rejected (This should NOT happen and might be a bug)
        print(self.trip.as_dict())
        '''

        if self.trip.as_dict()['is_occupied'] == False:
            self.trip.end_trip(sim_clock, current_loc, force_quit=False)

            self.trip.create_new_occupied_trip(sim_clock, current_loc, self.driver.as_dict(), self.driver.vehicle, requested_trip)
        else:
            logging.warning(f'Ignoring Assignment request: Driver {self.driver.get_id()} is already engaged in an Occupied trip')
            pass


    ################
    # Message Callbacks and other methods

    def on_receive_message(self, client, userdata, message):
        ''' Push message to a personal RabbitMQ Queue
        - At every step (simulation), The agent will pull items from queue and process them in sequence until Queue is empty
        '''
        payload = json.loads(message.payload.decode('utf-8'))
        self.enqueue_message(payload)


    def enqueue_message(self, payload):
        ''' '''
        self.message_queue.append(payload)

    def dequeue_message(self):
        ''' '''
        try:
            return self.message_queue.pop(0)
        except: return None

    def enfront_message(self, payload):
        self.message_queue.insert(0, payload)


if __name__ == '__main__':
    credentials = {
        "email": "valuex@test.org",
        "password": "abcd1234"
    }

    driver_app = DriverApp(datetime.utcnow(), credentials)

    print(driver_app.driver.driver)
