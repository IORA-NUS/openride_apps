import logging
import os, sys, time
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import json, requests
import paho.mqtt.client as paho
from datetime import datetime

from apps.messenger_service import Messenger
from apps.loc_service import OSRMClient

from apps.utils.user_registry import UserRegistry
from apps.config import settings

from apps.state_machine import RidehailPassengerTripStateMachine, RidehailDriverTripStateMachine
from .solver import * # NOTE * is deliberate to load all solvers in globals()
from .engine_manager import EngineManager

class AssignmentApp:
    ''' '''

    def __init__(self, run_id, sim_clock, credentials, solver_str, solver_params):
        ''' '''
        self.run_id = run_id
        self.credentials = credentials
        self.solver_params = solver_params

        self.user = UserRegistry(sim_clock, credentials, role='admin')

        self.solver = globals()[solver_str](self.solver_params)

        self.engine = EngineManager(self.run_id, sim_clock, self.user, self.solver)

        self.messenger = Messenger(credentials)
        self.server_max_results = 50 # make sure this is in sync with server


    def assign(self, sim_clock, clock_tick):
        ''' '''

        driver_trip = self.get_driver_trip()
        passenger_trip = self.get_passenger_trip()
        self.engine.refresh()

        driver_locs = {k: v['current_loc'] for k, v in driver_trip.items()}
        passenger_locs = {k: v['pickup_loc'] for k, v in passenger_trip.items()}

        distance_matrix = self.get_distance_matrix(driver_locs, passenger_locs)

        driver_list = [d['driver'] for k, d in driver_trip.items()]
        passenger_trip_list = [p for k, p in passenger_trip.items()]
        # print('driver_list', driver_list)
        # print('passenger_list', passenger_list)

        # print('Before Solve')
        start = time.time()
        try:
            assignment, matched_pairs = self.solver.solve(driver_list, passenger_trip_list, distance_matrix, self.engine.as_dict().get('online_params'))
        except Exception as e:
            logging.exception(e)
        end = time.time()
        # print('after Solve')

        online_params = self.solver.update_online_params(clock_tick, driver_list, passenger_trip_list, matched_pairs, self.engine.as_dict().get('offline_params'), self.engine.as_dict().get('online_params'))
        # print('after update_online_params')
        result = [{
            'driver': item[0]['_id'],
            'passenger': item[1]['passenger'],
            'passenger_trip': item[1]['_id']
        } for item in assignment]

        performance =  {
            "Runtime": end-start,
            "Num Drivers": len(driver_list),
            "Num Passenger Trips": len(passenger_trip_list),
            "result": result
        }
        self.engine.update_engine(sim_clock, online_params, performance)
        # print('after update_engine')

        return assignment

    def publish(self, assignment):
        ''' '''
        # print(assignment)

        for item in assignment:
            driver = item[0]
            passenger_trip = item[1]

            driver_assignment = {
                "action": "assigned",
                "driver_id": driver['_id'],
            }
            passenger_assignment = {
                "action": "requested_trip",
                "passenger_id": passenger_trip['passenger'],
                "requested_trip": passenger_trip
            }

            self.messenger.client.publish(f"{self.run_id}/{passenger_trip['passenger']}", json.dumps(driver_assignment))
            self.messenger.client.publish(f"{self.run_id}/{driver['_id']}", json.dumps(passenger_assignment))



    def get_driver_trip(self):
        ''' '''
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip"

        got_results = True
        response_items = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "is_active": True,
                    "is_occupied": False,
                    "state": {"$in": [RidehailDriverTripStateMachine.driver_looking_for_job.identifier] },
                    "current_loc": {"$geoWithin": {"$geometry": self.solver.params['planning_area']['geometry']}}
                }),
                "embedded": json.dumps({
                    "driver": 1
                }),
                'page': page,
                "max_results": self.server_max_results,
            }

            response = requests.get(driver_trip_url, headers=self.user.get_headers(), params=params)
            # response_items = response.json()['_items']
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                response_items.extend(response.json()['_items'])
                page += 1

        driver_trip = {}
        for item in response_items:
            driver_trip[item['driver']['_id']] = item

        return driver_trip

    def get_passenger_trip(self):
        ''' '''
        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip"

        got_results = True
        response_items = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "is_active": True,
                    "state": {"$in": [RidehailPassengerTripStateMachine.passenger_requested_trip.identifier]},
                    # "pickup_loc": {"$near": {"$geometry": self.solver.params['area']['center'], "$maxDistance": self.solver.params['area']['radius']}}
                    "pickup_loc": {"$geoWithin": {"$geometry": self.solver.params['planning_area']['geometry']}}
                }),
                'page': page,
                "max_results": self.server_max_results,
            }

            response = requests.get(passenger_trip_url, headers=self.user.get_headers(), params=params)
            # response_items = response.json()['_items']
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
        # print(distance_matrix)

        return distance_matrix


if __name__ == "__main__":

    solver_params = {
        'planning_area': {
            'name': 'Singapore',
            'geometry': {
                # THis must be a MultiPolygon describing the specific region where this engine will gather Supply / demand
                'center': {'type': 'Point', 'coordinates': (103.833057754201, 1.41709038337595)},
                'radius': 5000000, # meters
            },
        },

        'offline_params': {
            'reverseParameter': 480,  # 480;
            'reverseParameter2': 2.5,
            'gamma': 1.2,     # the target below is estimated from historical data

            # KPI Targets
            'targetReversePickupTime': 4915 * 12, # gamma
            'targetServiceScore': 5439 * 1.2, # gamma
            'targetRevenue': 4185 * 1.2, # gamma
        },
        'online_params': {
            'weightPickupTime': 1,
            'weightRevenue': 1,
            'weightServiceScore': 1,
        },
    }

    now = datetime.strftime(datetime.utcnow(), "%a, %d %b %Y %H:%M:%S GMT")

    app = AssignmentApp(now, {'email': 'admin@test.com', 'password': 'password'}, solver_params)

    app.assign()

    print('driver', app.driver)
    print('passenger', app.passenger)
