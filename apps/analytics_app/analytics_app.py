import logging
import os, sys
import traceback

from dateutil.relativedelta import relativedelta

from apps.utils.utils import is_success
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import json, requests
import paho.mqtt.client as paho
from datetime import datetime

# from apps.messenger_service import Messenger

# from apps.utils import transform_lonlat_webmercator, itransform_lonlat_webmercator
from apps.loc_service import transform_lonlat_webmercator, itransform_lonlat_webmercator
from apps.utils.user_registry import UserRegistry
from apps.config import settings

from apps.state_machine import RidehailPassengerTripStateMachine, RidehailDriverTripStateMachine

import websockets, asyncio

class AnalyticsApp:
    ''' '''

    def __init__(self, run_id, sim_clock, credentials, messenger):
        ''' '''
        self.run_id = run_id
        self.credentials = credentials

        self.user = UserRegistry(sim_clock, credentials, role='admin')

        # self.messenger = Messenger(credentials)
        self.messenger = messenger
        self.server_max_results = 50 # make sure this is in sync with server

        self.passenger_trips_for_metric = None
        self.driver_trips_for_metric = None

    def logout(self): #, sim_clock, current_loc):
        ''' '''
        logging.debug(f'logging out Analytics Service ')

        # self.messenger.disconnect()

        self.exited_market = True

    def get_active_driver_trips(self, sim_clock):
        ''' '''
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip"
        waypoint_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/waypoint"

        got_results = True
        response_items = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"is_active": True},
                    ]
                }),
                'page': page,
                "max_results": self.server_max_results
            }

            response = requests.get(driver_trip_url, headers=self.user.get_headers(), params=params)
            # response_items = response.json()['_items']
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                response_items.extend(response.json()['_items'])
                page += 1

        driver_trips = {}
        for item in response_items:
            waypoint_params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"trip": item['_id']},
                    ]
                }),
                "sort": "-counter",
                "max_results": 1
            }
            waypoint_response = requests.get(waypoint_url, headers=self.user.get_headers(), params=waypoint_params)
            waypoint = waypoint_response.json()['_items'][0]

            item['last_waypoint_id'] = waypoint['_id']

            driver_trips[item['driver']] = item


        return driver_trips

    def get_active_passenger_trips(self, sim_clock):
        ''' '''
        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip"
        waypoint_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/waypoint"

        display_expiry_time = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT") - relativedelta(seconds=30)
        # print(sim_clock, datetime.strftime(display_expiry_time, "%a, %d %b %Y %H:%M:%S GMT"))

        got_results = True
        response_items = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        # {"is_active": True},
                        {"$or": [
                            {"state": {"$in": [RidehailPassengerTripStateMachine.passenger_requested_trip.identifier,
                                               RidehailPassengerTripStateMachine.passenger_assigned_trip.identifier,
                                               RidehailPassengerTripStateMachine.passenger_accepted_trip.identifier,
                                               RidehailPassengerTripStateMachine.passenger_waiting_for_pickup.identifier,]} },
                            {"$and": [
                                {"state": {"$in": [RidehailPassengerTripStateMachine.passenger_completed_trip.identifier,
                                                RidehailPassengerTripStateMachine.passenger_cancelled_trip.identifier,
                                                ]}},
                                {"sim_clock": {"$gte": datetime.strftime(display_expiry_time, "%a, %d %b %Y %H:%M:%S GMT")}},
                                ],
                            }
                        ]}
                    ]
                }),
                'page': page,
                "max_results": self.server_max_results
            }

            response = requests.get(passenger_trip_url, headers=self.user.get_headers(), params=params)
            # response_items = response.json()['_items']
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                response_items.extend(response.json()['_items'])
                page += 1

        passenger_trips = {}
        for item in response_items:
            waypoint_params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"trip": item['_id']},
                    ]
                }),
                "sort": "-counter",
                "max_results": 1
            }
            waypoint_response = requests.get(waypoint_url, headers=self.user.get_headers(), params=waypoint_params)
            waypoint = waypoint_response.json()['_items'][0]

            item['last_waypoint_id'] = waypoint['_id']

            passenger_trips[item['passenger']] = item

        return passenger_trips

    def publish_active_trips(self, sim_clock):
        ''' '''
        # self.sim_clock = sim_clock
        driver_trips = self.get_active_driver_trips(sim_clock)
        passenger_trips = self.get_active_passenger_trips(sim_clock)

        location_stream = {
            "type":"featureResult",
            "features": []
        }
        route_stream = {
            "type":"featureResult",
            "features": []
        }

        for id, trip in driver_trips.items():
            # if (trip.get('projected_path') is None) or (len(trip.get('projected_path')) == 1):
            current_loc = trip['current_loc']

            transformed_loc = transform_lonlat_webmercator(current_loc['coordinates'][1], current_loc['coordinates'][0])
            driver_feature = {
                "attributes": {
                    "OBJECTID": trip['last_waypoint_id'],
                    "TRACKID": id,
                    "CLASS": 'driver',
                    "STATUS": trip['state']
                },
                "geometry": {
                    "x": transformed_loc[0],
                    "y": transformed_loc[1]
                }
            }
            location_stream['features'].append(driver_feature)

            # else:
            if (trip.get('projected_path') is not None) and (len(trip.get('projected_path')) > 1):
                projected_path = trip['projected_path']

                transformed_projected_path = itransform_lonlat_webmercator([[item[1], item[0]] for item in projected_path])
                driver_feature = {
                    "attributes": {
                        "OBJECTID": trip['last_waypoint_id'],
                        "TRACKID": id,
                        "CLASS": 'driver',
                        "STATUS": trip['state']
                    },
                    "geometry": {
                        "paths": [list(transformed_projected_path)] ### NOTE Paths is a [[[x,y], [x,y]]] format
                    }
                }
                route_stream['features'].append(driver_feature)

        for id, trip in passenger_trips.items():
            current_loc = trip['current_loc']

            transformed_loc = transform_lonlat_webmercator(current_loc['coordinates'][1], current_loc['coordinates'][0])
            passenger_feature = {
                "attributes": {
                    "OBJECTID": trip['last_waypoint_id'],
                    "TRACKID": id,
                    "CLASS": 'passenger',
                    "STATUS": trip['state']
                },
                "geometry": {
                    "x": transformed_loc[0],
                    "y": transformed_loc[1]
                }
            }
            location_stream['features'].append(passenger_feature)


        if settings['WEBSOCKET_SERVICE'] == 'MQTT':
            # # using Web MQTT as Websockets service

            # # # This code publishes on 'test' channel and can be received using rabbitmq's web_mqtt_examples.echo
            # self.messenger.client.publish('test', json.dumps(location_stream))
            # self.messenger.client.publish('test', json.dumps(route_stream))

            # # This is a publication channel that should be used by visualizer
            # # Note run_id should be made available to the listener so that it can listen to the proper channel
            self.messenger.client.publish(f'anaytics/location_stream', json.dumps(location_stream))
            self.messenger.client.publish(f'anaytics/route_stream', json.dumps(route_stream))
        elif settings['WEBSOCKET_SERVICE'] == 'WS':
            # # using ArcGIS compatible Websockets Service
            async def publish_location_stream_async(location_stream):
                uri = f"{settings['WS_SERVER']}/location_stream"
                async with websockets.connect(uri) as websocket:
                    resp = await websocket.send(json.dumps(location_stream))
                    # print(resp)

            async def publish_route_stream_async(route_stream):
                uri = f"{settings['WS_SERVER']}/route_stream"
                async with websockets.connect(uri) as websocket:
                    resp = await websocket.send(json.dumps(route_stream))
                    # print(resp)

            asyncio.run(publish_location_stream_async(location_stream))
            asyncio.run(publish_route_stream_async(route_stream))
            # -------------------

        return location_stream, route_stream

    def get_history_as_paths(self, timewindow_start, timewindow_end):
        ''' '''
        waypoint_history_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/waypoint_history/all_trips"

        params = {
            'from': datetime.strftime(timewindow_start, '%Y%m%d%H%M%S'),
            'to': datetime.strftime(timewindow_end, '%Y%m%d%H%M%S'),
        }

        response = requests.get(waypoint_history_url, headers=self.user.get_headers(), params=params)
        # print(response.url)
        # print(response.json())

        return response.json()


    def prep_metric_computation_queries(self, start_time, end_time):
        ''' '''
        self.get_passenger_trips_for_metric(start_time, end_time)
        self.get_driver_trips_for_metric(start_time, end_time)

    def get_passenger_trips_for_metric(self, start_time, end_time):

        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/ride_hail/trip"

        got_results = True
        self.passenger_trips_for_metric = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"state": {
                            '$in': [RidehailPassengerTripStateMachine.passenger_cancelled_trip.identifier,
                                #    RidehailPassengerTripStateMachine.passenger_pickedup.identifier]
                                   RidehailPassengerTripStateMachine.passenger_completed_trip.identifier]
                            }},
                        {"sim_clock": {
                            "$gte": datetime.strftime(start_time, "%a, %d %b %Y %H:%M:%S GMT"),
                            "$lt": datetime.strftime(end_time, "%a, %d %b %Y %H:%M:%S GMT"),
                        }}
                ]}),
                'page': page,
                "max_results": self.server_max_results
            }

            response = requests.get(passenger_trip_url, headers=self.user.get_headers(), params=params)
            # response_items = response.json()['_items']
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                self.passenger_trips_for_metric.extend(response.json()['_items'])
                page += 1

    def get_driver_trips_for_metric(self, start_time, end_time):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/ride_hail/trip"

        got_results = True
        self.driver_trips_for_metric = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"state": {
                            '$in': [RidehailDriverTripStateMachine.driver_completed_trip.identifier,]
                            }},
                        {'is_occupied': True},
                        {"sim_clock": {
                            "$gte": datetime.strftime(start_time, "%a, %d %b %Y %H:%M:%S GMT"),
                            "$lt": datetime.strftime(end_time, "%a, %d %b %Y %H:%M:%S GMT"),
                        }}
                ]}),
                'page': page,
                "max_results": self.server_max_results
            }

            response = requests.get(driver_trip_url, headers=self.user.get_headers(), params=params)
            # response_items = response.json()['_items']
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                self.driver_trips_for_metric.extend(response.json()['_items'])
                page += 1

    def active_driver_count(self):
        ''' NOTE: no run_id in url
        '''
        driver_trip_count_url = f"{settings['OPENRIDE_SERVER_URL']}/driver/ride_hail/trip/count_active"

        params = {
            "aggregate": json.dumps({
                "$run_id": self.run_id,
                "$is_active": True,
            })
        }

        response = requests.get(driver_trip_count_url, headers=self.user.get_headers(), params=params)

        try:
            if response.json()['_items'] == []:
                return 0
            else:
                return response.json()['_items'][0].get('num_trips', 0)
        except:
            logging.error(response.status_code, response.text)

    def active_passenger_count(self):
        ''' NOTE: no run_id in url
        '''
        passenger_trip_count_url = f"{settings['OPENRIDE_SERVER_URL']}/passenger/ride_hail/trip/count_active"

        params = {
            "aggregate": json.dumps({
                "$run_id": self.run_id,
                "$is_active": True,
            })
        }

        response = requests.get(passenger_trip_count_url, headers=self.user.get_headers(), params=params)
        try:
            if response.json()['_items'] == []:
                return 0
            else:
                return response.json()['_items'][0].get('num_trips', 0)
        except:
            logging.error(response.status_code, response.text)

    def compute_revenue(self):
        step_revenue = 0
        for item in self.passenger_trips_for_metric:
            # if item['state'] == RidehailPassengerTripStateMachine.passenger_pickedup.identifier:
            if item['state'] == RidehailPassengerTripStateMachine.passenger_completed_trip.identifier:
                step_revenue += item['trip_price']

        return step_revenue

    def compute_cancelled(self):
        num_cancelled = 0
        for item in self.passenger_trips_for_metric:
            if item['state'] == RidehailPassengerTripStateMachine.passenger_cancelled_trip.identifier:
                num_cancelled += 1

        return num_cancelled

    def compute_served(self):
        num_served = 0
        for item in self.passenger_trips_for_metric:
            # if item['state'] == RidehailPassengerTripStateMachine.passenger_pickedup.identifier:
            if item['state'] == RidehailPassengerTripStateMachine.passenger_completed_trip.identifier:
                num_served += 1

        return num_served

    # def compute_accepted(self):
    #     num_accepted = 0
    #     for item in self.passenger_trips_for_metric:
    #         if item['state'] == RidehailDriverTripStateMachine.driver_moving_to_pickup.identifier:
    #             num_accepted += 1

    #     return num_accepted

    def compute_waiting_time(self):
        wait_time_assignment = 0
        wait_time_driver_confirm = 0
        wait_time_total = 0
        wait_time_pickup = 0
        for item in self.passenger_trips_for_metric:
            try:
                # if item['state'] == RidehailPassengerTripStateMachine.passenger_pickedup.identifier:
                if item['state'] == RidehailPassengerTripStateMachine.passenger_completed_trip.identifier:
                    wait_time_driver_confirm += item['stats']['wait_time_driver_confirm']
                    wait_time_total += item['stats']['wait_time_total']
                    wait_time_assignment += item['stats']['wait_time_assignment']
                    wait_time_pickup += item['stats']['wait_time_pickup']
            except Exception as e:
                logging.exception(str(e))
                # logging.exception(f"{traceback.format_exc()}, {item}")
                # logging.warning()

        return {
            'wait_time_driver_confirm': wait_time_driver_confirm,
            'wait_time_total': wait_time_total,
            'wait_time_assignment': wait_time_assignment,
            'wait_time_pickup': wait_time_pickup,
        }

    def compute_service_score(self):
        service_score = 0
        for item in self.driver_trips_for_metric:
            # if item['state'] == RidehailDriverTripStateMachine.driver_moving_to_pickup.identifier:
            if item['state'] == RidehailDriverTripStateMachine.driver_completed_trip.identifier:
                service_score += item['meta']['profile']['service_score']

        return service_score


    def save_kpi(self, sim_clock, kpi_collection):
        ''' '''
        kpi_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/kpi"

        data = []
        for metric, value in kpi_collection.items():
            data.append({
                'metric': metric,
                'value': value,
                'sim_clock': sim_clock,
            })

        response = requests.post(kpi_url, headers=self.user.get_headers(),
                                 data=json.dumps(data))

        if not is_success(response.status_code):
            raise Exception(f"{response.url}, {response.text}")
        # return response.json()

