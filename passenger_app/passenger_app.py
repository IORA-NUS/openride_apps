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

    # current_trip = None
    exited_market = False

    def __init__(self, run_id, sim_clock, current_loc, credentials, passenger_settings):
        self.run_id = run_id
        self.credentials = credentials
        # self.credentials = credentials if credentials is not None else {
        #                     "email": "valuex@test.org",
        #                     "password": "abcd1234"
        #                 }

        self.user = UserRegistry(sim_clock, credentials)

        self.passenger = PassengerManager(run_id, sim_clock, self.user, passenger_settings)
        self.messenger = Messenger(run_id, credentials, self.passenger.get_id(), self.on_receive_message)
        self.message_queue = []
        self.trip = PassengerTripManager(run_id, sim_clock, self.user, self.messenger)

    def get_passenger(self):
        return self.passenger.as_dict()

    def get_trip(self):
        # return self.current_trip
        return self.trip.as_dict()

    def login(self, sim_clock, current_loc, pickup_loc=None, dropoff_loc=None, trip_value=None):
        ''' '''
        self.passenger.login(sim_clock)

        if (pickup_loc is not None) and (dropoff_loc is not None):
            # self.request_trip(sim_clock, current_loc, pickup_loc, dropoff_loc)
            self.trip.create_new_trip_request(sim_clock, current_loc, self.passenger.as_dict(), pickup_loc, dropoff_loc, trip_value)

    def logout(self, sim_clock, current_loc):
        ''' '''
        logging.info(f'logging out Passenger {self.passenger.get_id()}')
        try:
            self.trip.end_trip(sim_clock, current_loc, force_quit=True, shutdown=True)
            self.passenger.logout(sim_clock)
            self.exited_market = True
        except Exception as e:
            # print(e)
            logging.exception(str(e))
            # raise e

    # def end_trip(self, sim_clock, current_loc, force_quit=False, shutdown=False):
    #     ''' '''
    #     # self.trip.end_trip(sim_clock, current_loc)
    #     self.trip.end_trip(sim_clock, force_quit)

    #     # if shutdown == False:
    #     # #     self.start_new_unoccupied_trip(sim_clock)
    #     #     self.trip.create_new_trip_request(sim_clock, current_loc, self.passenger.as_dict(), pickup_loc, dropoff_loc)


    # # Passenger app must be a registered listener to Message events
    # def on_subscribe(self, client, userdata, mid, granted_qos):
    #     ''''''
    #     # print("passenger.on_subsctibe",  client,  userdata, mid, granted_qos)


    def ping(self, sim_clock, **kwargs):
        ''' '''
        self.trip.ping(sim_clock, **kwargs)


    # def request_trip(self, sim_clock, current_loc, pickup_loc, dropoff_loc):
    #     # print('inside request_trip')
    #     self.trip.create_new_trip_request(sim_clock, current_loc, self.passenger.as_dict(), pickup_loc, dropoff_loc)


    # def handle_assigned_trip(self, sim_clock, current_loc, driver_id):
    #     ''' '''
    #     transition = 'assign'
    #     # print(f"{transition = }")
    #     # print(driver_id)

    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition,
    #                         driver=driver_id,
    #                         # driver_trip=driver_trip['_id'],
    #                         # driver=driver_trip['driver'],
    #                     )

    # def driver_confirmed_trip(self, sim_clock, current_loc):
    #     transition = 'driver_confirmed_trip'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition
    #                     )

    # def accept(self, sim_clock, current_loc):
    #     transition = 'accept'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition
    #                     )
    #     # if self.get_trip().get('driver') is not None:
    #     self.messenger.client.publish(f'Agent/{self.get_trip()["driver"]}',
    #                                 json.dumps({
    #                                     'action': 'passenger_workflow_event',
    #                                     'passenger_id': self.passenger.get_id(),
    #                                     'data': {
    #                                         'event': 'passenger_confirmed_trip'
    #                                     }

    #                                 })
    #                             )

    # def cancel(self, sim_clock, current_loc):
    #     transition = 'cancel'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition
    #                     )

    #     # Cancel Trip sould send a message to driver so that driver can be freed for a different job
    #     if self.get_trip().get('driver') is not None:
    #         self.messenger.client.publish(f'Agent/{self.get_trip()["driver"]}',
    #                                 json.dumps({
    #                                     'action': 'passenger_workflow_event',
    #                                     'passenger_id': self.passenger.get_id(),
    #                                     'data': {
    #                                         'event': 'passenger_cancel_trip'
    #                                     }

    #                                 })
    #                             )

    # def move_for_pickup(self, sim_clock, current_loc):
    #     transition = 'move_for_pickup'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition
    #                     )

    # def wait_for_pickup(self, sim_clock, current_loc):
    #     transition = 'wait_for_pickup'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition
    #                     )

    # def driver_cancelled_trip(self, sim_clock, current_loc):
    #     # transition = 'pickedup'
    #     transition = 'driver_cancelled_trip'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition,
    #                     )


    # def driver_arrived_for_pickup(self, sim_clock, current_loc, driver_trip_id):
    #     # transition = 'pickedup'
    #     transition = 'driver_arrived_for_pickup'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition,
    #                         driver_trip=driver_trip_id,
    #                     )
    #     # Message driver
    #     self.messenger.client.publish(f'Agent/{self.get_trip()["driver"]}',
    #                             json.dumps({
    #                                 'action': 'passenger_workflow_event',
    #                                 'passenger_id': self.passenger.get_id(),
    #                                 'data': {
    #                                     'event': 'passenger_acknowledge_pickup'
    #                                 }

    #                             })
    #                         )

    # def driver_move_for_dropoff(self, sim_clock, current_loc, route):
    #     # transition = 'move_for_dropoff'
    #     transition = 'driver_move_for_dropoff'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition,
    #                         routes={
    #                             "planned": {
    #                                 "moving_for_dropoff": route
    #                             }
    #                         }
    #                     )

    # def driver_wait_to_dropoff(self, sim_clock, current_loc):
    #     # transition = 'wait_for_dropoff'
    #     transition = 'driver_wait_to_dropoff'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition
    #                     )

    # def driver_arrived_for_dropoff(self, sim_clock, current_loc):
    #     # transition = 'droppedoff'
    #     transition = 'driver_arrived_for_dropoff'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                         transition=transition
    #                     )
    #     # Message driver
    #     self.messenger.client.publish(f'Agent/{self.get_trip()["driver"]}',
    #                             json.dumps({
    #                                 'action': 'passenger_workflow_event',
    #                                 'passenger_id': self.passenger.get_id(),
    #                                 'data': {
    #                                     'event': 'passenger_acknowledge_dropoff'
    #                                 }

    #                             })
    #                         )

    # def end_trip(self, sim_clock, current_loc):
    #     ''' '''
    #     transition = 'end_trip'
    #     # print(f"{transition = }")
    #     self.ping(sim_clock, current_loc=current_loc,
    #                             transition=transition
    #                         )

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
        logging.info(f"{message.topic=}, {message.payload=}")
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
