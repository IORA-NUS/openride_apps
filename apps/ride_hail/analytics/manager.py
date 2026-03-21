import requests, json
from http import HTTPStatus

import logging
from apps.config import settings, simulation_domains
from apps.utils import id_generator, is_success
# from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine


from apps.agent_core.base_manager import BaseManager
from apps.common.resource_client_mixin import ResourceClientMixin

class AnalyticsManager(ResourceClientMixin, BaseManager):

    def __init__(self, run_id, sim_clock, user, persona):
        self.run_id = run_id
        self.user = user
        self.persona = persona
        self.resource_id = '' # keep this a string
        self.resource_type = 'kpi'

        self.simulation_domain = simulation_domains['ridehail']


        # AnalyticsManager does not require initialization of a resource in the same way as other managers, since it is primarily responsible for saving KPIs. However, we can still create a resource to store metadata about the analytics if needed. For now, we'll skip resource initialization for AnalyticsManager.
        # self.resource = self.init_resource(sim_clock, data=data, params={})
        self.resource = {}


    def login(self, sim_clock):
        """
        AnalyticsManager does not require login. This is a no-op for interface compatibility.
        """
        pass

    def logout(self, sim_clock):
        """
        AnalyticsManager does not require logout. This is a no-op for interface compatibility.
        """
        pass

    def get_active_driver_trips(self, sim_clock):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/driver/trip"
        waypoint_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/waypoint"

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
                "max_results": 50
            }

            response = requests.get(driver_trip_url, headers=self.user.get_headers(), params=params)
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
        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/passenger/trip"
        waypoint_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/waypoint"
        from dateutil.relativedelta import relativedelta
        from datetime import datetime
        display_expiry_time = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT") - relativedelta(seconds=30)
        got_results = True
        response_items = []
        page = 1
        from apps.state_machine import RidehailPassengerTripStateMachine
        while got_results:
            params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"$or": [
                            {"state": {"$in": [
                                RidehailPassengerTripStateMachine.passenger_requested_trip.name,
                                RidehailPassengerTripStateMachine.passenger_assigned_trip.name,
                                RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
                                RidehailPassengerTripStateMachine.passenger_waiting_for_pickup.name,
                            ]}},
                            {"$and": [
                                {"state": {"$in": [
                                    RidehailPassengerTripStateMachine.passenger_completed_trip.name,
                                    RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
                                ]}},
                                {"sim_clock": {"$gte": datetime.strftime(display_expiry_time, "%a, %d %b %Y %H:%M:%S GMT")}},
                            ]}
                        ]}
                    ]
                }),
                'page': page,
                "max_results": 50
            }
            response = requests.get(passenger_trip_url, headers=self.user.get_headers(), params=params)
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
        from apps.loc_service import transform_lonlat_webmercator, itransform_lonlat_webmercator
        driver_trips = self.get_active_driver_trips(sim_clock)
        passenger_trips = self.get_active_passenger_trips(sim_clock)
        location_stream = {
            "type": "featureResult",
            "features": []
        }
        route_stream = {
            "type": "featureResult",
            "features": []
        }
        for id, trip in driver_trips.items():
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
                        "paths": [list(transformed_projected_path)]
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
            self.messenger.client.publish(f'anaytics/location_stream', json.dumps(location_stream))
            self.messenger.client.publish(f'anaytics/route_stream', json.dumps(route_stream))
        elif settings['WEBSOCKET_SERVICE'] == 'WS':
            import websockets, asyncio
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
        return location_stream, route_stream

    def get_history_as_paths(self, timewindow_start, timewindow_end):
        waypoint_history_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/waypoint_history/all_trips"
        from datetime import datetime
        params = {
            'from': datetime.strftime(timewindow_start, '%Y%m%d%H%M%S'),
            'to': datetime.strftime(timewindow_end, '%Y%m%d%H%M%S'),
        }
        response = requests.get(waypoint_history_url, headers=self.user.get_headers(), params=params)
        return response.json()

    def get_passenger_trips_for_metric(self, start_time, end_time):
        passenger_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/passenger/trip"
        from apps.state_machine import RidehailPassengerTripStateMachine
        from datetime import datetime
        got_results = True
        passenger_trips_for_metric = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"state": {
                            '$in': [
                                RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
                                RidehailPassengerTripStateMachine.passenger_completed_trip.name,
                            ]
                        }},
                        {"sim_clock": {
                            "$gte": datetime.strftime(start_time, "%a, %d %b %Y %H:%M:%S GMT"),
                            "$lt": datetime.strftime(end_time, "%a, %d %b %Y %H:%M:%S GMT"),
                        }}
                    ]
                }),
                'page': page,
                "max_results": 50
            }
            response = requests.get(passenger_trip_url, headers=self.user.get_headers(), params=params)
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                passenger_trips_for_metric.extend(response.json()['_items'])
                page += 1
        return passenger_trips_for_metric

    def get_driver_trips_for_metric(self, start_time, end_time):
        driver_trip_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/driver/trip"
        from apps.state_machine import RidehailDriverTripStateMachine
        from datetime import datetime
        got_results = True
        driver_trips_for_metric = []
        page = 1
        while got_results:
            params = {
                "where": json.dumps({
                    "$and": [
                        {"run_id": self.run_id},
                        {"state": {'$in': [RidehailDriverTripStateMachine.driver_completed_trip.name]}},
                        {'is_occupied': True},
                        {"sim_clock": {
                            "$gte": datetime.strftime(start_time, "%a, %d %b %Y %H:%M:%S GMT"),
                            "$lt": datetime.strftime(end_time, "%a, %d %b %Y %H:%M:%S GMT"),
                        }}
                    ]
                }),
                'page': page,
                "max_results": 50
            }
            response = requests.get(driver_trip_url, headers=self.user.get_headers(), params=params)
            if response.json()['_items'] == []:
                got_results = False
                break
            else:
                driver_trips_for_metric.extend(response.json()['_items'])
                page += 1
        return driver_trips_for_metric

    def active_driver_count(self):
        driver_trip_count_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/driver/trip/count_active"
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
        except Exception:
            logging.error(response.status_code, response.text)

    def active_passenger_count(self):
        passenger_trip_count_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/passenger/trip/count_active"
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
        except Exception:
            logging.error(response.status_code, response.text)

    def save_kpi(self, sim_clock, kpi_collection):
        kpi_url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/kpi"
        import json
        data = []
        for metric, value in kpi_collection.items():
            data.append({
                'metric': metric,
                'value': value,
                'sim_clock': sim_clock,
            })
        response = requests.post(kpi_url, headers=self.user.get_headers(), data=json.dumps(data))
        from apps.utils.utils import is_success
        if not is_success(response.status_code):
            raise Exception(f"{response.url}, {response.text}")


