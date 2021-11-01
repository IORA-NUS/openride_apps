import requests, json, logging

from apps.config import settings
from apps.utils import id_generator, is_success
from apps.loc_service import OSRMClient

from apps.lib import RidehailPassengerTripStateMachine

class PassengerTripManager:
    ''' '''
    trip = None

    def __init__(self, run_id, sim_clock, user, messenger):
        self.run_id = run_id
        self.user = user
        self.messenger = messenger

    def as_dict(self):
        return self.trip

    def create_new_trip_request(self, sim_clock, current_loc, passenger, pickup_loc, dropoff_loc, trip_value=None):
        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip"

        data = {
            "passenger": passenger['_id'],
            "current_loc": current_loc, #{"type":"Point","coordinates": current_loc},
            "pickup_loc": pickup_loc, #{"type":"Point","coordinates":current_loc},
            "dropoff_loc": dropoff_loc, #{"type":"Point","coordinates":current_loc},
            "sim_clock": sim_clock,
            "trip_value": self.compute_trip_value(pickup_loc, dropoff_loc) if trip_value is None else trip_value
        }

        response = requests.post(passenger_trip_url, headers=self.user.get_headers(), data=json.dumps(data))
        # print(response.text)

        if is_success(response.status_code):
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{response.json()['_id']}"
            response = requests.get(passenger_trip_item_url, headers=self.user.get_headers())
            self.trip = response.json()
            # print(self.trip)
        else:
            raise Exception(response.text)


    # def create_new_assigned_trip(self, sim_clock, passenger, vehicle, current_loc, passenger_trip):
    #     passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/passenger/ride_hail/trip"

    #     data = {
    #         "passenger": f"{passenger['_id']}",
    #         "vehicle": f"{vehicle['_id']}",
    #         "current_loc": {"type":"Point","coordinates": current_loc},
    #         "passenger_trip": passenger_trip['_id'],
    #         "passenger": passenger_trip['passenger'],
    #         "start_loc": {"type":"Point","coordinates":passenger_trip['start_loc']},
    #         "end_loc": {"type":"Point","coordinates":passenger_trip['end_loc']},
    #         "is_occupied": True,
    #         "sim_clock": sim_clock,
    #     }

    #     response = requests.post(passenger_trip_url, headers=self.headers, data=json.dumps(data))

    #     if is_success(response.status_code):
    #         passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/passenger/ride_hail/trip/{response.json()['_id']}"
    #         response = requests.get(passenger_trip_item_url, headers=self.headers)
    #         self.trip = response.json()
    #     else:
    #         raise Exception(response.text)
    def compute_trip_value(self, start_loc, end_loc):
        route = OSRMClient.get_route(start_loc, end_loc)

        return route.get('duration', 0)

    def ping(self, sim_clock, **kwargs):
        ''' '''
        if self.trip is None:
            raise Exception('trip is not set')

        passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}"

        # data = {
        #     "current_loc": location,
        #     # "current_loc": {
        #     #     "type":"Point",
        #     #     "coordinates":location
        #     # }
        # }

        # if transition is not None:
        #     data['transition'] = transition

        data = kwargs
        data['sim_clock'] = sim_clock

        # print(data)

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))
        if is_success(response.status_code):
            # self.trip = response.json()
            self.refresh()
        else:
            # print (response.text)
            logging.error(f"{self.trip =}")
            logging.error(f"{data =}")
            raise Exception(response.text)

    def assign(self, sim_clock, current_loc, driver):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/assign"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'driver': driver,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

    def driver_confirmed_trip(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_confirmed_trip"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

    def accept(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/accept"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

        # self.messenger.client.publish(f'Agent/{self.trip["driver"]}',
        self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                    json.dumps({
                                        'action': 'passenger_workflow_event',
                                        'passenger_id': self.trip['passenger'],
                                        'data': {
                                            'event': 'passenger_confirmed_trip'
                                        }

                                    })
                                )

    def reject(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/reject"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

        # self.messenger.client.publish(f'Agent/{self.trip["driver"]}',
        self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                    json.dumps({
                                        'action': 'passenger_workflow_event',
                                        'passenger_id': self.trip['passenger'],
                                        'data': {
                                            'event': 'passenger_rejected_trip'
                                        }

                                    })
                                )

    def cancel(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/cancel"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()
        # print(self.trip)

        if self.trip.get('driver') is not None:
            # self.messenger.client.publish(f'Agent/{self.trip["driver"]}',
            self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                    json.dumps({
                                        'action': 'passenger_workflow_event',
                                        'passenger_id': self.trip['passenger'],
                                        'data': {
                                            'event': 'passenger_cancel_trip'
                                        }

                                    })
                                )

    def wait_for_pickup(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/wait_for_pickup"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

    def driver_cancelled_trip(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_cancelled_trip"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

    def driver_arrived_for_pickup(self, sim_clock, current_loc, driver_ride_hail_trip):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_arrived_for_pickup"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'driver_ride_hail_trip': driver_ride_hail_trip,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

        # Message driver
        # self.messenger.client.publish(f'Agent/{self.trip["driver"]}',
        self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                json.dumps({
                                    'action': 'passenger_workflow_event',
                                    'passenger_id': self.trip['passenger'],
                                    'data': {
                                        'event': 'passenger_acknowledge_pickup'
                                    }

                                })
                            )

    def driver_move_for_dropoff(self, sim_clock, current_loc, route):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_move_for_dropoff"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'routes.planned.moving_for_dropoff': route,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

    def driver_arrived_for_dropoff(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_arrived_for_dropoff"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

    def driver_waiting_for_dropoff(self, sim_clock, current_loc):

        try:
            passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_waiting_for_dropoff"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))

        self.refresh()

        # Message driver
        # self.messenger.client.publish(f'Agent/{self.trip["driver"]}',
        self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                json.dumps({
                                    'action': 'passenger_workflow_event',
                                    'passenger_id': self.trip['passenger'],
                                    'data': {
                                        'event': 'passenger_acknowledge_dropoff'
                                    }

                                })
                            )

    def end_trip(self, sim_clock, current_loc, force_quit=False, shutdown=False):

        try:
            if force_quit == True:
                passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/force_quit"
            else:
                passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/end_trip"
        except Exception as e:
            raise e

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = requests.patch(passenger_trip_item_url,
                                headers=self.user.get_headers(etag=self.trip['_etag']),
                                data=json.dumps(data))
        # if shutdown:
        # self.trip = None


    def refresh(self):
        # if self.trip is not None:
        passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}"
        try:
            response = requests.get(passenger_trip_item_url, headers=self.user.get_headers())
            self.trip = response.json()
        except Exception as e:
            logging.exception(str(e))


