import os, sys

from dateutil.relativedelta import relativedelta
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import json, requests
import paho.mqtt.client as paho
from datetime import datetime

from messenger_service import Messenger
# from loc_service import OSRMClient

from utils import transform_lonlat_webmercator, itransform_lonlat_webmercator
from utils.user_registry import UserRegistry
from config import settings

from lib import RidehailPassengerTripStateMachine, RidehailDriverTripStateMachine

import websockets, asyncio

class AnalyticsApp:
    ''' '''

    def __init__(self, run_id, sim_clock, credentials):
        ''' '''
        self.run_id = run_id
        self.credentials = credentials
        # self.solver_params = solver_params
        # self.sim_clock = sim_clock

        self.user = UserRegistry(sim_clock, credentials, role='admin')

        self.messenger = Messenger(run_id, credentials)

    def get_active_driver_trips(self, sim_clock):
        ''' '''
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/driver/trip"
        waypoint_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/waypoint"

        params = {
            "where": json.dumps({
                "$and": [
                    {"run_id": self.run_id},
                    {"is_active": True},
                ]
            })
        }

        response = requests.get(driver_trip_url, headers=self.user.get_headers(), params=params)
        response_items = response.json()['_items']

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
        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/passenger/trip"
        waypoint_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.run_id}/waypoint"

        display_expiry_time = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT") - relativedelta(minutes=2)
        # print(sim_clock, datetime.strftime(display_expiry_time, "%a, %d %b %Y %H:%M:%S GMT"))

        params = {
            "where": json.dumps({
                "$and": [
                    {"run_id": self.run_id},
                    {"$or": [
                        {"state": {"$in": [RidehailPassengerTripStateMachine.passenger_requested_trip.identifier]} },
                        {"$and": [
                            {"state": {"$in": [RidehailPassengerTripStateMachine.passenger_completed_trip.identifier,
                                            RidehailPassengerTripStateMachine.passenger_cancelled_trip.identifier,
                                            ]}},
                            {"sim_clock": {"$gte": datetime.strftime(display_expiry_time, "%a, %d %b %Y %H:%M:%S GMT")}},
                            ],
                        }
                    ]}
                ]
            })
        }

        response = requests.get(passenger_trip_url, headers=self.user.get_headers(), params=params)
        response_items = response.json()['_items']

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
            if (trip.get('current_route_coords') is None) or (len(trip.get('current_route_coords')) == 1):
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

            else:
                current_route_coords = trip['current_route_coords']

                transformed_current_route_coords = itransform_lonlat_webmercator([[item[1], item[0]] for item in current_route_coords])
                driver_feature = {
                    "attributes": {
                        "OBJECTID": trip['last_waypoint_id'],
                        "TRACKID": id,
                        "CLASS": 'driver',
                        "STATUS": trip['state']
                    },
                    "geometry": {
                        "paths": list(transformed_current_route_coords)
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
        # # self.messenger.client.publish('test', json.dumps(location_stream))
        # # self.messenger.client.publish('test', json.dumps(route_stream))

        # # This is a publication channel that should be used by visualizer
        # # Note run_id should be made available to the listener so that it can listen to the proper channel
            self.messenger.client.publish(f'anaytics/location_stream', json.dumps(location_stream))
            self.messenger.client.publish(f'anaytics/route_stream', json.dumps(route_stream))
        elif settings['WEBSOCKET_SERVICE'] == 'WS':
            # # using ArcGIS compatible Websockets Service
            async def publish_location_stream_async(location_stream):
                uri = f"{settings['WS_SERVER']}/location_stream"
                async with websockets.connect(uri) as websocket:
                    await websocket.send(json.dumps(location_stream))

            async def publish_route_stream_async(route_stream):
                uri = f"{settings['WS_SERVER']}/route_stream"
                async with websockets.connect(uri) as websocket:
                    await websocket.send(json.dumps(route_stream))

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

