import logging
import os, sys
import traceback

from dateutil.relativedelta import relativedelta

from apps.utils.utils import is_success
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import json, requests
from datetime import datetime

# from apps.messenger_service import Messenger

# from apps.utils import transform_lonlat_webmercator, itransform_lonlat_webmercator
from apps.loc_service import transform_lonlat_webmercator, itransform_lonlat_webmercator
from apps.utils.user_registry import UserRegistry
from apps.config import settings, simulation_domains

from apps.state_machine import RidehailPassengerTripStateMachine, RidehailDriverTripStateMachine
from apps.agent_core.base_app import BaseApp

from .manager import AnalyticsManager

import websockets, asyncio

from apps.utils import time_to_str, str_to_time


class AnalyticsApp(BaseApp):
    ''' '''

    def __init__(self, run_id, sim_clock, credentials, messenger, persona):
        super().__init__(run_id=run_id,
                         sim_clock=sim_clock,
                         credentials=credentials,
                         messenger=messenger,
                         persona=persona)
        self.kpi_collection = {
            'revenue': 0,
            'num_cancelled': 0,
            'num_served': 0,
            'wait_time_driver_confirm': 0,
            'wait_time_total': 0,
            'wait_time_assignment': 0,
            'wait_time_pickup': 0,
            'service_score': 0,
        }
        self.server_max_results = 50  # make sure this is in sync with server

        self.passenger_trips_for_metric = None
        self.driver_trips_for_metric = None

    def create_user(self):
        return UserRegistry(self.sim_clock, self.credentials, role='admin')

    def create_manager(self):
        return AnalyticsManager(self.run_id, self.sim_clock, self.user, None)
        # pass

    def launch(self):
        pass

    # def close(self):  # , sim_clock, current_loc):
    #     ''' '''
    #     logging.debug(f'logging out Analytics Service ')

    #     # self.messenger.disconnect()

    #     self.exited_market = True

    def compute_all_metrics(self, start_time, end_time):
        # logging.info(f"[compute_all_metrics] Starting metric computation for {time_to_str(start_time)} to {time_to_str(end_time)}")
        try:
            self.prep_metric_computation_queries(start_time, end_time)

            # Log if backend returned empty data
            if not self.passenger_trips_for_metric:
                logging.warning(f"No passenger trips returned for metric computation at {time_to_str(end_time)}")
            if not self.driver_trips_for_metric:
                logging.warning(f"No driver trips returned for metric computation at {time_to_str(end_time)}")

            self.kpi_collection['revenue'] = self.compute_revenue()
            self.kpi_collection['num_cancelled'] = self.compute_cancelled()
            self.kpi_collection['num_served'] = self.compute_served()

            waiting_time = self.compute_waiting_time()
            self.kpi_collection['wait_time_driver_confirm'] = waiting_time['wait_time_driver_confirm']
            self.kpi_collection['wait_time_total'] = waiting_time['wait_time_total']
            self.kpi_collection['wait_time_assignment'] = waiting_time['wait_time_assignment']
            self.kpi_collection['wait_time_pickup'] = waiting_time['wait_time_pickup']

            self.kpi_collection['service_score'] = self.compute_service_score()
            self.kpi_collection['active_driver_count'] = self.manager.active_driver_count()
            self.kpi_collection['active_passenger_count'] = self.manager.active_passenger_count()

            # Log the full KPI collection before saving
            # logging.info(f"[compute_all_metrics] KPI collection at {time_to_str(end_time)}: {self.kpi_collection}")

            # check if any KPI is None and log a warning if so
            for kpi_name, kpi_value in self.kpi_collection.items():
                if kpi_value is None:
                    logging.warning(f"KPI {kpi_name} is None at time {time_to_str(end_time)}")

            self.manager.save_kpi(time_to_str(end_time), self.kpi_collection)
            # logging.info(f"[compute_all_metrics] Successfully saved KPIs for {time_to_str(end_time)}")
        except Exception as e:
            # logging.exception(f"[compute_all_metrics] Exception occurred: {str(e)}")
            raise


    def get_active_driver_trips(self, sim_clock):
        return self.manager.get_active_driver_trips(sim_clock)

    def get_active_passenger_trips(self, sim_clock):
        return self.manager.get_active_passenger_trips(sim_clock)

    # publish_active_trips should be implemented in the agent, not the app

    def get_history_as_paths(self, timewindow_start, timewindow_end):
        return self.manager.get_history_as_paths(timewindow_start, timewindow_end)

    def prep_metric_computation_queries(self, start_time, end_time):
        self.passenger_trips_for_metric = self.manager.get_passenger_trips_for_metric(start_time, end_time)
        self.driver_trips_for_metric = self.manager.get_driver_trips_for_metric(start_time, end_time)

    # def get_passenger_trips_for_metric(self, start_time, end_time):
    #     return self.manager.get_passenger_trips_for_metric(start_time, end_time)

    # def get_driver_trips_for_metric(self, start_time, end_time):
    #     return self.manager.get_driver_trips_for_metric(start_time, end_time)

    # def active_driver_count(self):
    #     return self.manager.active_driver_count()

    # def active_passenger_count(self):
    #     return self.manager.active_passenger_count()

    def compute_revenue(self):
        step_revenue = 0
        for item in self.passenger_trips_for_metric:
            if item['state'] == RidehailPassengerTripStateMachine.passenger_completed_trip.name:
                step_revenue += item['trip_price']

        return step_revenue

    def compute_cancelled(self):
        num_cancelled = 0
        for item in self.passenger_trips_for_metric:
            if item['state'] == RidehailPassengerTripStateMachine.passenger_cancelled_trip.name:
                num_cancelled += 1

        return num_cancelled

    def compute_served(self):
        num_served = 0
        for item in self.passenger_trips_for_metric:
            if item['state'] == RidehailPassengerTripStateMachine.passenger_completed_trip.name:
                num_served += 1

        return num_served

    def compute_waiting_time(self):
        wait_time_assignment = 0
        wait_time_driver_confirm = 0
        wait_time_total = 0
        wait_time_pickup = 0
        for item in self.passenger_trips_for_metric:
            try:
                if item['state'] == RidehailPassengerTripStateMachine.passenger_completed_trip.name:
                    wait_time_driver_confirm += item['stats']['wait_time_driver_confirm']
                    wait_time_total += item['stats']['wait_time_total']
                    wait_time_assignment += item['stats']['wait_time_assignment']
                    wait_time_pickup += item['stats']['wait_time_pickup']
            except Exception as e:
                logging.exception(str(e))

        return {
            'wait_time_driver_confirm': wait_time_driver_confirm,
            'wait_time_total': wait_time_total,
            'wait_time_assignment': wait_time_assignment,
            'wait_time_pickup': wait_time_pickup,
        }

    def compute_service_score(self):
        service_score = 0
        for item in self.driver_trips_for_metric:
            if item['state'] == RidehailDriverTripStateMachine.driver_completed_trip.name:
                service_score += item['meta']['profile']['service_score']

        return service_score

    # def save_kpi(self, sim_clock, kpi_collection):
    #     return self.manager.save_kpi(sim_clock, kpi_collection)
