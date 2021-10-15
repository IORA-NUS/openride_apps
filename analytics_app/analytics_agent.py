import os, sys, json
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from config import settings
from mesa import Agent
from .analytics_app import AnalyticsApp

from datetime import datetime
from dateutil.relativedelta import relativedelta

class AnalyticsAgent(Agent):
    ''' '''

    def __init__(self, unique_id, model, behavior=None):
        super().__init__(unique_id, model)

        if behavior is not None:
            self.behavior = behavior
        else:
            self.behavior = AnalyticsAgent.load_behavior(unique_id)

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.analytics_app = AnalyticsApp(model.run_id, model.get_current_time_str(), self.credentials)

    def get_current_time_str(self):
        return self.model.get_current_time_str()

    @classmethod
    def load_behavior(cls, unique_id, behavior=None):
        ''' '''
        if behavior is None:
            behavior = {
                'email': f'{unique_id}@test.com',
                'password': 'password',

            }

        return behavior


    def step(self):
        ''' '''
        # print('AnalyticsAgent.step')

        # Publish Active trips using websocket Protocol
        if settings['PUBLISH_REALTIME_DATA']:
            location_stream, route_stream = self.analytics_app.publish_active_trips(self.get_current_time_str())
            # print(publish_dict)

            if settings['WRITE_WS_OUTPUT_TO_FILE']:
                current_dir = os.path.dirname(os.path.abspath(__file__))
                if not os.path.exists(f"{current_dir}/output/{self.model.run_id}"):
                    os.makedirs(f"{current_dir}/output/{self.model.run_id}")

                with open(f"{current_dir}/output/{self.model.run_id}/{self.model.service_schedule.time}.location_stream.json", 'w') as publish_file:
                    publish_file.write(json.dumps(location_stream))

                with open(f"{current_dir}/output/{self.model.run_id}/{self.model.service_schedule.time}.route_stream.json", 'w') as publish_file:
                    publish_file.write(json.dumps(route_stream))


        # Gather history in timewindow as paths for visualization
        if settings['PUBLISH_PATHS_HISTORY']:
            if (((self.model.service_schedule.time + 1) * settings['SIM_STEP_SIZE']) % settings['PATHS_HISTORY_TIME_WINDOW'] ) == 0:
                timewindow_end = self.model.get_current_time()
                timewindow_start = timewindow_end - relativedelta(seconds=settings['PATHS_HISTORY_TIME_WINDOW']+settings['SIM_STEP_SIZE'])
                print(timewindow_start, timewindow_end)

                paths_history = self.analytics_app.get_history_as_paths(timewindow_start, timewindow_end)
                # print(publish_dict)

                if settings['WRITE_PH_OUTPUT_TO_FILE']:
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    if not os.path.exists(f"{current_dir}/output/{self.model.run_id}"):
                        os.makedirs(f"{current_dir}/output/{self.model.run_id}")

                    with open(f"{current_dir}/output/{self.model.run_id}/{self.model.service_schedule.time}.paths_history.json", 'w') as publish_file:
                        publish_file.write(json.dumps(paths_history))

