import os, sys, json
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging
from mesa import Agent
from .analytics_app import AnalyticsApp

from datetime import datetime
from dateutil.relativedelta import relativedelta

from apps.messenger_service import Messenger

from apps.orsim import ORSimAgent

from apps.config import analytics_settings, orsim_settings

class AnalyticsAgentIndie(ORSimAgent):
    ''' '''

    def __init__(self, unique_id, run_id, reference_time, scheduler_id, behavior):
        # # NOTE, model should include run_id and start_time
        super().__init__(unique_id, run_id, reference_time, scheduler_id, behavior)

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.analytics_app = AnalyticsApp(self.run_id, self.get_current_time_str(), self.credentials)

    def process_payload(self, payload):
        if payload.get('action') == 'step':
            self.step(payload.get('time_step'))


    def logout(self):
        self.step(self.current_time_step)

    def step(self, time_step):
        ''' '''
        if self.current_time_step % analytics_settings['STEPS_PER_ACTION'] == 0:

            self.compute_all_metrics()


            # print('AnalyticsAgent.step')
            output_dir = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/output/{self.run_id}"

            # Publish Active trips using websocket Protocol
            if analytics_settings['PUBLISH_REALTIME_DATA']:
                location_stream, route_stream = self.analytics_app.publish_active_trips(self.get_current_time_str())
                # print(publish_dict)

                if analytics_settings['WRITE_WS_OUTPUT_TO_FILE']:
                    stream_output_dir = f"{output_dir}/stream"
                    if not os.path.exists(stream_output_dir):
                        os.makedirs(stream_output_dir)

                    with open(f"{stream_output_dir}/{self.current_time_step}.location_stream.json", 'w') as publish_file:
                        publish_file.write(json.dumps(location_stream))

                    with open(f"{stream_output_dir}/{self.current_time_step}.route_stream.json", 'w') as publish_file:
                        publish_file.write(json.dumps(route_stream))


            # Gather history in timewindow as paths for visualization
            if analytics_settings['PUBLISH_PATHS_HISTORY']:
                if (((self.current_time_step + 1) * orsim_settings['STEP_INTERVAL']) % analytics_settings['PATHS_HISTORY_TIME_WINDOW'] ) == 0:
                    timewindow_end = self.current_time
                    timewindow_start = timewindow_end - relativedelta(seconds=analytics_settings['PATHS_HISTORY_TIME_WINDOW']+orsim_settings['STEP_INTERVAL'])
                    logging.info(f"{timewindow_start}, {timewindow_end}")

                    paths_history = self.analytics_app.get_history_as_paths(timewindow_start, timewindow_end)
                    # print(publish_dict)

                    if analytics_settings['WRITE_PH_OUTPUT_TO_FILE']:
                        rest_output_dir = f"{output_dir}/rest"
                        if not os.path.exists(rest_output_dir):
                            os.makedirs(rest_output_dir)

                        with open(f"{rest_output_dir}/{self.current_time_step}.paths_history.json", 'w') as publish_file:
                            publish_file.write(json.dumps(paths_history))


    def compute_all_metrics(self):
        # METRICS COMPUTATION
        start_time = self.current_time - relativedelta(seconds=(analytics_settings['STEPS_PER_ACTION'] * orsim_settings['STEP_INTERVAL'] ))
        end_time = self.current_time
        self.analytics_app.prep_metric_computation_queries(start_time, end_time)

        # Compute and Store platform revenue
        step_revenue = self.analytics_app.compute_revenue()
        self.analytics_app.save_kpi(self.get_current_time_str(), 'revenue', step_revenue)

        # Compute and Store cancellation
        num_cancelled = self.analytics_app.compute_cancelled()
        self.analytics_app.save_kpi(self.get_current_time_str(), 'cancelled', num_cancelled)

        # Compute and Store Served
        num_served = self.analytics_app.compute_served()
        self.analytics_app.save_kpi(self.get_current_time_str(), 'served', num_served)

        # Compute and Store Waiting_time (sum)
        wait_time_driver_confirm, wait_time_total, wait_time_assignment = self.analytics_app.compute_waiting_time()
        self.analytics_app.save_kpi(self.get_current_time_str(), 'wait_time_driver_confirm', wait_time_driver_confirm)
        self.analytics_app.save_kpi(self.get_current_time_str(), 'wait_time_total', wait_time_total)
        self.analytics_app.save_kpi(self.get_current_time_str(), 'wait_time_assignment', wait_time_assignment)
