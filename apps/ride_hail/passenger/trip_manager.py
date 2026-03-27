import json, logging

from datetime import datetime
from dateutil.relativedelta import relativedelta
from apps.utils import is_success
from apps.loc_service import OSRMClient

from apps.ride_hail.statemachine import RidehailPassengerTripStateMachine
from apps.utils import str_to_time, time_to_str
from apps.common.trip_manager_base import TripManagerBase
from apps.ride_hail.statemachine import RideHailActions, RideHailEvents

from apps.utils.excepions import WriteFailedException, RefreshException
from apps.config import settings, simulation_domains

from apps.ride_hail.statemachine.driver_passenger_interactions import driver_passenger_interactions

class PassengerTripManager(TripManagerBase):
    ''' '''
    # trip = None
    # StateMachineCls = RidehailPassengerTripStateMachine
    # action_header = RideHailActions.PASSENGER_WORKFLOW_EVENT

    def __init__(self, run_id, sim_clock, user, messenger, persona):
        super().__init__(run_id, user, messenger, persona=persona)

        self.time_requested = None
        self.time_assigned = None
        self.time_confirmed = None
        self.time_pickedup = None
        self.time_droppedoff = None
        self.simulation_domain = simulation_domains['ridehail']

    @property
    def StateMachineCls(self):
        return RidehailPassengerTripStateMachine

    @property
    def message_channel(self):
        return f'{self.run_id}/{self.trip["driver"]}'

    @property
    def statemachine_interaction_mapping(self):
        return driver_passenger_interactions

    def message_template(self, event):
        # NOTE This message template is critical. Ensure the action, self recognition and data with event is included
        return {
            'action': RideHailActions.PASSENGER_WORKFLOW_EVENT,
            'passenger_id': self.trip.get('passenger'),
            'data': {
                'event': event
            }
        }


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
                "domain": self.simulation_domain,
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


    def end_active_trip(self, sim_clock, current_loc, force=False):
        if force:
            self.apply_trip_transition_and_notify(
                transition=RidehailPassengerTripStateMachine.force_quit.name,
                data={
                    'sim_clock': sim_clock,
                    'current_loc': current_loc,
                },
                context={}
            )
        else:
            self.apply_trip_transition_and_notify(
                transition=RidehailPassengerTripStateMachine.end_trip.name,
                data={
                    'sim_clock': sim_clock,
                    'current_loc': current_loc,
                },
                context={}
            )

    def refresh(self):
        if self.trip is not None:
            response = self._get_trip()

            if is_success(response.status_code):
                self.trip = response.json()
            else:
                raise RefreshException(f'PassengerTripManager.refresh: Failed getting response for {self.trip["_id"]} Got {response.text}')

