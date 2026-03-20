import json, logging

from datetime import datetime
from dateutil.relativedelta import relativedelta
from apps.utils import is_success
from apps.loc_service import OSRMClient

from apps.state_machine import RidehailPassengerTripStateMachine
from apps.utils import str_to_time, time_to_str
from apps.common.trip_manager_base import TripManagerBase
from apps.ride_hail import RideHailActions, RideHailEvents

from apps.utils.excepions import WriteFailedException, RefreshException

class PassengerTripManager(TripManagerBase):
    ''' '''
    trip = None

    def __init__(self, run_id, sim_clock, user, messenger, persona):
        super().__init__(run_id, user, messenger, persona=persona)

        self.time_requested = None
        self.time_assigned = None
        self.time_confirmed = None
        self.time_pickedup = None
        self.time_droppedoff = None

    def as_dict(self):
        return self.trip

    def estimate_next_event_time(self, current_time):
        '''
        current_time is datetime
        '''
        try:
            if self.trip['state'] in RidehailPassengerTripStateMachine.passenger_requested_trip.name:
                try:
                    patience = self.trip['meta']['profile']['patience']
                except Exception as e:
                    logging.warning(str(e))
                    patience = 0

                try:
                    # last_waypoint_time = str_to_time(self.trip['last_waypoint']['_updated']) # May use sim_clock for consistency
                    trip_created_time = str_to_time(self.trip['_created']) # May use sim_clock for consistency
                    # NOTE May need to check if elapsed time since state change needs to be computed instead of using last_waypoint_time
                except Exception as e:
                    logging.warning(str(e))
                    trip_created_time = current_time

                next_waypoint_time = max((trip_created_time + relativedelta(seconds=patience)), current_time)

            elif self.trip['state'] in [RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
                                        RidehailPassengerTripStateMachine.passenger_waiting_for_pickup.name]:
                try:
                    estimated_time_to_arrive = self.trip['stats']['estimated_time_to_arrive']
                except Exception as e:
                    logging.warning(str(e))
                    estimated_time_to_arrive = 0

                try:
                    last_waypoint_time = str_to_time(self.trip['last_waypoint']['_updated']) # May use sim_clock for consistency
                    # NOTE May need to check if elapsed time since state change needs to be computed instead of using last_waypoint_time
                except Exception as e:
                    logging.warning(str(e))
                    last_waypoint_time = current_time

                next_waypoint_time = max((last_waypoint_time + relativedelta(seconds=estimated_time_to_arrive)), current_time)

            elif self.trip['state'] == RidehailPassengerTripStateMachine.passenger_moving_for_dropoff.name:
                try:
                    estimated_time_to_dropoff = self.trip['stats']['estimated_time_to_dropoff']
                except Exception as e:
                    logging.warning(str(e))
                    estimated_time_to_dropoff = 0

                try:
                    last_waypoint_time = str_to_time(self.trip['last_waypoint']['_updated']) # May use sim_clock for consistency
                    # NOTE May need to check if elapsed time since state change needs to be computed instead of using last_waypoint_time
                except Exception as e:
                    logging.warning(str(e))
                    last_waypoint_time = current_time

                next_waypoint_time = max((last_waypoint_time + relativedelta(seconds=estimated_time_to_dropoff)), current_time)

            else:
                next_waypoint_time = current_time
        except:
            next_waypoint_time = current_time

        return next_waypoint_time


    def create_new_trip_request(self, sim_clock, current_loc, passenger, pickup_loc, dropoff_loc, trip_price=None):
        self.time_requested = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT")
        print(f"Creating new trip request at {sim_clock} for passenger {passenger['_id']} from {pickup_loc} to {dropoff_loc} with price {trip_price}")

        data = {
            "passenger": passenger['_id'],
            "persona": self.persona,
            "meta": {
                'profile': passenger['profile'],
            },
            "current_loc": current_loc,
            "pickup_loc": pickup_loc,
            "dropoff_loc": dropoff_loc,
            "sim_clock": sim_clock,
            "statemachine": {
                "name": "RidehailPassengerTripStateMachine",
                "domain": "ride_hail",
            },
            "state": RidehailPassengerTripStateMachine.initial_state.name,
            "trip_price": self.compute_trip_price(pickup_loc, dropoff_loc) if trip_price is None else trip_price,
        }

        response = self._post_trip(data)
        print(f"Received response for new trip request: {response.status_code = } - {response.text = }")

        if is_success(response.status_code):
            # passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{response.json()['_id']}"
            # response = requests.get(passenger_trip_item_url, headers=self.user.get_headers())
            # self.trip = response.json()
            self.trip = {'_id': response.json()['_id']}
            print(f"New trip created with ID {self.trip['_id']}")
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def compute_trip_price(self, start_loc, end_loc):
        route = OSRMClient.get_route(start_loc, end_loc)

        return route.get('duration', 0)

    def ping(self, sim_clock, current_loc, **kwargs):
        ''' '''
        if self.trip is None:
            raise Exception('trip is not set')

        data = kwargs
        data['sim_clock'] = sim_clock
        data['current_loc'] = current_loc

        response = self._patch_trip(data)
        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def assign(self, sim_clock, current_loc, driver):
        self.time_assigned = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT")
        wait_time_assignment = (self.time_assigned - self.time_requested).total_seconds()

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'driver': driver,
            'stats.wait_time_assignment': wait_time_assignment
        }

        response = self._patch_trip_transition('assign', data)

        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def driver_confirmed_trip(self, sim_clock, current_loc, estimated_time_to_arrive):
        self.time_confirmed = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT")
        wait_time_driver_confirm = (self.time_confirmed - self.time_requested).total_seconds()

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'stats.wait_time_driver_confirm': wait_time_driver_confirm,
            'stats.estimated_time_to_arrive': estimated_time_to_arrive,
        }

        response = self._patch_trip_transition('driver_confirmed_trip', data)

        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def accept(self, sim_clock, current_loc):
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('accept', data)

        if is_success(response.status_code):
            self.refresh()

            self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                    json.dumps({
                                        'action': RideHailActions.PASSENGER_WORKFLOW_EVENT,
                                        'passenger_id': self.trip['passenger'],
                                        'data': {
                                            'event': RideHailEvents.PASSENGER_CONFIRMED_TRIP
                                        }

                                    })
                                )
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def reject(self, sim_clock, current_loc):
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('reject', data)

        if is_success(response.status_code):
            self.refresh()

            self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                    json.dumps({
                                        'action': RideHailActions.PASSENGER_WORKFLOW_EVENT,
                                        'passenger_id': self.trip['passenger'],
                                        'data': {
                                            'event': RideHailEvents.PASSENGER_REJECTED_TRIP
                                        }

                                    })
                                )
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def cancel(self, sim_clock, current_loc):
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('cancel', data)

        if is_success(response.status_code):
            self.refresh()

            if self.trip.get('driver') is not None:
                self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                    json.dumps({
                                        'action': RideHailActions.PASSENGER_WORKFLOW_EVENT,
                                        'passenger_id': self.trip['passenger'],
                                        'data': {
                                            'event': RideHailEvents.PASSENGER_CANCEL_TRIP
                                        }

                                    })
                                )
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def wait_for_pickup(self, sim_clock, current_loc):

        # try:
        #     passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/wait_for_pickup"
        # except Exception as e:
        #     raise e
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('wait_for_pickup', data)

        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def driver_cancelled_trip(self, sim_clock, current_loc):

        # try:
        #     passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_cancelled_trip"
        # except Exception as e:
        #     raise e
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('driver_cancelled_trip', data)

        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def driver_arrived_for_pickup(self, sim_clock, current_loc, driver_ride_hail_trip):

        # try:
        #     passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_arrived_for_pickup"
        # except Exception as e:
        #     raise e
        self.time_pickedup = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT")
        wait_time_pickup = (self.time_pickedup - self.time_assigned).total_seconds()
        wait_time_total = (self.time_pickedup - self.time_requested).total_seconds()

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'driver_ride_hail_trip': driver_ride_hail_trip,
            'stats.wait_time_pickup': wait_time_pickup,
            'stats.wait_time_total': wait_time_total
        }

        response = self._patch_trip_transition('driver_arrived_for_pickup', data)

        if is_success(response.status_code):
            self.refresh()

            # Message driver
            self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                json.dumps({
                                    'action': RideHailActions.PASSENGER_WORKFLOW_EVENT,
                                    'passenger_id': self.trip['passenger'],
                                    'data': {
                                        'event': RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP
                                    }

                                })
                            )
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def driver_move_for_dropoff(self, sim_clock, current_loc, route):

        # try:
        #     passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_move_for_dropoff"
        # except Exception as e:
        #     raise e
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'routes.planned.moving_for_dropoff': route,
            'stats.estimated_time_to_dropoff': route['duration']
        }

        response = self._patch_trip_transition('driver_move_for_dropoff', data)

        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def driver_arrived_for_dropoff(self, sim_clock, current_loc):

        # try:
        #     passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_arrived_for_dropoff"
        # except Exception as e:
        #     raise e
        self.time_droppedoff = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT")
        travel_time_total = (self.time_droppedoff - self.time_pickedup).total_seconds()

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
            'stats.travel_time_total': travel_time_total
        }

        response = self._patch_trip_transition('driver_arrived_for_dropoff', data)

        if is_success(response.status_code):
            self.refresh()
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def driver_waiting_for_dropoff(self, sim_clock, current_loc):

        # try:
        #     passenger_trip_item_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip/{self.trip['_id']}/driver_waiting_for_dropoff"
        # except Exception as e:
        #     raise e
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('driver_waiting_for_dropoff', data)

        if is_success(response.status_code):
            self.refresh()

            # Message driver
            self.messenger.client.publish(f'{self.run_id}/{self.trip["driver"]}',
                                json.dumps({
                                    'action': RideHailActions.PASSENGER_WORKFLOW_EVENT,
                                    'passenger_id': self.trip['passenger'],
                                    'data': {
                                        'event': RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF
                                    }

                                })
                            )
        else:
            raise WriteFailedException(f"{response.url}, {response.text}")

    def end_trip(self, sim_clock, current_loc):
        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('end_trip', data)

        # # # WATCH THIS
        # if is_success(response.status_code):
        #     logging.info('ending_trip')
        #     self.trip = None
        # else:
        #     raise Exception(f"{response.url}, {response.text}")


    def force_quit(self, sim_clock, current_loc):

        if (self.trip is None) or  (self.trip['state'] in [RidehailPassengerTripStateMachine.passenger_completed_trip.name,
                                                            RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,]):
            return

        data = {
            'sim_clock': sim_clock,
            'current_loc': current_loc,
        }

        response = self._patch_trip_transition('force_quit', data)

        # if is_success(response.status_code):
        #     logging.info('force quit trip')
        #     self.trip = None
        # else:
        #     raise Exception(f"{response.url}, {response.text}")

    def refresh(self):
        if self.trip is not None:
            response = self._get_trip()

            if is_success(response.status_code):
                self.trip = response.json()
            else:
                raise RefreshException(f'PassengerTripManager.refresh: Failed getting response for {self.trip["_id"]} Got {response.text}')

