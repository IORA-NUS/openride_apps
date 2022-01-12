import os, sys, json
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging
# from mesa import Agent
from random import random
from .analytics_app import AnalyticsApp

from datetime import datetime
from dateutil.relativedelta import relativedelta

# from apps.messenger_service import Messenger

# from apps.orsim import ORSimAgent

from orsim import ORSimAgent

# from apps.config import analytics_settings, orsim_settings

class AnalyticsAgentIndie(ORSimAgent):
    ''' '''

    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler_id, behavior, orsim_settings):
        # # NOTE, model should include run_id and start_time
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler_id, behavior, orsim_settings)

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
            self.app = AnalyticsApp(self.run_id, self.get_current_time_str(),
                                self.credentials,
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
        self.app.logout()

    def estimate_next_event_time(self):
        ''' '''
        return self.current_time

    def step(self, time_step):
        ''' '''
        # if self.current_time_step % analytics_settings['STEPS_PER_ACTION'] == 0:
        if (self.current_time_step % self.behavior['STEPS_PER_ACTION'] == 0) and \
                    (random() <= self.behavior['RESPONSE_RATE']) and \
                    (self.next_event_time <= self.current_time):
            # Do not update next_event time for this agent

            self.compute_all_metrics()


            # print('AnalyticsAgent.step')
            output_dir = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/output/{self.run_id}"

            # Publish Active trips using websocket Protocol
            if self.behavior['PUBLISH_REALTIME_DATA']:
                location_stream, route_stream = self.app.publish_active_trips(self.get_current_time_str())
                # print(publish_dict)

                if self.behavior['WRITE_WS_OUTPUT_TO_FILE']:
                    stream_output_dir = f"{output_dir}/stream"
                    if not os.path.exists(stream_output_dir):
                        os.makedirs(stream_output_dir)

                    with open(f"{stream_output_dir}/{self.current_time_step}.location_stream.json", 'w') as publish_file:
                        publish_file.write(json.dumps(location_stream))

                    with open(f"{stream_output_dir}/{self.current_time_step}.route_stream.json", 'w') as publish_file:
                        publish_file.write(json.dumps(route_stream))


            # Gather history in timewindow as paths for visualization
            if self.behavior['PUBLISH_PATHS_HISTORY']:
                if (((self.current_time_step + 1) * self.orsim_settings['STEP_INTERVAL']) % self.behavior['PATHS_HISTORY_TIME_WINDOW'] ) == 0:
                    timewindow_end = self.current_time
                    timewindow_start = timewindow_end - relativedelta(seconds=self.behavior['PATHS_HISTORY_TIME_WINDOW']+self.orsim_settings['STEP_INTERVAL'])
                    logging.debug(f"{timewindow_start}, {timewindow_end}")

                    paths_history = self.app.get_history_as_paths(timewindow_start, timewindow_end)
                    # print(publish_dict)

                    if self.behavior['WRITE_PH_OUTPUT_TO_FILE']:
                        rest_output_dir = f"{output_dir}/rest"
                        if not os.path.exists(rest_output_dir):
                            os.makedirs(rest_output_dir)

                        with open(f"{rest_output_dir}/{self.current_time_step}.paths_history.json", 'w') as publish_file:
                            publish_file.write(json.dumps(paths_history))

            return True
        else:
            return False


    def compute_all_metrics(self):
        # METRICS COMPUTATION
        # start_time = self.current_time - relativedelta(seconds=(analytics_settings['STEPS_PER_ACTION'] * orsim_settings['STEP_INTERVAL'] ))
        start_time = self.current_time - relativedelta(seconds=(self.behavior['STEPS_PER_ACTION'] * self.orsim_settings['STEP_INTERVAL'] ))
        end_time = self.current_time
        self.app.prep_metric_computation_queries(start_time, end_time)

        # Compute and Store platform revenue
        self.kpi_collection['revenue'] = self.app.compute_revenue()

        # Compute and Store cancellation
        self.kpi_collection['num_cancelled'] = self.app.compute_cancelled()

        # Compute and Store Served
        self.kpi_collection['num_served'] = self.app.compute_served()

        # Compute and Store Waiting_time (sum)
        waiting_time = self.app.compute_waiting_time()
        self.kpi_collection['wait_time_driver_confirm'] = waiting_time['wait_time_driver_confirm']
        self.kpi_collection['wait_time_total'] = waiting_time['wait_time_total']
        self.kpi_collection['wait_time_assignment'] = waiting_time['wait_time_assignment']
        self.kpi_collection['wait_time_pickup'] = waiting_time['wait_time_pickup']

        # compute Service Score
        self.kpi_collection['service_score'] = self.app.compute_service_score()

        # active_driver_count
        self.kpi_collection['active_driver_count'] = self.app.active_driver_count()
        # passenger_count
        self.kpi_collection['active_passenger_count'] = self.app.active_passenger_count()


        self.app.save_kpi(self.get_current_time_str(), self.kpi_collection)
