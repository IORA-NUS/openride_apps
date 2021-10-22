import os, sys, json, time
# from time import time
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from mesa import Agent
from .assignment_app import AssignmentApp
from apps.config import settings
from apps.loc_service import PlanningArea

from shapely.geometry import MultiPolygon, mapping
from datetime import datetime
from dateutil.relativedelta import relativedelta

from apps.messenger_service import Messenger

from apps.orsim import ORSimAgent

class AssignmentAgentIndie(ORSimAgent):
    ''' '''

    def __init__(self, unique_id, run_id, reference_time, scheduler_id, behavior):

        super().__init__(unique_id, run_id, reference_time, scheduler_id, behavior)

        self.sim_settings = settings['SIM_SETTINGS']

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        # self.assignment_app = AssignmentApp(model.run_id, model.get_current_time_str(), self.credentials, self.behavior['solver'], self.behavior['solver_params'])
        self.assignment_app = AssignmentApp(self.run_id, self.get_current_time_str(), self.credentials, self.behavior['solver'], self.behavior['solver_params'])



    #     self.agent_messenger = Messenger(run_id, self.credentials, f"sim_agent_{self.unique_id}", self.on_receive_message)

    def process_payload(self, payload):
        if payload.get('action') == 'step':
            time.sleep(1)
            self.step(payload.get('time_step'))


    # def process_message(self, client, userdata, message):
    #     ''' '''
    #     payload = json.loads(message.payload.decode('utf-8'))

    #     if payload.get('action') == 'step':
    #         time.sleep(1)
    #         self.step(payload.get('time_step'))

    # def on_receive_message(self, client, userdata, message):
    #     ''' '''
    #     payload = json.loads(message.payload.decode('utf-8'))

    #     if payload.get('action') == 'step':
    #         self.step(payload.get('time_step'))

    #     response_payload = {
    #         'agent_id': self.unique_id,
    #         'action': 'completed'
    #     }

    #     self.agent_messenger.client.publish(f'sim_agent/response', json.dumps(response_payload))


    # # def get_current_time_str(self):
    # #     return self.model.get_current_time_str()
    # def get_current_time_str(self):
    #     return datetime.strftime(self.current_time, "%a, %d %b %Y %H:%M:%S GMT")

    # @classmethod
    # def load_behavior(cls, unique_id, behavior=None):
    #     ''' '''
    #     if behavior is None:
    #         behavior = {
    #             'email': f'{unique_id}@test.com',
    #             'password': 'password',

    #             # 'solver': 'RandomAssignment',
    #             'solver': 'CompromiseMatching',

    #             'solver_params': {
    #                 'name': settings['SIM_SETTINGS']['PLANNING_AREA'],
    #                 # 'area': {
    #                 #     # NOTE This must be a MultiPolygon describing the specific region where this engine will gather Supply / demand
    #                 #     'center': {'type': 'Point', 'coordinates': (103.833057754201, 1.41709038337595)},
    #                 #     'radius': 50000, # meters
    #                 # },
    #                 'area': mapping(PlanningArea().get_planning_area(settings['SIM_SETTINGS']['PLANNING_AREA'])),

    #                 'offline_params': {
    #                     'reverseParameter': 480,  # 480;
    #                     'reverseParameter2': 2.5,
    #                     'gamma': 1.2,     # the target below is estimated from historical data

    #                     # KPI Targets
    #                     'targetReversePickupTime': 4915 * 1.2, # gamma
    #                     'targetServiceScore': 5439 * 1.2, # gamma
    #                     'targetRevenue': 4185 * 1.2, # gamma
    #                 },
    #                 'online_params': {
    #                     'realtimePickupTime': 0,
    #                     'realtimeRevenue': 0,
    #                     'realtimeServiceScore': 0,

    #                     'weightPickupTime': 1,
    #                     'weightRevenue': 1,
    #                     'weightServiceScore': 1,
    #                 },
    #             },
    #         }

    #     # print(f"{behavior['solver_params']['area']=}")
    #     return behavior


    # def step(self):
    def step(self, time_step):
        ''' '''
        # self.refresh(time_step)

        # print('AssignmentAgent.step')
        if self.current_time_step % self.sim_settings['NUMSTEPS_BETWEEN_SOLVER'] == 0:
            result = self.assignment_app.assign(self.get_current_time_str(), self.current_time_step)
        # print('After assign')

            self.assignment_app.publish(result)

        if self.current_time_step == self.sim_settings['SIM_DURATION']-1:
            self.shutdown()



    # def refresh(self, time_step):
    #     self.prev_time_step = self.current_time_step
    #     # self.current_time_step = self.model.driver_schedule.time
    #     self.current_time_step = time_step
    #     self.elapsed_duration_steps = self.current_time_step - self.prev_time_step

    #     self.current_time = self.reference_time + relativedelta(seconds = time_step * self.sim_settings['SIM_STEP_SIZE'])

