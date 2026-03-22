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

    def on_init(self):
        pass

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

    # --- Helper methods for endpoint URL construction ---
    def _trip_url(self, role):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/{role}/trip"

    def _waypoint_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/waypoint"

    def _kpi_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/kpi"

    def _waypoint_history_url(self):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/waypoint_history/all_trips"

    def _trip_count_url(self, role):
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/{role}/trip/count_active"

    # --- Helper methods for HTTP requests ---
    def _get(self, url, params=None):
        response = requests.get(url, headers=self.user.get_headers(), params=params or {})
        self._check_response(response)
        return response.json()

    def _post(self, url, data):
        response = requests.post(url, headers=self.user.get_headers(), data=json.dumps(data))
        self._check_response(response)
        return response.json()



    def get_active_driver_trips(self, sim_clock):
        driver_trip_url = self._trip_url('driver')
        waypoint_url = self._waypoint_url()
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
            result = self._get(driver_trip_url, params)
            if result['_items'] == []:
                got_results = False
                break
            else:
                response_items.extend(result['_items'])
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
            waypoint_result = self._get(waypoint_url, waypoint_params)
            waypoint = waypoint_result['_items'][0]
            item['last_waypoint_id'] = waypoint['_id']
            driver_trips[item['driver']] = item
        return driver_trips

    def get_active_passenger_trips(self, sim_clock):
        passenger_trip_url = self._trip_url('passenger')
        waypoint_url = self._waypoint_url()
        from dateutil.relativedelta import relativedelta
        from datetime import datetime
        display_expiry_time = datetime.strptime(sim_clock, "%a, %d %b %Y %H:%M:%S GMT") - relativedelta(seconds=30)
        got_results = True
        response_items = []
        page = 1
        from apps.ride_hail.statemachine import RidehailPassengerTripStateMachine
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
            result = self._get(passenger_trip_url, params)
            if result['_items'] == []:
                got_results = False
                break
            else:
                response_items.extend(result['_items'])
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
            waypoint_result = self._get(waypoint_url, waypoint_params)
            waypoint = waypoint_result['_items'][0]
            item['last_waypoint_id'] = waypoint['_id']
            passenger_trips[item['passenger']] = item
        return passenger_trips

    def get_history_as_paths(self, timewindow_start, timewindow_end):
        waypoint_history_url = self._waypoint_history_url()
        from datetime import datetime
        params = {
            'from': datetime.strftime(timewindow_start, '%Y%m%d%H%M%S'),
            'to': datetime.strftime(timewindow_end, '%Y%m%d%H%M%S'),
        }
        return self._get(waypoint_history_url, params)

    def get_passenger_trips_for_metric(self, start_time, end_time):
        passenger_trip_url = self._trip_url('passenger')
        from apps.ride_hail.statemachine import RidehailPassengerTripStateMachine
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
            result = self._get(passenger_trip_url, params)
            if result['_items'] == []:
                got_results = False
                break
            else:
                passenger_trips_for_metric.extend(result['_items'])
                page += 1
        return passenger_trips_for_metric

    def get_driver_trips_for_metric(self, start_time, end_time):
        driver_trip_url = self._trip_url('driver')
        from apps.ride_hail.statemachine import RidehailDriverTripStateMachine
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
            result = self._get(driver_trip_url, params)
            if result['_items'] == []:
                got_results = False
                break
            else:
                driver_trips_for_metric.extend(result['_items'])
                page += 1
        return driver_trips_for_metric

    def active_driver_count(self):
        driver_trip_count_url = self._trip_count_url('driver')
        params = {
            "aggregate": json.dumps({
                "$run_id": self.run_id,
                "$is_active": True,
            })
        }
        try:
            result = self._get(driver_trip_count_url, params)
            if result['_items'] == []:
                return 0
            else:
                return result['_items'][0].get('num_trips', 0)
        except Exception as e:
            logging.error(str(e))

    def active_passenger_count(self):
        passenger_trip_count_url = self._trip_count_url('passenger')
        params = {
            "aggregate": json.dumps({
                "$run_id": self.run_id,
                "$is_active": True,
            })
        }
        try:
            result = self._get(passenger_trip_count_url, params)
            if result['_items'] == []:
                return 0
            else:
                return result['_items'][0].get('num_trips', 0)
        except Exception as e:
            logging.error(str(e))

    def save_kpi(self, sim_clock, kpi_collection):
        kpi_url = self._kpi_url()
        data = []
        for metric, value in kpi_collection.items():
            data.append({
                'metric': metric,
                'value': value,
                'sim_clock': sim_clock,
            })
        self._post(kpi_url, data)


