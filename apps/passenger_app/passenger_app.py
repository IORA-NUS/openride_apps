import requests, json, polyline, traceback
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

from apps.state_machine import RidehailPassengerTripStateMachine


# from apps.messenger_service import Messenger



class PassengerApp:

    exited_market = False

    def __init__(self, run_id, sim_clock, current_loc, credentials, passenger_profile, messenger):
        self.run_id = run_id
        self.credentials = credentials

        self.user = UserRegistry(sim_clock, credentials)

        self.passenger = PassengerManager(run_id, sim_clock, self.user, passenger_profile)
        # self.messenger = Messenger(credentials, f"{self.run_id}/{self.passenger.get_id()}", self.on_receive_message)
        self.messenger = messenger
        self.topic_params = {
            f"{self.run_id}/{self.passenger.get_id()}": self.message_handler
        }

        self.message_queue = []
        self.trip = PassengerTripManager(run_id, sim_clock, self.user, self.messenger)

        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

    def get_passenger(self):
        return self.passenger.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def login(self, sim_clock, current_loc, pickup_loc=None, dropoff_loc=None, trip_price=None):
        ''' '''
        self.passenger.login(sim_clock)

        if (pickup_loc is not None) and (dropoff_loc is not None):
            self.trip.create_new_trip_request(sim_clock, current_loc, self.passenger.as_dict(), pickup_loc, dropoff_loc, trip_price)

    def logout(self, sim_clock, current_loc):
        ''' '''
        logging.debug(f'logging out Passenger {self.passenger.get_id()}')
        try:
            # self.trip.end_trip(sim_clock, current_loc, force_quit=True, shutdown=True)
            self.trip.force_quit(sim_clock, current_loc)
        except Exception as e:
            logging.exception(str(e))

        try:
            self.passenger.logout(sim_clock)
        except Exception as e:
            logging.warning(str(e))

        # self.messenger.disconnect()

        self.exited_market = True


    def ping(self, sim_clock, current_loc, **kwargs):
        ''' '''
        # self.latest_sim_clock = sim_clock
        # self.latest_loc = current_loc

        self.trip.ping(sim_clock, current_loc, **kwargs)

    def refresh(self):
        ''' Sync ALL inMemory State with the db State'''
        # Passenger
        # No need to refresh passenger at every step
        # self.passenger.refresh()
        self.trip.refresh()
        # raise exception if unable to refresh

    def handle_overbooking(self, sim_clock, driver):

        self.messenger.client.publish(f'{self.run_id}/{driver}',
                            json.dumps({
                                'action': 'passenger_workflow_event',
                                'passenger_id': self.passenger.get_id(),
                                'data': {
                                    'event': 'passenger_rejected_trip'
                                }
                            })
                        )
    ################
    # Message Callbacks and other methods

    def update_current(self, sim_clock, current_loc):
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

    # def on_receive_message(self, client, userdata, message):
    #     ''' Push message to a personal RabbitMQ Queue
    #     - At every step (simulation), pull items from queue and process them in sequence until Queue is empty
    #     '''
    #     payload = json.loads(message.payload.decode('utf-8'))

    #     if payload['action'] == 'assigned':
    #         if self.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.name:
    #             try:
    #                 self.trip.assign(self.latest_sim_clock,
    #                                 current_loc=self.latest_loc,
    #                                 driver=payload['driver_id'])
    #             except Exception as e:
    #                 logging.exception(str(e))
    #                 raise e
    #         else:
    #             self.handle_overbooking(self.latest_sim_clock, driver=payload['driver_id'])
    #             # logging.warning(f"WARNING: Cannot assign Driver {payload['driver_id']} to passenger_trip {self.app.get_trip()['_id']} with state: {self.app.get_trip()['state']} ")
    #     else:
    #         self.enqueue_message(payload)

    def message_handler(self, payload):
        ''' Push message to a personal RabbitMQ Queue
        - At every step (simulation), pull items from queue and process them in sequence until Queue is empty
        '''
        # payload = json.loads(message.payload.decode('utf-8'))
        # print('passenger_app received_message', payload)

        if payload['action'] == 'assigned':
            if self.get_trip()['state'] == RidehailPassengerTripStateMachine.passenger_requested_trip.name:
                try:
                    self.trip.assign(self.latest_sim_clock,
                                    current_loc=self.latest_loc,
                                    driver=payload['driver_id'])
                except Exception as e:
                    # logging.exception(traceback.format_exc())
                    logging.warning(f"Assignment failed for {payload=}: {str(e)}")
                    self.handle_overbooking(self.latest_sim_clock, driver=payload['driver_id'])
                    # Mesage driver of failure
                    # raise e
            else:
                self.handle_overbooking(self.latest_sim_clock, driver=payload['driver_id'])
                # logging.warning(f"WARNING: Cannot assign Driver {payload['driver_id']} to passenger_trip {self.app.get_trip()['_id']} with state: {self.app.get_trip()['state']} ")
        else:
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
    passenger_app = PassengerApp()

    print(passenger_app.registry.passenger)
