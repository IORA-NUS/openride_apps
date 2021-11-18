import os, sys, json, time
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

# from mesa import Agent
from random import random
from .assignment_app import AssignmentApp
from apps.loc_service import PlanningArea

from shapely.geometry import MultiPolygon, mapping
from datetime import datetime
from dateutil.relativedelta import relativedelta

from apps.messenger_service import Messenger

from apps.orsim import ORSimAgent
from apps.config import assignment_settings, orsim_settings

class AssignmentAgentIndie(ORSimAgent):
    ''' '''

    def __init__(self, unique_id, run_id, reference_time, scheduler_id, behavior):

        super().__init__(unique_id, run_id, reference_time, scheduler_id, behavior)

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        self.assignment_app = AssignmentApp(self.run_id, self.get_current_time_str(), self.credentials, self.behavior['solver'], self.behavior['solver_params'], self.behavior['STEPS_PER_ACTION'])


    def process_payload(self, payload):
        if payload.get('action') == 'step':
            # time.sleep(1)
            self.step(payload.get('time_step'))


    def logout(self):
        pass

    def estimate_next_event_time(self):
        ''' '''
        return self.current_time

    def step(self, time_step):
        ''' '''
        # if self.current_time_step % assignment_settings['STEPS_PER_ACTION'] == 0:
        if (self.current_time_step % self.behavior['STEPS_PER_ACTION'] == 0) and \
                    (random() <= self.behavior['RESPONSE_RATE']) and \
                    (self.next_event_time <= self.current_time):

            result = self.assignment_app.assign(self.get_current_time_str(), self.current_time_step)
            self.assignment_app.publish(result)
            # Do not update next_event time for this agent
