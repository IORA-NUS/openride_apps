import os, sys, json, time, logging
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

from random import random
from .app import AssignmentApp
from apps.loc_service import PlanningArea

from shapely.geometry import MultiPolygon, mapping
from datetime import datetime
from dateutil.relativedelta import relativedelta

from orsim.lifecycle import ORSimAgent


class AssignmentAgentIndie(ORSimAgent):
    ''' '''

    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior)

        self.credentials = {
            'email': self.behavior.get('email'),
            'password': self.behavior.get('password'),
        }

        try:
            self.app = AssignmentApp(
                run_id=self.run_id,
                sim_clock=self.get_current_time_str(),
                credentials=self.credentials,
                solver_name=self.behavior['solver'],
                solver_params=self.behavior['solver_params'],
                steps_per_action=self.behavior['steps_per_action'],
                messenger=self.messenger,
                persona=self.behavior.get('persona', {})
            )
        except Exception as e:
            logging.exception(f"{self.unique_id = }: {str(e)}")
            self.agent_failed = True

    def process_payload(self, payload):
        did_step = False
        if payload.get('action') == 'step':
            did_step = self.step(payload.get('time_step'))

        return did_step

    def logout(self):
        self.app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        return self.current_time

    def step(self, time_step):
        if (self.current_time_step % self.behavior['steps_per_action'] == 0) and \
                    (random() <= self.behavior['response_rate']):
            result = self.app.assign(self.get_current_time_str(), self.current_time_step)
            self.app.publish(result)
            return True
        else:
            return False
