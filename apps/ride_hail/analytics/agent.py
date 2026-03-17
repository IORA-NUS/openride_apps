import os, sys, json
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging
from random import random
from .app import AnalyticsApp

from datetime import datetime
from dateutil.relativedelta import relativedelta

from orsim import ORSimAgent


class AnalyticsAgentIndie(ORSimAgent):
    ''' '''

    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior)

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }
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

        try:
            self.app = AnalyticsApp(run_id=self.run_id,
                                    sim_clock=self.get_current_time_str(),
                                    credentials=self.credentials,
                                    messenger=self.messenger)
        except Exception as e:
            logging.exception(f"{self.unique_id = }: {str(e)}")
            self.agent_failed = True

    def process_payload(self, payload):
        did_step = False
        if payload.get('action') == 'step':
            did_step = self.step(payload.get('time_step'))

        return did_step

    def logout(self):
        self.step(self.current_time_step)
        self.app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        return self.current_time

    def step(self, time_step):
        if (self.current_time_step % self.behavior['steps_per_action'] == 0) and \
                    (random() <= self.behavior['response_rate']) and \
                    (self.next_event_time <= self.current_time):
            self.compute_all_metrics()

            output_dir = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/output/{self.run_id}"

            if self.behavior['publish_realtime_data']:
                location_stream, route_stream = self.app.publish_active_trips(self.get_current_time_str())

                if self.behavior['write_ws_output_to_file']:
                    stream_output_dir = f"{output_dir}/stream"
                    if not os.path.exists(stream_output_dir):
                        os.makedirs(stream_output_dir)

                    with open(f"{stream_output_dir}/{self.current_time_step}.location_stream.json", 'w') as publish_file:
                        publish_file.write(json.dumps(location_stream))

                    with open(f"{stream_output_dir}/{self.current_time_step}.route_stream.json", 'w') as publish_file:
                        publish_file.write(json.dumps(route_stream))

            if self.behavior['publish_paths_history']:
                if (((self.current_time_step + 1) * self.orsim_settings['STEP_INTERVAL']) % self.behavior['paths_history_time_window']) == 0:
                    timewindow_end = self.current_time
                    timewindow_start = timewindow_end - relativedelta(seconds=self.behavior['paths_history_time_window'] + self.orsim_settings['STEP_INTERVAL'])
                    logging.debug(f"{timewindow_start}, {timewindow_end}")

                    paths_history = self.app.get_history_as_paths(timewindow_start, timewindow_end)

                    if self.behavior['write_ph_output_to_file']:
                        rest_output_dir = f"{output_dir}/rest"
                        if not os.path.exists(rest_output_dir):
                            os.makedirs(rest_output_dir)

                        with open(f"{rest_output_dir}/{self.current_time_step}.paths_history.json", 'w') as publish_file:
                            publish_file.write(json.dumps(paths_history))

            return True
        else:
            return False

    def compute_all_metrics(self):
        start_time = self.current_time - relativedelta(seconds=(self.behavior['steps_per_action'] * self.orsim_settings['STEP_INTERVAL']))
        end_time = self.current_time
        self.app.prep_metric_computation_queries(start_time, end_time)

        self.kpi_collection['revenue'] = self.app.compute_revenue()
        self.kpi_collection['num_cancelled'] = self.app.compute_cancelled()
        self.kpi_collection['num_served'] = self.app.compute_served()

        waiting_time = self.app.compute_waiting_time()
        self.kpi_collection['wait_time_driver_confirm'] = waiting_time['wait_time_driver_confirm']
        self.kpi_collection['wait_time_total'] = waiting_time['wait_time_total']
        self.kpi_collection['wait_time_assignment'] = waiting_time['wait_time_assignment']
        self.kpi_collection['wait_time_pickup'] = waiting_time['wait_time_pickup']

        self.kpi_collection['service_score'] = self.app.compute_service_score()
        self.kpi_collection['active_driver_count'] = self.app.active_driver_count()
        self.kpi_collection['active_passenger_count'] = self.app.active_passenger_count()

        # check if any KPI is None and log a warning if so
        for kpi_name, kpi_value in self.kpi_collection.items():
            if kpi_value is None:
                logging.warning(f"KPI {kpi_name} is None at time {self.get_current_time_str()}")

        self.app.save_kpi(self.get_current_time_str(), self.kpi_collection)
