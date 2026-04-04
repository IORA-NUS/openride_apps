import logging
import os, sys, time

current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from apps.utils.utils import is_success
from apps.ridehail.message_data_models import RequestedTripActionPayload

import json, requests, traceback
from datetime import datetime

from apps.loc_service import OSRMClient

from apps.common.user_registry import UserRegistry
from apps.config import settings, simulation_domains

from apps.ridehail.statemachine import RidehailPassengerTripStateMachine, RidehailDriverTripStateMachine
from apps.ridehail.statemachine import RideHailActions
from .solver import *  # NOTE * is deliberate to load all solvers in globals()
from .manager import AssignmentManager
from orsim.lifecycle import ORSimApp


class AssignmentApp(ORSimApp):
    ''' '''
    @property
    def managed_statemachine(self):
        return None

    @property
    def interaction_ground_truth_list(self):
        return []

    @property
    def runtime_behavior_schema(self):
        return {
            'profile': {
                'type': 'dict',
                'required': True,
                'schema': {
                    'solver': {'type': 'string', 'required': True},
                    'solver_params': {'type': 'dict', 'required': True},
                }
            }
        }

    def __init__(self, run_id, sim_clock, behavior, messenger):
        super().__init__(run_id=run_id,
                         sim_clock=sim_clock,
                         behavior=behavior,
                         messenger=messenger,
                    )
        self.server_max_results = 50  # make sure this is in sync with server

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials, role='admin')

    def _create_manager(self):
        solver_name = self.behavior.get('profile', {}).get('solver')
        solver_params = self.behavior.get('profile', {}).get('solver_params')
        solver = globals()[solver_name](params=solver_params)

        return AssignmentManager(run_id=self.run_id,
                                 sim_clock=self.sim_clock,
                                 user=self.user,
                                 persona=self.behavior.get('persona', {}),
                                 solver=solver)

    def handle_app_topic_messages(self, payload):
        ''' '''
        # Handle any incoming messages on the app topic if needed
        pass

    def get_scale_factor(self, time_step):

        if self.behavior.get('profile', {}).get('solver_params', {}).get('online_metric_scale_strategy') == 'demand':
            try:
                passenger_trip_count_url = f"{settings['OPENRIDE_SERVER_URL']}/{simulation_domains['ridehail']}/{self.run_id}/passenger/trip/count"  # NOTE Absence of run_id in URL
                params = {
                    "aggregate": json.dumps({
                        "$run_id": self.run_id,
                        "$state": {},
                    }),  # The format above is critical. Must match OpenRoad Server API Specs.
                }

                response = requests.get(passenger_trip_count_url, headers=self.user.get_headers(), params=params)
                if is_success(response.status_code):
                    result = response.json()["_items"][0]
                    return result.get('num_trips', time_step)
                else:
                    logging.warning(f"Failed to generate scale factor. returning {time_step=}")
                    return time_step
            except Exception as e:
                logging.exception(str(e))
                return time_step
        else:  # 'time'
            return time_step

    def assign(self, sim_clock, time_step):
        ''' '''

        driver_trip = self.get_driver_trip()
        passenger_trip = self.get_passenger_trip()
        self.manager.refresh()

        driver_locs = {k: v['current_loc'] for k, v in driver_trip.items()}
        passenger_locs = {k: v['pickup_loc'] for k, v in passenger_trip.items()}

        distance_matrix = self.get_distance_matrix(driver_locs, passenger_locs)

        driver_list = [d for k, d in driver_trip.items()]
        passenger_trip_list = [p for k, p in passenger_trip.items()]

        start = time.time()
        try:
            assignment, matched_pairs = self.manager.solver.solve(driver_list, passenger_trip_list, distance_matrix, self.manager.as_dict().get('offline_params'), self.manager.as_dict().get('online_params'))
        except Exception as e:
            logging.exception(traceback.format_exc())
            assignment = []
            matched_pairs = []

        end = time.time()
        scale_factor = self.get_scale_factor(time_step)

        online_params = self.manager.solver.update_online_params(scale_factor, driver_list, passenger_trip_list, matched_pairs, self.manager.as_dict().get('offline_params'), self.manager.as_dict().get('online_params'))
        result = [{
            'driver': item[0]['driver'],
            'passenger': item[1]['passenger'],
            'passenger_trip': item[1]['_id']
        } for item in assignment]

        performance = {
            "run_time": end-start,
            "num_drivers": len(driver_list),
            "num_passenger_trips": len(passenger_trip_list),
            "result": result
        }
        # self.manager.update_engine(sim_clock, online_params, performance)
        self.manager.update_resource({"online_params": online_params, "last_run_performance": performance, "sim_clock": sim_clock})

        return assignment

    def publish(self, assignment):
        ''' '''
        for item in assignment:
            driver = item[0]
            passenger_trip = item[1]

            passenger_assignment = RequestedTripActionPayload(
                action=RideHailActions.REQUESTED_TRIP,
                passenger_id=passenger_trip['passenger'],
                requested_trip=passenger_trip
            )
            self.messenger.client.publish(
                f"{self.run_id}/{driver['driver']}",
                json.dumps(passenger_assignment.__dict__)
            )

    def get_driver_trip(self):
        ''' '''
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{simulation_domains['ridehail']}/{self.run_id}/driver/trip"

        got_results = True
        response_items = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "is_active": True,
                    "is_occupied": False,
                    "state": {"$in": [RidehailDriverTripStateMachine.driver_looking_for_job.name]},
                    "current_loc": {"$geoWithin": {"$geometry": self.manager.solver.params['planning_area']['geometry']}}
                }),
                'projection': json.dumps({
                    '_id': 1,
                    'driver': 1,
                    'current_loc': 1,
                    'meta': 1,
                }),
                'page': page,
                "max_results": self.server_max_results,
            }

            response = requests.get(driver_trip_url, headers=self.user.get_headers(), params=params)
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                response_items.extend(response.json()['_items'])
                page += 1

        driver_trip = {}
        for item in response_items:
            driver_trip[item['driver']] = item

        return driver_trip

    def get_passenger_trip(self):
        ''' '''
        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{simulation_domains['ridehail']}/{self.run_id}/passenger/trip"

        got_results = True
        response_items = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "is_active": True,
                    "state": {"$in": [RidehailPassengerTripStateMachine.passenger_requested_trip.name]},
                    "pickup_loc": {"$geoWithin": {"$geometry": self.manager.solver.params['planning_area']['geometry']}}
                }),
                'projection': json.dumps({
                    '_id': 1,
                    'passenger': 1,
                    'pickup_loc': 1,
                    'dropoff_loc': 1,
                    'trip_price': 1,
                    'meta': 1,
                }),
                'page': page,
                "max_results": self.server_max_results,
            }

            response = requests.get(passenger_trip_url, headers=self.user.get_headers(), params=params)
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                response_items.extend(response.json()['_items'])
                page += 1

        passenger_trip = {}
        for item in response_items:
            passenger_trip[item['passenger']] = item

        return passenger_trip

    def get_distance_matrix(self, driver_locs, passenger_locs):
        '''
        NOTE: Ensure no bugs due to Dict-List conversions.
        '''

        distance_matrix = OSRMClient.get_distance_matrix(driver_locs, passenger_locs, units='duration')

        return distance_matrix


if __name__ == "__main__":

    solver_params = {
        'planning_area': {
            'name': 'Singapore',
            'geometry': {
                'center': {'type': 'Point', 'coordinates': (103.833057754201, 1.41709038337595)},
                'radius': 5000000,
            },
        },

        'offline_params': {
            'reverseParameter': 480,
            'reverseParameter2': 2.5,
            'gamma': 1.2,

            'targetReversePickupTime': 4915 * 1.2,
            'targetServiceScore': 5439 * 1.2,
            'targetRevenue': 4185 * 1.2,
        },
        'online_params': {
            'weightPickupTime': 1,
            'weightRevenue': 1,
            'weightServiceScore': 1,
        },
    }

    now = datetime.strftime(datetime.utcnow(), "%a, %d %b %Y %H:%M:%S GMT")

    app = AssignmentApp(now, {'email': 'admin@test.com', 'password': 'password'}, solver_params) # changed to bahevior

    app.assign()

    print('driver', app.driver)
    print('passenger', app.passenger)
