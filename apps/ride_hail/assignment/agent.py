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

    def _create_app(self):
        return AssignmentApp(
            run_id=self.run_id,
            sim_clock=self.get_current_time_str(),
            behavior=self.behavior,
            messenger=self.messenger,
        )

    @property
    def process_payload_on_init(self):
        return False

    def entering_market(self, time_step):
        # Be sure to set self.active = True at the end of this method, otherwise the agent will not step in the next time steps.
        self.active = True

    def exiting_market(self):
        self.active = False

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
