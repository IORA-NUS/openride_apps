import requests, json, logging, traceback

from apps.config import settings, driver_settings
from apps.utils import id_generator, is_success, deep_update

from apps.state_machine import RidehailDriverTripStateMachine


class DriverTripManager:
    ''' '''
    trip = None

    def __init__(self, run_id, sim_clock, user, messenger):
        self.run_id = run_id
        self.user = user
        self.messenger = messenger

    def as_dict(self):
        return self.trip

    # def create_new_unoccupied_trip(self, sim_clock, current_loc, driver, vehicle):
    def create_new_unoccupied_trip(self, sim_clock, current_loc, driver, vehicle, route):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip"
        # print(sim_clock)

        data = {
            "driver": f"{driver['_id']}",
            "meta": {
                'profile': driver['profile']
            },
            "vehicle": f"{vehicle['_id']}",
            "current_loc": current_loc,
            "next_dest_loc": current_loc,
            "is_occupied": False,
            "sim_clock": sim_clock,
        }

        response = requests.post(driver_trip_url, headers=self.user.get_headers(), data=json.dumps(data))

        if is_success(response.status_code):
            # driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{response.json()['_id']}"
            # response = requests.get(driver_trip_item_url, headers=self.user.get_headers())
            # self.trip = response.json()
            self.trip = {'_id': response.json()['_id']}
            self.refresh()
            self.look_for_job(sim_clock, current_loc, route)
        else:
            raise Exception(response.text)

    def create_new_occupied_trip(self, sim_clock, current_loc, driver, vehicle, passenger_ride_hail_trip):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip"

        data = {
            "driver": f"{driver['_id']}",
            "meta": {
                'profile': driver['profile']
            },
            "vehicle": f"{vehicle['_id']}",
            "current_loc": current_loc,
            "next_dest_loc": passenger_ride_hail_trip['pickup_loc'],
            "passenger_ride_hail_trip": passenger_ride_hail_trip['_id'],
            "passenger": passenger_ride_hail_trip['passenger'],
            "trip_start_loc": current_loc,
            "pickup_loc": passenger_ride_hail_trip['pickup_loc'],
            "dropoff_loc": passenger_ride_hail_trip['dropoff_loc'],
            "is_occupied": True,
            "sim_clock": sim_clock,
        }


        response = requests.post(driver_trip_url, headers=self.user.get_headers(), data=json.dumps(data))

        if is_success(response.status_code):
            # driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{response.json()['_id']}"
            # response = requests.get(driver_trip_item_url, headers=self.user.get_headers())
            # self.trip = response.json()
            self.trip = {'_id': response.json()['_id']}
            self.refresh()
            self.recieve(sim_clock, current_loc)
        else:
            raise Exception(response.text)

    def look_for_job(self, sim_clock, current_loc, route):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/look_for_job"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/look_for_job"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'routes.planned.looking_for_job': route,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()
        else:
            raise Exception(response.text)


    def recieve(self, sim_clock, current_loc):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/recieve"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/recieve"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()

            self.messenger.client.publish(f'{self.run_id}/{self.trip["passenger"]}',
                                            json.dumps({
                                                "action": "assigned", # NOTE This is not a 'driver_workflow_event but an 'assigned' event
                                                "driver_id": self.trip['driver'],
                                            }))

        else:
            raise Exception(response.text)

    def confirm(self, sim_clock, current_loc):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/confirm"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/confirm"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()

            self.messenger.client.publish(f'{self.run_id}/{self.trip["passenger"]}',
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
        else:
            raise Exception(response.text)

    def reject(self, sim_clock, current_loc):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/reject"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/reject"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()
        else:
            raise Exception(response.text)

    def cancel(self, sim_clock, current_loc):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/cancel"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/cancel"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()

            self.messenger.client.publish(f'{self.run_id}/{self.trip["passenger"]}',
                                json.dumps({
                                    'action': 'driver_workflow_event',
                                    'driver_id': self.trip['driver'],
                                    'data': {
                                        'event': 'driver_cancelled_trip',
                                    }

                                })
                            )
        else:
            raise Exception(response.text)

    def passenger_confirmed_trip(self, sim_clock, current_loc, route):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/passenger_confirmed_trip"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/passenger_confirmed_trip"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            "routes.planned.moving_to_pickup": route
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()
        else:
            raise Exception(response.text)

    def wait_to_pickup(self, sim_clock, current_loc):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/wait_to_pickup"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/wait_to_pickup"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()

            self.messenger.client.publish(f'{self.run_id}/{self.trip["passenger"]}',
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
        else:
            raise Exception(response.text)

    def passenger_acknowledge_pickup(self, sim_clock, current_loc, route):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/passenger_acknowledge_pickup"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/passenger_acknowledge_pickup"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            "routes.planned.moving_to_dropoff": route
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()
        else:
            raise Exception(response.text)

    def move_to_dropoff(self, sim_clock, current_loc):

        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/move_to_dropoff"

        if self.trip['state'] != 'driver_moving_to_dropoff':
            first_ping = True

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()

            if first_ping or driver_settings['UPDATE_PASSENGER_LOCATION']:
                self.messenger.client.publish(f'{self.run_id}/{self.trip["passenger"]}',
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
        else:
            raise Exception(response.text)

    def wait_to_dropoff(self, sim_clock, current_loc):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/wait_to_dropoff"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/wait_to_dropoff"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()

            self.messenger.client.publish(f'{self.run_id}/{self.trip["passenger"]}',
                                json.dumps({
                                    'action': 'driver_workflow_event',
                                    'driver_id': self.trip['driver'],
                                    'data': {
                                        'event': 'driver_waiting_for_dropoff',
                                        'location': current_loc,
                                    }
                                })
                            )
        else:
            raise Exception(response.text)

    def passenger_acknowledge_dropoff(self, sim_clock, current_loc):

        # try:
        #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/passenger_acknowledge_dropoff"
        # except Exception as e:
        #     raise e
        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/passenger_acknowledge_dropoff"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()
        else:
            raise Exception(response.text)

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
        # try:
        #     if force_quit == True:
        #         driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/force_quit"
        #     else:
        #         driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/end_trip"
        # except Exception as e:
        #     raise e
        if force_quit == True:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/force_quit"
        else:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/end_trip"

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.trip = None
        else:
            raise Exception(response.text)


    # def end_trip(self, sim_clock, current_loc):
    #     '''
    #     Send an end_trip signal to the current trip.
    #     - Force Quit implies that the trip shall be terminated regardless of the current state (i.e. set is_active=False directly)
    #         -- This will result in inconsistent states and should be used carefully
    #     - Shutdown signal indicates if a new empty trip should be initiated
    #         -- Driver should always have an active trip while logged on.
    #         -- When Driver Logs off, then no new trip should be created. Shutdown signal handles this case.
    #         -- Hence use with Care.
    #     '''
    #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/end_trip"

    #     data = {
    #         'sim_clock': sim_clock,
    #         'current_loc': current_loc,
    #     }

    #     response = requests.patch(driver_trip_item_url,
    #                             headers=self.user.get_headers(etag=self.trip['_etag']),
    #                             data=json.dumps(data))

    #     if is_success(response.status_code):
    #         self.trip = None
    #     else:
    #         raise Exception(response.text)

    # def force_quit(self, sim_clock, current_loc):
    #     '''
    #     Send an end_trip signal to the current trip.
    #     - Force Quit implies that the trip shall be terminated regardless of the current state (i.e. set is_active=False directly)
    #         -- This will result in inconsistent states and should be used carefully
    #     - Shutdown signal indicates if a new empty trip should be initiated
    #         -- Driver should always have an active trip while logged on.
    #         -- When Driver Logs off, then no new trip should be created. Shutdown signal handles this case.
    #         -- Hence use with Care.
    #     '''
    #     # try:
    #     #     if force_quit == True:
    #     #         driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/force_quit"
    #     #     else:
    #     #         driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/end_trip"
    #     # except Exception as e:
    #     #     raise e
    #     driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}/force_quit"

    #     data = {
    #         'sim_clock': sim_clock,
    #         'current_loc': current_loc,
    #     }

    #     response = requests.patch(driver_trip_item_url,
    #                             headers=self.user.get_headers(etag=self.trip['_etag']),
    #                             data=json.dumps(data))

    #     if is_success(response.status_code):
    #         self.trip = None
    #     else:
    #         raise Exception(response.text)


    def ping(self, sim_clock, current_loc, **kwargs):
        ''' '''
        if self.trip is None:
            raise Exception('trip is not set')

        driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}"

        data = kwargs
        data['sim_clock'] = sim_clock
        data['current_loc'] = current_loc

        response = requests.patch(driver_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        if is_success(response.status_code):
            self.refresh()
        else:
            # raise Exception(response.text)
            logging.exception(f"Unable to Ping: {response.text}")

    def refresh(self):
        if self.trip is not None:
            driver_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip/{self.trip['_id']}"

            response = requests.get(driver_trip_item_url, headers=self.user.get_headers())

            if is_success(response.status_code):
                self.trip = response.json()
            else:
                raise Exception(f'DriverTripManager.refresh: Failed getting response for {self.trip["_id"]} Got {response.text}')

