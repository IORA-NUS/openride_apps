import requests, json

from config import settings
from utils import id_generator, is_success

from lib import RidehailDriverTripStateMachine


class DriverTripManager:
    ''' '''
    trip = None

    def __init__(self, run_id, sim_clock, user, messenger):
        self.run_id = run_id
        self.user = user
        self.messenger = messenger

    def as_dict(self):
        return self.trip

    def create_new_unoccupied_trip(self, sim_clock, current_loc, driver, vehicle): #, route=None):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip"
        # print(sim_clock)

        data = {
            "driver": f"{driver['_id']}",
            "vehicle": f"{vehicle['_id']}",
            "current_loc": current_loc, #{"type":"Point","coordinates": current_loc},
            "next_dest_loc": current_loc, #{"type":"Point","coordinates":current_loc},
            "is_occupied": False,
            "sim_clock": sim_clock,
            # "routes.planned.looking_for_job": route
        }

        response = requests.post(driver_trip_url, headers=self.user.get_headers(), data=json.dumps(data))

        if is_success(response.status_code):
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{response.json()['_id']}"
            response = requests.get(driver_trip_item_url, headers=self.user.get_headers())
            self.trip = response.json()
            # print(self.trip)
        else:
            raise Exception(response.text)

    def create_new_occupied_trip(self, sim_clock, current_loc, driver, vehicle, passenger_trip):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip"

        data = {
            "driver": f"{driver['_id']}",
            "vehicle": f"{vehicle['_id']}",
            "current_loc": current_loc, # {"type":"Point","coordinates": current_loc},
            "next_dest_loc": passenger_trip['pickup_loc'], # {"type":"Point","coordinates": current_loc},
            "passenger_trip": passenger_trip['_id'],
            "passenger": passenger_trip['passenger'],
            "trip_start_loc": current_loc,
            "pickup_loc": passenger_trip['pickup_loc'], #{"type":"Point","coordinates":passenger_trip['start_loc']},
            "dropoff_loc": passenger_trip['dropoff_loc'], # {"type":"Point","coordinates":passenger_trip['end_loc']},
            "is_occupied": True,
            "sim_clock": sim_clock,
        }


        response = requests.post(driver_trip_url, headers=self.user.get_headers(), data=json.dumps(data))

        if is_success(response.status_code):
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{response.json()['_id']}"
            response = requests.get(driver_trip_item_url, headers=self.user.get_headers())
            self.trip = response.json()

            self.recieve(sim_clock, current_loc)
        else:
            raise Exception(response.text)

    def look_for_job(self, sim_clock, current_loc, route):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/look_for_job"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'routes.planned.looking_for_job': route,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

    def recieve(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/recieve"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

    def confirm(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/confirm"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

        self.messenger.client.publish(f'Agent/{self.trip["passenger"]}',
                                json.dumps({
                                    'action': 'driver_workflow_event',
                                    'driver_id': self.trip['driver'],
                                    'data': {
                                        'event': 'driver_confirmed_trip',
                                        'location': current_loc,
                                        'driver_trip_id': self.trip['_id']
                                    }

                                })
                            )

    def reject(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/reject"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

    def cancel(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/cancel"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

        self.messenger.client.publish(f'Agent/{self.trip["passenger"]}',
                                json.dumps({
                                    'action': 'driver_workflow_event',
                                    'driver_id': self.trip['driver'],
                                    'data': {
                                        'event': 'driver_cancelled_trip',
                                    }

                                })
                            )

    def passenger_confirmed_trip(self, sim_clock, current_loc, route):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/passenger_confirmed_trip"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            "routes.planned.moving_to_pickup": route
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

    def wait_to_pickup(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/wait_to_pickup"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

        self.messenger.client.publish(f'Agent/{self.trip["passenger"]}',
                                json.dumps({
                                    'action': 'driver_workflow_event',
                                    'driver_id': self.trip['driver'],
                                    'data': {
                                        'event': 'driver_arrived_for_pickup',
                                        'location': current_loc,
                                        'driver_trip_id': self.trip['_id']
                                    }

                                })
                            )

    def passenger_acknowledge_pickup(self, sim_clock, current_loc, route):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/passenger_acknowledge_pickup"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            "routes.planned.moving_to_dropoff": route
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

    def move_to_dropoff(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/move_to_dropoff"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

        self.messenger.client.publish(f'Agent/{self.trip["passenger"]}',
                                json.dumps({
                                    'action': 'driver_workflow_event',
                                    'driver_id': self.trip['driver'],
                                    'data': {
                                        'event': 'driver_move_for_dropoff',
                                        'location': current_loc,
                                        'planned_route': self.trip['routes']['planned']['moving_to_dropoff']
                                    }
                                })
                            )

    def wait_to_dropoff(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/wait_to_dropoff"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

        self.messenger.client.publish(f'Agent/{self.trip["passenger"]}',
                                json.dumps({
                                    'action': 'driver_workflow_event',
                                    'driver_id': self.trip['driver'],
                                    'data': {
                                        'event': 'driver_waiting_for_dropoff',
                                        'location': current_loc,
                                    }
                                })
                            )

    def passenger_acknowledge_dropoff(self, sim_clock, current_loc):

        try:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/passenger_acknowledge_dropoff"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh() # refresh self.trip

    def end_trip(self, sim_clock, current_loc, force_quit=False):
        '''
        Send an end_trip signal to the current trip.
        - Force Quit implies that the trip shall be terminated regardless of the current state (i.e. set is_active=False directly)
            -- This will result in inconsistent states and should be used carefully
        - Shutdown signal indicates if a new empty trip should be initiated
            -- Driver should always have an active trip while logged on.
            -- When Driver Logs off, then no new trip should be created. Shutdown signal handles this case.
            -- Hence use with Care.
        '''
        try:
            if force_quit == True:
                driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/force_quit"
            else:
                driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}/end_trip"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))
        self.trip = None

    def ping(self, sim_clock, current_loc, **kwargs):
        ''' '''
        if self.trip is None:
            raise Exception('trip is not set')

        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip/{self.trip['_id']}"

        data = kwargs
        data['sim_clock'] = sim_clock
        data['current_loc'] = current_loc

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))
        if is_success(response.status_code):
            self.refresh()
        else:
            # print (response.text)
            raise Exception(response.text)

    def refresh(self):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip"
        driver_trip_item_url = driver_trip_url + f"/{self.trip['_id']}"

        response = requests.get(driver_trip_item_url, headers=self.user.get_headers())

        self.trip = response.json()
