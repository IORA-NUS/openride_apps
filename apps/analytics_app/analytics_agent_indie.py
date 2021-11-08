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
        pass

    def step(self, time_step):
        ''' '''
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
            if (((self.current_time_step + 1) * orsim_settings['SIM_STEP_SIZE']) % analytics_settings['PATHS_HISTORY_TIME_WINDOW'] ) == 0:
                timewindow_end = self.current_time
                timewindow_start = timewindow_end - relativedelta(seconds=analytics_settings['PATHS_HISTORY_TIME_WINDOW']+orsim_settings['SIM_STEP_SIZE'])
                logging.info(f"{timewindow_start}, {timewindow_end}")

                paths_history = self.analytics_app.get_history_as_paths(timewindow_start, timewindow_end)
                # print(publish_dict)

                if analytics_settings['WRITE_PH_OUTPUT_TO_FILE']:
                    rest_output_dir = f"{output_dir}/rest"
                    if not os.path.exists(rest_output_dir):
                        os.makedirs(rest_output_dir)

                    with open(f"{rest_output_dir}/{self.current_time_step}.paths_history.json", 'w') as publish_file:
                        publish_file.write(json.dumps(paths_history))

