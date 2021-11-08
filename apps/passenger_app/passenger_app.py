import requests, json, polyline
from random import choice
from http import HTTPStatus
import shapely
from shapely.geometry.geo import mapping

import logging
from apps.config import settings
from apps.utils.utils import is_success

from apps.utils.user_registry import UserRegistry
from .passenger_manager import PassengerManager
from .passenger_trip_manager import PassengerTripManager
from apps.loc_service import OSRMClient
import paho.mqtt.client as paho


from apps.messenger_service import Messenger



class PassengerApp:

    exited_market = False

    def __init__(self, run_id, sim_clock, current_loc, credentials, passenger_settings):
        self.run_id = run_id
        self.credentials = credentials

        self.user = UserRegistry(sim_clock, credentials)

        self.passenger = PassengerManager(run_id, sim_clock, self.user, passenger_settings)
        self.messenger = Messenger(credentials, f"{self.run_id}/{self.passenger.get_id()}", self.on_receive_message)
        self.message_queue = []
        self.trip = PassengerTripManager(run_id, sim_clock, self.user, self.messenger)

    def get_passenger(self):
        return self.passenger.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def login(self, sim_clock, current_loc, pickup_loc=None, dropoff_loc=None, trip_value=None):
        ''' '''
        self.passenger.login(sim_clock)

        if (pickup_loc is not None) and (dropoff_loc is not None):
            self.trip.create_new_trip_request(sim_clock, current_loc, self.passenger.as_dict(), pickup_loc, dropoff_loc, trip_value)

    def logout(self, sim_clock, current_loc):
        ''' '''
        logging.info(f'logging out Passenger {self.passenger.get_id()}')
        try:
            self.trip.end_trip(sim_clock, current_loc, force_quit=True, shutdown=True)
            self.passenger.logout(sim_clock)
            self.exited_market = True
        except Exception as e:
            logging.exception(str(e))


    def ping(self, sim_clock, **kwargs):
        ''' '''
        self.trip.ping(sim_clock, **kwargs)

    def refresh(self):
        ''' Sync ALL inMemory State with the db State'''
        # Passenger
        self.passenger.refresh()
        self.trip.refresh()


    ################
    # Message Callbacks and other methods

    def on_receive_message(self, client, userdata, message):
        ''' Push message to a personal RabbitMQ Queue
        - At every step (simulation), pull items from queue and process them in sequence until Queue is empty
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



if __name__ == '__main__':
    passenger_app = PassengerApp()

    print(passenger_app.registry.passenger)
